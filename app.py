from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
import nonebot
from nonebot import on_message, on_metaevent, on_notice, on_request
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageEvent, MessageSegment

from core.chat_splitter import coalesce_for_rate_limit, split_semantic_text
from core.napcat_compat import build_napcat_file_reference, call_napcat_bot_api
from core import prompt_loader as _pl
from core.engine import EngineMessage, EngineResponse, YukikoEngine
from core.queue import GroupQueueDispatcher
from utils.text import clip_text, normalize_text

_MEDIA_HTTP_TIMEOUT = httpx.Timeout(12.0, connect=8.0)
_MEDIA_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MEDIA_VIDEO_PROBE_MAX_BYTES = 512 * 1024
_MEDIA_MIN_VIDEO_BYTES = 180 * 1024
_MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
from core.tools_types import _find_ffmpeg as _find_ffmpeg_tool
_FFPROBE_BIN = _find_ffmpeg_tool("ffprobe")

_log = logging.getLogger("yukiko.app")
_GROUP_SEND_BLOCK_UNTIL: dict[int, datetime] = {}
_GROUP_SEND_BLOCK_REASON: dict[int, str] = {}
_GROUP_SEND_BLOCK_DEFAULT_SECONDS = 180
_GROUP_MEMBER_PROBE_SKIP_UNTIL: dict[int, datetime] = {}
_BOT_SEND_SUSPEND_UNTIL: dict[str, datetime] = {}
_BOT_SEND_SUSPEND_REASON: dict[str, str] = {}
_BOT_ONLINE_STATE: dict[str, bool] = {}
_RUNTIME_WEBUI_BRIDGE: dict[str, Any] = {
    "queue": None,
    "latest_ctx": {},
}


def get_runtime_agent_states(limit: int = 200) -> list[dict[str, Any]]:
    """供 WebUI 查询当前队列运行状态。"""
    queue = _RUNTIME_WEBUI_BRIDGE.get("queue")
    if queue is None or not hasattr(queue, "list_conversation_states"):
        return []
    try:
        rows = queue.list_conversation_states(limit=max(1, int(limit)))
    except Exception:
        return []
    latest_ctx = _RUNTIME_WEBUI_BRIDGE.get("latest_ctx")
    if not isinstance(latest_ctx, dict):
        latest_ctx = {}
    enriched: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = normalize_text(str(row.get("conversation_id", "")))
        ctx = latest_ctx.get(cid, {}) if cid else {}
        if not isinstance(ctx, dict):
            ctx = {}
        item = dict(row)
        item["last_trace_id"] = normalize_text(str(ctx.get("trace_id", ""))) or normalize_text(str(row.get("latest_trace_id", "")))
        item["last_user_id"] = normalize_text(str(ctx.get("user_id", "")))
        item["last_text_preview"] = clip_text(normalize_text(str(ctx.get("text", ""))), 120)
        item["last_update"] = normalize_text(str(ctx.get("timestamp", "")))
        enriched.append(item)
    return enriched


async def interrupt_runtime_conversation(conversation_id: str, reason: str = "cancelled_by_webui") -> dict[str, int]:
    """供 WebUI 主动中断会话任务。"""
    cid = normalize_text(conversation_id)
    if not cid:
        return {"cancelled": 0, "skipped_non_interruptible": 0, "skipped_running": 0, "skipped_finished": 0}
    queue = _RUNTIME_WEBUI_BRIDGE.get("queue")
    if queue is None or not hasattr(queue, "cancel_conversation"):
        return {"cancelled": 0, "skipped_non_interruptible": 0, "skipped_running": 0, "skipped_finished": 0}
    try:
        result = await queue.cancel_conversation(
            cid,
            reason=normalize_text(reason) or "cancelled_by_webui",
            include_running=True,
            interruptible_only=True,
        )
    except Exception:
        return {"cancelled": 0, "skipped_non_interruptible": 0, "skipped_running": 0, "skipped_finished": 0}
    latest_ctx = _RUNTIME_WEBUI_BRIDGE.get("latest_ctx")
    if isinstance(latest_ctx, dict):
        latest_ctx.pop(cid, None)
    return result if isinstance(result, dict) else {
        "cancelled": 0,
        "skipped_non_interruptible": 0,
        "skipped_running": 0,
        "skipped_finished": 0,
    }


class _TokenBucket:
    def __init__(self, capacity: int, refill_seconds: int, warn_threshold: int):
        self.capacity = max(1, int(capacity))
        self.refill_seconds = max(1, int(refill_seconds))
        self.refill_per_second = self.capacity / float(self.refill_seconds)
        self.warn_threshold = max(1, min(self.capacity, int(warn_threshold)))
        self.tokens = float(self.capacity)
        self.updated_at = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = max(0.0, now - self.updated_at)
        self.updated_at = now
        if elapsed <= 0:
            return
        self.tokens = min(float(self.capacity), self.tokens + elapsed * self.refill_per_second)

    def reserve(self, amount: int = 1) -> tuple[float, bool]:
        self._refill()
        need = max(1, int(amount))
        if self.tokens >= need:
            self.tokens -= need
            return 0.0, self.used_in_window() >= self.warn_threshold
        missing = float(need) - self.tokens
        wait_seconds = missing / self.refill_per_second if self.refill_per_second > 0 else float(self.refill_seconds)
        self.tokens = 0.0
        return max(0.0, wait_seconds), True

    def used_in_window(self) -> int:
        return int(math.ceil(float(self.capacity) - self.tokens))

    def near_warn(self) -> bool:
        self._refill()
        return self.used_in_window() >= self.warn_threshold


_SEND_RATE_BUCKETS: dict[str, _TokenBucket] = {}


def _check_group_send_block(group_id: int) -> tuple[bool, str]:
    if group_id <= 0:
        return False, ""
    until = _GROUP_SEND_BLOCK_UNTIL.get(group_id)
    if not isinstance(until, datetime):
        return False, ""
    now = datetime.now(timezone.utc)
    if now >= until:
        _GROUP_SEND_BLOCK_UNTIL.pop(group_id, None)
        _GROUP_SEND_BLOCK_REASON.pop(group_id, None)
        return False, ""
    return True, _GROUP_SEND_BLOCK_REASON.get(group_id, "temporary_block")


def _mark_group_send_block(group_id: int, until: datetime, reason: str) -> None:
    if group_id <= 0:
        return
    _GROUP_SEND_BLOCK_UNTIL[group_id] = until
    _GROUP_SEND_BLOCK_REASON[group_id] = reason
    _log.warning(
        "group_send_blocked | group=%s | until=%s | reason=%s",
        group_id,
        until.astimezone(timezone.utc).isoformat(),
        reason,
    )


def _should_skip_group_member_probe(group_id: int) -> bool:
    if group_id <= 0:
        return False
    until = _GROUP_MEMBER_PROBE_SKIP_UNTIL.get(group_id)
    if not isinstance(until, datetime):
        return False
    now = datetime.now(timezone.utc)
    if now >= until:
        _GROUP_MEMBER_PROBE_SKIP_UNTIL.pop(group_id, None)
        return False
    return True


def _mark_group_member_probe_skip(group_id: int, *, seconds: int = 900, reason: str = "") -> None:
    if group_id <= 0:
        return
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=max(30, int(seconds)))
    prev = _GROUP_MEMBER_PROBE_SKIP_UNTIL.get(group_id)
    if isinstance(prev, datetime) and prev > until:
        until = prev
    _GROUP_MEMBER_PROBE_SKIP_UNTIL[group_id] = until
    _log.info(
        "group_member_probe_skip_set | group=%s | until=%s | reason=%s",
        group_id,
        until.astimezone(timezone.utc).isoformat(),
        normalize_text(reason) or "-",
    )


def _check_bot_send_suspended(bot_id: str) -> tuple[bool, str]:
    bid = normalize_text(str(bot_id))
    if not bid:
        return False, ""
    until = _BOT_SEND_SUSPEND_UNTIL.get(bid)
    if not isinstance(until, datetime):
        return False, ""
    now = datetime.now(timezone.utc)
    if now >= until:
        _BOT_SEND_SUSPEND_UNTIL.pop(bid, None)
        _BOT_SEND_SUSPEND_REASON.pop(bid, None)
        return False, ""
    return True, _BOT_SEND_SUSPEND_REASON.get(bid, "send_channel_suspended")


def _suspend_bot_send(bot_id: str, seconds: int, reason: str) -> None:
    bid = normalize_text(str(bot_id))
    if not bid:
        return
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=max(5, int(seconds)))
    prev = _BOT_SEND_SUSPEND_UNTIL.get(bid)
    if isinstance(prev, datetime) and prev > until:
        until = prev
    _BOT_SEND_SUSPEND_UNTIL[bid] = until
    _BOT_SEND_SUSPEND_REASON[bid] = normalize_text(reason) or "send_channel_suspended"
    _log.warning(
        "bot_send_suspended | bot=%s | until=%s | reason=%s",
        bid,
        until.astimezone(timezone.utc).isoformat(),
        _BOT_SEND_SUSPEND_REASON[bid],
    )


def _resume_bot_send(bot_id: str, reason: str = "") -> None:
    bid = normalize_text(str(bot_id))
    if not bid:
        return
    had = bid in _BOT_SEND_SUSPEND_UNTIL
    _BOT_SEND_SUSPEND_UNTIL.pop(bid, None)
    _BOT_SEND_SUSPEND_REASON.pop(bid, None)
    if had:
        _log.info("bot_send_resumed | bot=%s | reason=%s", bid, normalize_text(reason) or "-")


def _resolve_send_rate_profile(config: dict[str, Any]) -> tuple[int, int, int, bool]:
    send_rate_cfg = config.get("send_rate", {}) if isinstance(config, dict) else {}
    if not isinstance(send_rate_cfg, dict):
        send_rate_cfg = {}
    control_cfg = config.get("control", {}) if isinstance(config, dict) else {}
    if not isinstance(control_cfg, dict):
        control_cfg = {}

    profile = normalize_text(
        str(
            send_rate_cfg.get(
                "profile",
                control_cfg.get("send_rate_profile", "safe_qq_group"),
            )
        )
    ).lower()
    if not profile:
        profile = "safe_qq_group"

    defaults: dict[str, tuple[int, int, int]] = {
        "safe_qq_group": (10, 60, 8),
        "balanced": (12, 60, 9),
        "active": (15, 60, 12),
    }
    cap_default, refill_default, warn_default = defaults.get(profile, defaults["safe_qq_group"])
    max_per_window = max(1, int(send_rate_cfg.get("max_per_window", send_rate_cfg.get("max_per_minute", cap_default))))
    refill_seconds = max(10, int(send_rate_cfg.get("window_seconds", refill_default)))
    warn_threshold = max(1, int(send_rate_cfg.get("warn_threshold", warn_default)))
    enable = bool(send_rate_cfg.get("enable", True))
    return max_per_window, refill_seconds, min(max_per_window, warn_threshold), enable


def _get_send_bucket(
    conversation_id: str,
    group_id: int,
    max_per_window: int,
    refill_seconds: int,
    warn_threshold: int,
) -> _TokenBucket:
    key = f"group:{group_id}" if group_id > 0 else f"conv:{conversation_id}"
    bucket = _SEND_RATE_BUCKETS.get(key)
    if (
        bucket is None
        or bucket.capacity != max_per_window
        or bucket.refill_seconds != refill_seconds
        or bucket.warn_threshold != warn_threshold
    ):
        bucket = _TokenBucket(
            capacity=max_per_window,
            refill_seconds=refill_seconds,
            warn_threshold=warn_threshold,
        )
        _SEND_RATE_BUCKETS[key] = bucket
    return bucket


async def _maybe_block_group_send_on_error(bot: Bot, event: MessageEvent, exc: Exception) -> bool:
    """检测发送失败是否为群禁言/权限拒绝，并在短时间内停发，避免刷屏重试。"""
    group_id = int(getattr(event, "group_id", 0) or 0)
    if group_id <= 0:
        return False

    err_text = normalize_text(str(exc))
    err_lower = err_text.lower()
    is_rate_limited = bool(
        re.search(r'"result"\s*:\s*299\b', err_text)
        or re.search(r"\bresult\s*[:=]\s*299\b", err_lower)
        or "rate limit" in err_lower
        or "发送频率" in err_text
        or "过快" in err_text
    )
    if not (
        is_rate_limited
        or
        re.search(r'"result"\s*:\s*120\b', err_text)
        or re.search(r"\bresult\s*[:=]\s*120\b", err_lower)
        or "forbidden" in err_lower
        or "mute" in err_lower
        or "禁言" in err_text
    ):
        return False

    now = datetime.now(timezone.utc)
    if is_rate_limited:
        until = now + timedelta(seconds=65)
        reason = "send_error_299_rate_limit"
    else:
        until = now + timedelta(seconds=_GROUP_SEND_BLOCK_DEFAULT_SECONDS)
        reason = "send_error_120_or_forbidden"

    # 尝试读取机器人在该群的禁言结束时间，尽量给出精确停发窗口。
    try:
        if not is_rate_limited:
            if _should_skip_group_member_probe(group_id):
                info = {}
            else:
                info = await call_napcat_bot_api(
                    bot,
                    "get_group_member_info",
                    group_id=group_id,
                    user_id=int(bot.self_id),
                    no_cache=True,
                )
        else:
            info = {}
        payload: dict[str, Any] = {}
        if isinstance(info, dict):
            data_part = info.get("data")
            payload = data_part if isinstance(data_part, dict) else info
        if isinstance(payload, dict):
            shut_ts = 0
            for key in ("shut_up_timestamp", "shut_up_time", "mute_end_time"):
                raw_val = payload.get(key)
                try:
                    shut_ts = int(raw_val or 0)
                except Exception:
                    shut_ts = 0
                if shut_ts > 0:
                    break
            now_ts = int(now.timestamp())
            if shut_ts > now_ts:
                until = datetime.fromtimestamp(shut_ts, timezone.utc)
                reason = f"group_member_muted_until:{shut_ts}"
    except Exception as probe_exc:
        probe_text = normalize_text(str(probe_exc))
        probe_lower = probe_text.lower()
        if ("成员" in probe_text and "不存在" in probe_text) or (
            "member" in probe_lower and ("not exists" in probe_lower or "not exist" in probe_lower or "not found" in probe_lower)
        ):
            # 机器人不在群里时，停止短期内重复探测，避免持续触发 NapCat 错误日志。
            _mark_group_member_probe_skip(group_id, reason="member_not_found")
        _log.debug("group_send_block_probe_fail | group=%s | %s", group_id, probe_exc)

    _mark_group_send_block(group_id=group_id, until=until, reason=reason)
    return True


def _build_send_error_text(exc: Exception) -> str:
    """展开异常链，提升发送错误分类的命中率。"""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        ident = id(current)
        if ident in seen:
            break
        seen.add(ident)
        text = normalize_text(str(current))
        if text:
            parts.append(text)
        nxt = current.__cause__ or current.__context__
        current = nxt if isinstance(nxt, BaseException) else None
    return " | ".join(parts)


def _is_transient_send_error(exc: Exception) -> bool:
    """识别可短暂重试的发送异常（网络抖动/连接瞬断）。"""
    err_text = _build_send_error_text(exc)
    err_lower = err_text.lower()
    err_compact = re.sub(r"\s+", "", err_lower)
    if not err_text:
        return False
    # NapCat 发消息通道超时通常不是瞬时抖动，重试会导致重复刷屏。
    if "timeout:ntevent" in err_compact and "nodeikernelmsgservice/sendmsg" in err_compact:
        return False
    # 明确不可重试的限流/权限类错误，避免无意义重试。
    if (
        re.search(r'"result"\s*:\s*299\b', err_text)
        or re.search(r"\bresult\s*[:=]\s*299\b", err_lower)
        or re.search(r'"result"\s*:\s*120\b', err_text)
        or re.search(r"\bresult\s*[:=]\s*120\b", err_lower)
        or "forbidden" in err_lower
        or "mute" in err_lower
        or "禁言" in err_text
        or "发送频率" in err_text
        or "过快" in err_text
    ):
        return False
    transient_cues = (
        "网络连接异常",
        "network abnormal",
        "connection reset",
        "connection aborted",
        "connection closed",
        "websocket",
        "ws closed",
        "timeout",
        "timed out",
        "1006514",
    )
    return any(cue in err_lower for cue in transient_cues) or any(cue in err_text for cue in ("网络连接异常",))


def _is_unretryable_send_error(exc: Exception) -> bool:
    """这一条消息不该重发，但**不代表发送通道坏了**。

    NapCat 的 sendMsg 回调超时/失败属于「结果未知」：消息可能已经发出去了，
    只是内核没把回调送回来。所以不能重发（会重复刷屏），
    但也没有任何依据据此停掉整个 bot 的发送。

    实测（storage/logs/yukiko.log，2026-08-05 ~ 08-06）：
      报文 `EventChecker Failed: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg
      ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate` 共 31 次，
      分布在 video(12) / upload_group_file(6) / 纯文本(8) / 无通道记录(8) —— 多种通道都会撞。
      同期成功发送 215 次，说明通道并没有坏。
    """
    err_text = _build_send_error_text(exc)
    err_lower = err_text.lower()
    err_compact = re.sub(r"\s+", "", err_lower)
    if not err_text:
        return False
    if "timeout:ntevent" in err_compact and "nodeikernelmsgservice/sendmsg" in err_compact:
        return True
    if "nodeikernelmsglistener/onmsginfolistupdate" in err_compact and "sendmsg" in err_compact:
        return True
    return False


def _is_hard_send_channel_error(exc: Exception) -> bool:
    """识别**发送通道真的不可用**、必须停掉全 bot 发送的错误。

    只有「账号掉线 / 登录失效」够得上这个判定 —— 那时候发什么都不会到。

    这里原先还包含 sendMsg 回调失败那两条，那是把两个不同的决定混成了一个：
      1. 这条消息别重发（对，避免重复刷屏）
      2. 停掉所有群的发送 120 秒（没有依据）
    原注释给的理由是「重试会导致重复刷屏」，那只支持第 1 条。
    实测代价：11 个撞上该报文的回合**零交付**（7 个 delivered=False、
    4 个连 send_final 都没有），而同期通道成功发送 215 次。
    判定 1 现在归 `_is_unretryable_send_error()`。
    """
    err_text = _build_send_error_text(exc)
    err_lower = err_text.lower()
    if not err_text:
        return False
    return "kickedoffline" in err_lower or "登录已失效" in err_text


def _is_payload_send_error(exc: Exception) -> bool:
    """仅这类错误才值得做“去 reply / 纯文本”回退。"""
    err_text = _build_send_error_text(exc)
    err_lower = err_text.lower()
    if not err_text:
        return False
    payload_cues = (
        "invalid message",
        "invalid segment",
        "unsupported segment",
        "segment format",
        "bad request",
        "参数错误",
        "消息格式",
        "消息段",
        "cq code",
        "illegal message",
    )
    return any(cue in err_lower for cue in payload_cues)


def _find_ffmpeg_bin() -> str:
    """查找 ffmpeg，委托给 core.tools_types 的去重实现。"""
    from core.tools_types import _find_ffmpeg
    return _find_ffmpeg()


_FFMPEG_BIN = _find_ffmpeg_bin()


def _generate_video_thumbnail_sync(video_path: Path) -> Path | None:
    """用 ffmpeg 为视频生成缩略图，返回 jpg 路径或 None。"""
    if not _FFMPEG_BIN:
        _log.warning("thumbnail_skip | no ffmpeg | video=%s", video_path.name)
        return None
    thumb_path = video_path.with_suffix(".thumb.jpg")
    if thumb_path.exists() and thumb_path.stat().st_size > 1000:
        return thumb_path
    cmd = [
        _FFMPEG_BIN, "-y",
        "-ss", "1",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "5",
        str(thumb_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
        if proc.returncode == 0 and thumb_path.exists() and thumb_path.stat().st_size > 500:
            _log.info("thumbnail_ok | %s | %d bytes", thumb_path.name, thumb_path.stat().st_size)
            return thumb_path
        stderr_text = (proc.stderr or b"").decode("utf-8", errors="ignore")[:300]
        _log.warning("thumbnail_fail | video=%s | rc=%d | stderr=%s", video_path.name, proc.returncode, stderr_text)
    except Exception as exc:
        _log.warning("thumbnail_error | video=%s | %s", video_path.name, exc)
    return None


async def _generate_video_thumbnail(video_path: Path) -> Path | None:
    return await asyncio.to_thread(_generate_video_thumbnail_sync, video_path)


def _probe_audio_duration_seconds_sync(audio_path: Path) -> float:
    """探测音频时长（秒），失败返回 0。"""
    if not audio_path.exists() or not audio_path.is_file():
        return 0.0
    if _FFPROBE_BIN:
        cmd = [
            _FFPROBE_BIN,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(audio_path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            if proc.returncode == 0:
                payload = json.loads(proc.stdout or "{}")
                duration = float(payload.get("format", {}).get("duration", 0) or 0)
                if duration > 0:
                    return duration
        except Exception:
            pass
    if _FFMPEG_BIN:
        cmd = [_FFMPEG_BIN, "-hide_banner", "-i", str(audio_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            text = (proc.stderr or "") + "\n" + (proc.stdout or "")
            m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)
            if m:
                hh = int(m.group(1))
                mm = int(m.group(2))
                ss = float(m.group(3))
                return hh * 3600 + mm * 60 + ss
        except Exception:
            pass
    return 0.0


def _prepare_voice_audio_file_sync(audio_path: Path, max_seconds: int) -> tuple[Path, float, bool]:
    """发送前把语音素材裁到可控长度，降低 rich media 上传失败概率。"""
    duration = _probe_audio_duration_seconds_sync(audio_path)
    if max_seconds <= 0:
        return audio_path, duration, False
    if duration <= 0 or duration <= float(max_seconds) + 0.8:
        return audio_path, duration, False
    if not _FFMPEG_BIN:
        return audio_path, duration, False
    if audio_path.suffix.lower() in {".silk"}:
        return audio_path, duration, False

    trimmed = audio_path.with_name(f"{audio_path.stem}.voice{max_seconds}s.mp3")
    try:
        if (
            trimmed.exists()
            and trimmed.stat().st_size > 1024
            and trimmed.stat().st_mtime >= audio_path.stat().st_mtime
        ):
            return trimmed, duration, True
    except Exception:
        pass

    cmd = [
        _FFMPEG_BIN,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(audio_path),
        "-t",
        str(max_seconds),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "24000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(trimmed),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if proc.returncode == 0 and trimmed.exists() and trimmed.stat().st_size > 1024:
            return trimmed, duration, True
    except Exception:
        pass

    trimmed.unlink(missing_ok=True)
    return audio_path, duration, False


async def _prepare_voice_audio_file(audio_path: Path, max_seconds: int) -> tuple[Path, float, bool]:
    return await asyncio.to_thread(_prepare_voice_audio_file_sync, audio_path, max_seconds)


def _build_file_uri(path_like: Path | str) -> str:
    return build_napcat_file_reference(path_like)


def _noop_send_failure_mark(group_id: int, bot_id: str, reason: str) -> None:
    """发送失败标记的 no-op 注入（架构收敛任务 B3）。

    统一发送核心 `build_send_guard` 在 sender 失败时会调用 mark_failure_fn；
    NoneBot 路径的熔断/暂停标记已经由 `_safe_send` 内部完成（按错误分类决定
    是否停群/停 bot），这里显式 no-op，避免对「结果未知/不可重试」这类
    本就不该停发的情况额外加 180s 群熔断 + 120s bot 暂停（行为等价保留）。
    """


def _silk_duration_seconds_sync(path: Path) -> float:
    """探测 silk 时长（秒）。ffprobe/ffmpeg 读不出 silk 时长（返回 0），
    优先用 pilk.get_duration（毫秒），失败才回退 ffprobe。"""
    try:
        import pilk
        return float(pilk.get_duration(str(path))) / 1000.0
    except Exception:
        pass
    try:
        return _probe_audio_duration_seconds_sync(path)
    except Exception:
        return 0.0


def _silk_encode_for_record_sync(audio_path: Path, max_seconds: int) -> Path | None:
    """把音频转成 QQ 可播放的 silk，并截断到 max_seconds。

    QQ 语音消息必须是 silk 编码（NapCat 对 mp3 record 不保证自动转码，
    实际点歌直发 mp3 在 QQ 端点开无法播放）。这里用 ffmpeg 解出 PCM，
    再用 pilk/pysilk 编码成 silk。输入已是合规 silk 时直接复用。
    返回 None 表示转换失败（调用方回退原文件）。
    """
    try:
        if audio_path is None or not audio_path.exists() or not audio_path.is_file():
            return None
        if not _FFMPEG_BIN:
            return None
        if audio_path.suffix.lower() == ".silk":
            if max_seconds <= 0:
                return audio_path
            try:
                dur = _silk_duration_seconds_sync(audio_path)
                if dur > 0 and dur <= max_seconds + 0.8:
                    return audio_path
            except Exception:
                pass
            # 超长 silk：尝试 ffmpeg 解码重转（silk 解码可能不支持，失败则回退原文件）。
        try:
            import pilk
            encoder = pilk
        except ImportError:
            try:
                import pysilk as encoder  # type: ignore[no-redef]
            except ImportError:
                _log.warning("voice_silk_encode_skip | no pilk/pysilk")
                return None
        silk_path = audio_path.with_suffix(".silk")
        # 已有合规的 silk（如 music 层生成过）直接复用，避免重复编码。
        if silk_path.exists() and silk_path.stat().st_size >= 256:
            try:
                dur = _silk_duration_seconds_sync(silk_path)
                if max_seconds <= 0 or (dur > 0 and dur <= max_seconds + 0.8):
                    return silk_path
            except Exception:
                pass
        # 中间文件用唯一临时名 + os.replace 原子落盘：同一音频并发编码（不同群同歌）
        # 不再互相覆盖/unlink（旧固定路径 .silk/.silk_src.pcm 会被并发线程写坏，导致回退 mp3）。
        uid = uuid4().hex
        tmp_silk = audio_path.with_name(f".{audio_path.stem}.{uid}.silk")
        tmp_pcm = audio_path.with_name(f".{audio_path.stem}.{uid}.silk_src.pcm")
        try:
            cmd = [
                _FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "warning",
                "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "24000", "-f", "s16le",
            ]
            if max_seconds > 0:
                cmd += ["-t", str(max_seconds)]
            cmd.append(str(tmp_pcm))
            proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
            if proc.returncode != 0 or not tmp_pcm.exists() or tmp_pcm.stat().st_size < 256:
                return None
            try:
                # pilk 0.2.4 签名: encode(pcm, silk, pcm_rate, silk_rate, tencent)。
                # silk_rate 也传 24000 且 tencent=True 才产出 QQ 可用的 tencent 头 silk；
                # (24000, True) 会把 True 落到 silk_rate，产出非 tencent 8kHz 低质文件。
                encoder.encode(str(tmp_pcm), str(tmp_silk), 24000, 24000, True)
            except TypeError:
                try:
                    encoder.encode(str(tmp_pcm), str(tmp_silk), sample_rate=24000, tencent=True)
                except TypeError:
                    try:
                        encoder.encode(str(tmp_pcm), str(tmp_silk), 24000, True)
                    except TypeError:
                        with tmp_pcm.open("rb") as src, tmp_silk.open("wb") as dst:
                            encoder.encode(src, dst, 24000, 24000, True)
            if not tmp_silk.exists() or tmp_silk.stat().st_size < 256:
                return None
            # 原子落盘到稳定 silk_path，供后续调用/其他层（music）复用。
            os.replace(tmp_silk, silk_path)
            _log.info(
                "voice_silk_encode_ok | src=%s | silk=%s | size=%d",
                audio_path.name,
                silk_path.name,
                silk_path.stat().st_size,
            )
            return silk_path
        finally:
            tmp_pcm.unlink(missing_ok=True)
            tmp_silk.unlink(missing_ok=True)
    except Exception as exc:
        _log.warning("voice_silk_encode_fail | %s", exc)
        return None


async def _silk_encode_for_record(audio_path: Path, max_seconds: int) -> Path | None:
    return await asyncio.to_thread(_silk_encode_for_record_sync, audio_path, max_seconds)


def _split_voice_audio_file_sync(audio_path: Path, segment_seconds: int, max_segments: int) -> list[Path]:
    """把长音频切成多个小段，供 record 分段发送。"""
    if segment_seconds <= 0 or max_segments <= 0:
        return []
    if not audio_path.exists() or not audio_path.is_file():
        return []
    if not _FFMPEG_BIN:
        return []
    if audio_path.suffix.lower() in {".silk"}:
        return []

    parts_dir = audio_path.with_name(f"{audio_path.stem}.parts_{segment_seconds}s")
    parts_pattern = f"{audio_path.stem}.part*.mp3"
    try:
        parts_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []

    existing = sorted(parts_dir.glob(parts_pattern))
    try:
        if existing and all(p.stat().st_size > 1024 for p in existing):
            src_mtime = audio_path.stat().st_mtime
            newest_part_mtime = max(p.stat().st_mtime for p in existing)
            if newest_part_mtime >= src_mtime:
                return existing[:max_segments]
    except Exception:
        pass

    for old in parts_dir.glob(parts_pattern):
        old.unlink(missing_ok=True)

    out_tpl = parts_dir / f"{audio_path.stem}.part%03d.mp3"
    cmd = [
        _FFMPEG_BIN,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "24000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        "-f",
        "segment",
        "-segment_time",
        str(int(segment_seconds)),
        str(out_tpl),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
        if proc.returncode != 0:
            return []
    except Exception:
        return []

    parts = [p for p in sorted(parts_dir.glob(parts_pattern)) if p.stat().st_size > 1024]
    return parts[:max_segments]


async def _split_voice_audio_file(audio_path: Path, segment_seconds: int, max_segments: int) -> list[Path]:
    return await asyncio.to_thread(_split_voice_audio_file_sync, audio_path, segment_seconds, max_segments)


# ── 队列取消通知：按会话去重，不按 pending 一刀切 ──
# 原实现在 pending_count > 0 时直接静默。那个抑制条件恰好等于超时的高发条件：
# 群越活跃 pending 越大，被 @ 的人越容易被静默，而群活跃正是超时最容易发生的时候。
# 实测 trace=118886-25：用户明确 @ 了机器人并发了语音，trigger 判 directed=True、
# queue_cancel_policy high_priority=True reply_to_bot=True，结果 pending=5 命中静默，
# 从入队到取消 120 秒全程零输出 —— 用户视角是「机器人死了」，日志里却是「故意不说」。
# 现在的判据只有结构事实：取消原因 / 这一回合是否被直接指向 / 同会话上一条通知过去了多久。
_QUEUE_CANCEL_NOTICE_LAST_AT: dict[str, float] = {}
_QUEUE_CANCEL_NOTICE_DEDUP_DEFAULT_SECONDS = 45.0
_QUEUE_CANCEL_NOTICE_STATE_MAX_ENTRIES = 2000
_QUEUE_CANCEL_NOTICE_REASONS = frozenset(
    {
        "process_timeout",
        "process_error",
        # TTL 过期的取消同样是「用户提了要求但什么都没收到」，真实群里出现过 13 次。
        "message_ttl_expired",
    }
)
_QUEUE_CANCEL_NOTICE_TEXTS = {
    "process_timeout": "这条任务处理超时了，我先停下。你可以重试，或把问题拆成更短一步。",
    "process_error": "这条任务执行失败了，我先停下。你可以重试一次。",
    "message_ttl_expired": "这条消息排队太久已经过期，我没来得及处理。你可以再发一次。",
}
_QUEUE_CANCEL_NOTICE_FALLBACK_TEXT = "这条任务没能完成，我先停下。你可以重试一次。"


def _queue_cancel_notice_text(reason: str) -> str:
    return _QUEUE_CANCEL_NOTICE_TEXTS.get(str(reason), _QUEUE_CANCEL_NOTICE_FALLBACK_TEXT)


def _should_send_queue_cancel_notice(
    *,
    conversation_id: str,
    reason: str,
    directed: bool,
    dedup_window_seconds: Any = None,
    now: float | None = None,
) -> tuple[bool, str]:
    """队列取消要不要给用户回一句。返回 (是否发送, 判定依据)。

    `directed` 是结构事实（被 @ / 私聊 / 回复机器人），不是词义判断。
    刻意不看 pending_count：队列里别的任务有多少，与当前这个用户是否需要反馈无关。
    """

    if str(reason) not in _QUEUE_CANCEL_NOTICE_REASONS:
        return False, "reason_not_notifiable"

    stamp = time.monotonic() if now is None else float(now)
    key = str(conversation_id or "-")

    if directed:
        # 被直接指向的回合：pending 再多也要出声 —— pending 大恰恰是最需要反馈的时候。
        _QUEUE_CANCEL_NOTICE_LAST_AT[key] = stamp
        return True, "directed_turn"

    try:
        window = float(dedup_window_seconds)
    except (TypeError, ValueError):
        window = _QUEUE_CANCEL_NOTICE_DEDUP_DEFAULT_SECONDS
    if window < 0:
        window = _QUEUE_CANCEL_NOTICE_DEDUP_DEFAULT_SECONDS

    last_at = _QUEUE_CANCEL_NOTICE_LAST_AT.get(key)
    if window > 0 and isinstance(last_at, (int, float)) and stamp - float(last_at) < window:
        return False, "dedup_window"

    _QUEUE_CANCEL_NOTICE_LAST_AT[key] = stamp
    if len(_QUEUE_CANCEL_NOTICE_LAST_AT) > _QUEUE_CANCEL_NOTICE_STATE_MAX_ENTRIES:
        stale = sorted(_QUEUE_CANCEL_NOTICE_LAST_AT, key=_QUEUE_CANCEL_NOTICE_LAST_AT.get)
        for old_key in stale[: len(_QUEUE_CANCEL_NOTICE_LAST_AT) // 2]:
            _QUEUE_CANCEL_NOTICE_LAST_AT.pop(old_key, None)
    return True, "dedup_window_elapsed"


def create_engine() -> YukikoEngine:
    root = Path(__file__).resolve().parent
    return YukikoEngine.from_default_paths(project_root=root)


def _normalize_send_media_fields(
    result: Any,
) -> tuple[str, bool, str, str, list[str], str, str, str, str]:
    """把发送结果里媒体相关字段统一成字符串/list 形式，并做 image_urls 去重对齐。"""
    action = str(getattr(result, "action", "") or "")
    is_music_voice_action = action in {"music_play", "music_play_by_id", "bilibili_audio_extract"}
    reply_text = _normalize_reply_text(str(getattr(result, "reply_text", "") or ""))
    image_url = str(getattr(result, "image_url", "") or "")
    raw_image_urls = getattr(result, "image_urls", []) or []
    image_urls: list[str] = []
    if isinstance(raw_image_urls, list):
        image_urls = [
            normalize_text(str(item))
            for item in raw_image_urls
            if normalize_text(str(item))
        ]
    if image_url and image_url not in image_urls:
        image_urls.insert(0, image_url)
    if image_urls and not image_url:
        image_url = image_urls[0]
    video_url = str(getattr(result, "video_url", "") or "")
    cover_url = str(getattr(result, "cover_url", "") or "")
    record_b64 = str(getattr(result, "record_b64", "") or "")
    audio_file = str(getattr(result, "audio_file", "") or "")
    return action, is_music_voice_action, reply_text, image_url, image_urls, video_url, cover_url, record_b64, audio_file


def _classify_stale_send(
    latest_ctx: dict | None,
    latest_trace: str,
    payload: Any,
    action: str,
    reply_text: str,
    image_urls: list[str],
    video_url: str,
    cover_url: str,
    record_b64: str,
    audio_file: str,
) -> tuple[int, bool, bool, bool, bool]:
    """判断本次发送是否是被更新的回合打断（纯计算，无副作用）。"""
    latest_seq = 0
    if isinstance(latest_ctx, dict):
        try:
            latest_seq = int(latest_ctx.get("seq", 0) or 0)
        except (TypeError, ValueError):
            latest_seq = 0
    payload_seq = int(payload.seq or 0)
    latest_user_id = (
        normalize_text(str(latest_ctx.get("user_id", "")))
        if isinstance(latest_ctx, dict)
        else ""
    )
    latest_text = (
        normalize_text(str(latest_ctx.get("text", "")))
        if isinstance(latest_ctx, dict)
        else ""
    )
    stale_trace = bool(
        latest_trace
        and latest_trace != payload.trace_id
        and (not latest_seq or not payload_seq or latest_seq > payload_seq)
    )
    same_user_newer_turn = bool(
        stale_trace
        and latest_user_id
        and latest_user_id == normalize_text(str(payload.user_id))
    )
    cancel_newer_turn = bool(stale_trace and _looks_like_cancel_previous_request(latest_text))
    stale_plain_reply = (
        stale_trace
        and action == "reply"
        and not payload.is_private
        and reply_text
        and not image_urls
        and not video_url
        and not cover_url
        and not record_b64
        and not audio_file
    )
    return latest_seq, stale_trace, same_user_newer_turn, cancel_newer_turn, stale_plain_reply


def _plan_reply_text_chunks(
    reply_text: str,
    video_url: str,
    action: str,
    multi_reply_enable: bool,
    multi_reply_max_lines: int,
    multi_reply_max_chars: int,
    multi_reply_max_chunks: int,
    multi_reply_chat_max_lines: int,
    multi_reply_chat_max_chars: int,
    multi_reply_chat_max_chunks: int,
    video_analysis_requested: bool,
    chat_split_mode: str,
    send_rate_enable: bool,
    send_rate_max_per_window: int,
    send_rate_window_seconds: int,
    send_rate_warn_threshold: int,
    conversation_id: str,
    group_id: int,
) -> tuple[list[str], bool, int]:
    """把 reply_text 切成发送用 chunk：拆分 → 限流合并 → 超长重平衡。"""
    text_chunks: list[str] = []
    if reply_text:
        # 视频场景默认单条文本，避免“昵称 + 逗号开头下一条”的断裂体验
        if video_url:
            text_chunks = [reply_text]
        elif multi_reply_enable:
            chunk_max_lines = multi_reply_max_lines
            chunk_max_chars = multi_reply_max_chars
            chunk_max_count = multi_reply_max_chunks
            if action == "reply":
                chunk_max_lines = multi_reply_chat_max_lines
                chunk_max_chars = multi_reply_chat_max_chars
                chunk_max_count = multi_reply_chat_max_chunks
            if action == "search":
                chunk_max_lines = max(chunk_max_lines, 4)
                chunk_max_chars = max(chunk_max_chars, 260)
                chunk_max_count = max(chunk_max_count, 6)
            if video_analysis_requested:
                chunk_max_lines = max(chunk_max_lines, 6)
                chunk_max_chars = max(chunk_max_chars, 360)
                chunk_max_count = max(chunk_max_count, 8)
            if chat_split_mode == "semantic":
                text_chunks = split_semantic_text(
                    reply_text,
                    max_lines=chunk_max_lines,
                    max_chars=chunk_max_chars,
                    max_chunks=chunk_max_count,
                )
            else:
                text_chunks = _split_reply_chunks(
                    reply_text,
                    max_lines=chunk_max_lines,
                    max_chars=chunk_max_chars,
                    max_chunks=chunk_max_count,
                )
        if not text_chunks:
            text_chunks = [reply_text]

    rate_limited = False
    if text_chunks and send_rate_enable:
        bucket = _get_send_bucket(
            conversation_id=conversation_id,
            group_id=group_id,
            max_per_window=send_rate_max_per_window,
            refill_seconds=send_rate_window_seconds,
            warn_threshold=send_rate_warn_threshold,
        )
        if bucket.near_warn():
            text_chunks = coalesce_for_rate_limit(
                text_chunks,
                max_chars=max(220, multi_reply_max_chars + 80),
                short_chunk_chars=90,
            )
            rate_limited = True
    if text_chunks:
        base_chunk_chars = (
            multi_reply_chat_max_chars if action == "reply" else multi_reply_max_chars
        )
        safe_chunk_chars = max(240, min(920, base_chunk_chars + 140))
        text_chunks = _rebalance_text_chunks_for_send(
            text_chunks,
            max_chars=safe_chunk_chars,
        )
        if not text_chunks and reply_text:
            text_chunks = [reply_text]

    chunk_count = len(text_chunks)
    return text_chunks, rate_limited, chunk_count


def register_handlers(engine: YukikoEngine) -> None:
    dispatcher = GroupQueueDispatcher(engine.config.get("queue", {}))
    _latest_queue_task_ctx: dict[str, dict[str, Any]] = {}
    _platform_manager: Any = None
    _RUNTIME_WEBUI_BRIDGE["queue"] = dispatcher
    _RUNTIME_WEBUI_BRIDGE["latest_ctx"] = _latest_queue_task_ctx
    # 暴露给 WebUI: 运行中会话状态 + 主动中断能力
    setattr(engine, "runtime_agent_state_provider", get_runtime_agent_states)
    setattr(engine, "runtime_agent_interrupt", interrupt_runtime_conversation)
    router = on_message(priority=90, block=False)
    meta_router = on_metaevent(priority=90, block=False)
    notice_router = on_notice(priority=90, block=False)
    request_router = on_request(priority=90, block=False)
    def _normalize_trigger_guard_flags(
        bot_cfg_any: dict[str, Any],
        trigger_cfg_any: dict[str, Any],
        control_cfg_any: dict[str, Any],
    ) -> tuple[bool, bool, bool, str]:
        allow_non_to_me_defined = "allow_non_to_me" in bot_cfg_any
        ai_listen_defined = "ai_listen_enable" in trigger_cfg_any
        delegate_undirected_defined = "delegate_undirected_to_ai" in trigger_cfg_any

        allow_non_to_me_flag = bool(bot_cfg_any.get("allow_non_to_me", False))
        ai_listen_enable_flag = bool(trigger_cfg_any.get("ai_listen_enable", False))
        explicit_ai_listen_on = ai_listen_defined and ai_listen_enable_flag
        delegate_undirected_flag = bool(trigger_cfg_any.get("delegate_undirected_to_ai", False))
        policy = (
            normalize_text(str(control_cfg_any.get("undirected_policy", ""))).lower()
            or "high_confidence_only"
        )

        if policy in {"off", "disabled"}:
            return False, False, False, policy
        if policy in {"mention_only", "directed_only"}:
            if explicit_ai_listen_on:
                # 显式开启旁听时自动提升为高置信模式，避免“开关已开但入口硬拦截”。
                policy = "high_confidence_only"
            else:
                return False, False, False, policy

        if policy == "high_confidence_only":
            # 仅在字段缺省时才注入策略默认值；显式配置优先，避免“关不掉旁听”。
            if not allow_non_to_me_defined:
                allow_non_to_me_flag = True
            if not ai_listen_defined:
                ai_listen_enable_flag = allow_non_to_me_flag
            if not delegate_undirected_defined:
                delegate_undirected_flag = False

        if explicit_ai_listen_on and policy not in {"off", "disabled"}:
            allow_non_to_me_flag = True
            ai_listen_enable_flag = True

        if not allow_non_to_me_flag:
            ai_listen_enable_flag = False
            delegate_undirected_flag = False

        return allow_non_to_me_flag, ai_listen_enable_flag, delegate_undirected_flag, policy

    bot_cfg = engine.config.get("bot", {}) if isinstance(engine.config, dict) else {}
    trigger_cfg = engine.config.get("trigger", {}) if isinstance(engine.config, dict) else {}
    control_cfg = engine.config.get("control", {}) if isinstance(engine.config, dict) else {}
    (
        allow_non_to_me,
        ai_listen_enable_effective,
        delegate_undirected_effective,
        undirected_policy,
    ) = _normalize_trigger_guard_flags(
        bot_cfg if isinstance(bot_cfg, dict) else {},
        trigger_cfg if isinstance(trigger_cfg, dict) else {},
        control_cfg if isinstance(control_cfg, dict) else {},
    )
    _log.info(
        "trigger_guard_effective | allow_non_to_me=%s | ai_listen_enable=%s | delegate_undirected_to_ai=%s | undirected_policy=%s",
        allow_non_to_me,
        ai_listen_enable_effective,
        delegate_undirected_effective,
        undirected_policy or "-",
    )
    _trigger_guard_runtime_snapshot = ""

    def _resolve_runtime_matcher_flags() -> tuple[bool, dict[str, Any], dict[str, Any], dict[str, Any]]:
        nonlocal _trigger_guard_runtime_snapshot
        cfg = engine.config if isinstance(engine.config, dict) else {}
        bot_cfg_rt = cfg.get("bot", {}) if isinstance(cfg.get("bot"), dict) else {}
        trigger_cfg_rt = cfg.get("trigger", {}) if isinstance(cfg.get("trigger"), dict) else {}
        control_cfg_rt = cfg.get("control", {}) if isinstance(cfg.get("control"), dict) else {}

        (
            allow_non_to_me_rt,
            ai_listen_enable_rt,
            delegate_undirected_rt,
            undirected_policy_rt,
        ) = _normalize_trigger_guard_flags(
            bot_cfg_rt,
            trigger_cfg_rt,
            control_cfg_rt,
        )

        guard_snapshot = (
            f"{int(allow_non_to_me_rt)}|"
            f"{int(ai_listen_enable_rt)}|"
            f"{int(delegate_undirected_rt)}|"
            f"{undirected_policy_rt}"
        )
        if guard_snapshot != _trigger_guard_runtime_snapshot:
            _trigger_guard_runtime_snapshot = guard_snapshot
            _log.info(
                "trigger_guard_runtime | allow_non_to_me=%s | ai_listen_enable=%s | delegate_undirected_to_ai=%s | undirected_policy=%s",
                allow_non_to_me_rt,
                ai_listen_enable_rt,
                delegate_undirected_rt,
                undirected_policy_rt or "-",
            )
        return allow_non_to_me_rt, bot_cfg_rt, trigger_cfg_rt, control_cfg_rt

    def _starts_with_bot_alias(text: str, bot_cfg_any: dict[str, Any]) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        aliases: list[str] = []
        for item in bot_cfg_any.get("nicknames", []) if isinstance(bot_cfg_any.get("nicknames"), list) else []:
            alias = normalize_text(str(item)).lower()
            if alias:
                aliases.append(alias)
        bot_name = normalize_text(str(bot_cfg_any.get("name", ""))).lower()
        if bot_name:
            aliases.append(bot_name)
        if not aliases:
            return False
        aliases = sorted(set(aliases), key=len, reverse=True)
        return any(content.startswith(alias) for alias in aliases)

    def _looks_like_explicit_user_command(text: str) -> bool:
        content = normalize_text(text).strip()
        if not content:
            return False
        # 显式命令（如 /点歌、/搜图）在 mention-only 模式下也应进入处理。
        return bool(re.match(r"^[/／][^\s]{1,64}", content))

    def _parse_private_chat_whitelist(raw: Any) -> set[str]:
        values: list[str] = []
        if isinstance(raw, list):
            values = [normalize_text(str(item)) for item in raw]
        elif isinstance(raw, str):
            values = [normalize_text(item) for item in re.split(r"[\n,，;；\s]+", raw)]
        return {item for item in values if item}

    def _allow_private_chat_for_user(bot_cfg_any: dict[str, Any], user_id: str) -> bool:
        mode = normalize_text(str(bot_cfg_any.get("private_chat_mode", "off"))).lower() or "off"
        if mode == "all":
            return True
        if mode == "whitelist":
            return normalize_text(user_id) in _parse_private_chat_whitelist(
                bot_cfg_any.get("private_chat_whitelist", [])
            )
        return False

    def _resolve_admin_action(text: str) -> str:
        raw = normalize_text(text).strip()
        if not raw:
            return ""
        parts = raw.split(maxsplit=2)
        first = parts[0].lower()
        top_map = getattr(engine.admin, "_TOP", {})
        if isinstance(top_map, dict) and first in top_map:
            return normalize_text(str(top_map.get(first, ""))).lower()

        if first not in {"/yuki", "/yuki帮助"}:
            return ""
        sub = normalize_text(parts[1]).lower() if len(parts) > 1 else "help"
        if first == "/yuki帮助":
            sub = "help"
        sub_map = getattr(engine.admin, "_SUB", {})
        action = sub_map.get(sub) if isinstance(sub_map, dict) else ""
        if not action:
            fuzzy = getattr(engine.admin, "_fuzzy_match_command", None)
            if callable(fuzzy):
                try:
                    action = fuzzy(sub)
                except Exception:
                    action = ""
        return normalize_text(str(action or "")).lower()

    def _is_group_admin_sender(*, user_id: str, group_id: int, sender_role: str) -> bool:
        if engine.admin.is_super_admin(user_id):
            return True
        role = normalize_text(sender_role).lower()
        if group_id <= 0:
            return False
        if role not in {"owner", "admin"}:
            return False
        return bool(engine.admin.is_group_whitelisted(group_id))

    # ── 消息去重：同一用户短时间内发送完全相同的消息只处理一次 ──
    _recent_msg_hashes: dict[str, tuple[str, float]] = {}  # key=conv:uid → (hash, timestamp)
    _DEDUP_WINDOW_SECONDS = 5.0
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    _LOGIN_BACKLOG_STATE_FILE = Path(__file__).resolve().parent / "storage" / "runtime" / "login_backlog_state.json"
    _login_backlog_lock = asyncio.Lock()
    _login_backlog_last_run = 0.0

    def _unwrap_onebot_payload(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload and ("retcode" in payload or "status" in payload):
            return payload.get("data")
        return payload

    def _load_login_backlog_state() -> dict[str, Any]:
        try:
            if not _LOGIN_BACKLOG_STATE_FILE.exists():
                return {}
            parsed = json.loads(_LOGIN_BACKLOG_STATE_FILE.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _save_login_backlog_state(state: dict[str, Any]) -> None:
        try:
            _LOGIN_BACKLOG_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _LOGIN_BACKLOG_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(_LOGIN_BACKLOG_STATE_FILE)
        except Exception as exc:
            _log.debug("login_backlog_state_save_failed | %s", exc)

    def _resolve_login_backlog_options() -> dict[str, Any]:
        cfg = engine.config if isinstance(engine.config, dict) else {}
        control = cfg.get("control", {})
        if not isinstance(control, dict):
            control = {}
        return {
            "enable": bool(control.get("login_backlog_import_enable", False)),
            "llm_summary_enable": bool(control.get("login_backlog_llm_summary_enable", False)),
            "include_private": bool(control.get("login_backlog_import_include_private", True)),
            "only_unread": bool(control.get("login_backlog_import_only_unread", True)),
            "max_conversations": max(1, _as_int(control.get("login_backlog_import_max_conversations", 30), 30)),
            "max_messages_per_conversation": max(
                1,
                _as_int(control.get("login_backlog_import_max_messages_per_conversation", 40), 40),
            ),
            "max_pages_per_conversation": max(
                1,
                _as_int(control.get("login_backlog_import_max_pages_per_conversation", 3), 3),
            ),
            "lookback_hours": max(1, _as_int(control.get("login_backlog_import_lookback_hours", 72), 72)),
            "min_interval_seconds": max(10, _as_int(control.get("login_backlog_import_min_interval_seconds", 20), 20)),
        }

    def _render_history_message_text(raw_message: Any, segments: Any) -> str:
        text = normalize_text(str(raw_message))
        if text:
            return text
        if not isinstance(segments, list):
            return ""
        parts: list[str] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = normalize_text(str(seg.get("type", ""))).lower()
            data = seg.get("data", {}) or {}
            if seg_type == "text":
                part = normalize_text(str(data.get("text", "")))
                if part:
                    parts.append(part)
            elif seg_type in {"image", "video", "record", "audio", "file"}:
                parts.append(f"[{seg_type}]")
            elif seg_type == "at":
                qq = normalize_text(str(data.get("qq", "")))
                parts.append(f"@{qq or 'someone'}")
            elif seg_type:
                parts.append(f"[{seg_type}]")
        return normalize_text(" ".join(parts))

    def _extract_history_rows(payload: Any) -> list[dict[str, Any]]:
        body = _unwrap_onebot_payload(payload)
        rows = body.get("messages", []) if isinstance(body, dict) else body
        if isinstance(body, dict) and not rows:
            rows = body.get("items", [])
        if not isinstance(rows, list):
            return []
        return [item for item in rows if isinstance(item, dict)]

    def _safe_int_or_zero(raw: Any) -> int:
        try:
            return int(raw)
        except Exception:
            return 0

    async def _fetch_contact_history(
        bot: Bot,
        *,
        chat_type: str,
        peer_id: str,
        max_messages: int,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        next_seq: int | None = None
        page_limit = max(1, int(max_pages))
        per_page = max(1, min(80, int(max_messages)))
        for _ in range(page_limit):
            kwargs: dict[str, Any] = {}
            try:
                peer_num = int(peer_id)
            except Exception:
                break
            if chat_type == "group":
                kwargs["group_id"] = peer_num
            else:
                kwargs["user_id"] = peer_num
                kwargs["count"] = per_page
            if next_seq is not None and next_seq > 0:
                kwargs["message_seq"] = int(next_seq)

            api_name = "get_group_msg_history" if chat_type == "group" else "get_friend_msg_history"
            try:
                payload = await call_napcat_bot_api(bot, api_name, **kwargs)
            except Exception as exc:
                _log.debug("login_backlog_fetch_history_failed | api=%s | peer=%s | err=%s", api_name, peer_id, exc)
                break
            page_rows = _extract_history_rows(payload)
            if not page_rows:
                break

            min_seq = 0
            new_count = 0
            for row in page_rows:
                message_id = normalize_text(
                    str(row.get("message_id", "") or row.get("real_id", "") or row.get("id", ""))
                )
                seq = normalize_text(str(row.get("message_seq", "") or row.get("real_seq", "")))
                sender = row.get("sender", {}) if isinstance(row.get("sender"), dict) else {}
                sender_id = normalize_text(str(sender.get("user_id", "")))
                ts = _safe_int_or_zero(row.get("time", 0))
                dedup_key = f"{message_id}|{seq}|{sender_id}|{ts}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                rows.append(row)
                new_count += 1
                seq_value = _safe_int_or_zero(row.get("message_seq", row.get("real_seq", 0)))
                if seq_value > 0 and (min_seq <= 0 or seq_value < min_seq):
                    min_seq = seq_value

            if len(rows) >= max_messages:
                break
            if new_count <= 0 or min_seq <= 1:
                break
            if next_seq is not None and min_seq >= next_seq:
                break
            next_seq = min_seq - 1

        rows.sort(key=lambda item: _safe_int_or_zero(item.get("time", 0)))
        return rows[-max_messages:]

    async def _import_login_backlog_for_bot(bot: Bot, *, reason: str, opts: dict[str, Any]) -> None:
        state = _load_login_backlog_state()
        per_bot = state.get("by_bot", {})
        if not isinstance(per_bot, dict):
            per_bot = {}
        bot_id = normalize_text(str(getattr(bot, "self_id", "")))
        bot_state = per_bot.get(bot_id, {})
        if not isinstance(bot_state, dict):
            bot_state = {}
        conv_state = bot_state.get("conversations", {})
        if not isinstance(conv_state, dict):
            conv_state = {}

        try:
            recent_raw = await call_napcat_bot_api(bot, "get_recent_contact", count=int(opts["max_conversations"]))
        except Exception as exc:
            _log.debug("login_backlog_recent_contact_failed | bot=%s | err=%s", bot_id or "-", exc)
            return

        recent_body = _unwrap_onebot_payload(recent_raw)
        recent_items = recent_body.get("items", []) if isinstance(recent_body, dict) else recent_body
        if not isinstance(recent_items, list):
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())
        fallback_since = now_ts - int(opts["lookback_hours"]) * 3600
        imported_count = 0
        imported_lines: list[str] = []

        for item in recent_items:
            if not isinstance(item, dict):
                continue
            chat_type = "group" if int(item.get("chatType", 0) or 0) == 2 else "private"
            if chat_type == "private" and not opts["include_private"]:
                continue
            unread_count = _safe_int_or_zero(item.get("unreadCnt", 0))
            if opts["only_unread"] and unread_count <= 0:
                continue
            peer_id = normalize_text(str(item.get("peerUin", "") or item.get("peerUid", "") or item.get("peer_id", "")))
            if not peer_id or not peer_id.isdigit():
                continue

            conversation_id = f"{chat_type}:{peer_id}"
            since_ts = max(_safe_int_or_zero(conv_state.get(conversation_id, 0)), fallback_since)
            rows = await _fetch_contact_history(
                bot,
                chat_type=chat_type,
                peer_id=peer_id,
                max_messages=int(opts["max_messages_per_conversation"]),
                max_pages=int(opts["max_pages_per_conversation"]),
            )
            if not rows:
                conv_state[conversation_id] = max(since_ts, _safe_int_or_zero(item.get("msgTime", 0)))
                continue

            newest_ts = since_ts
            for row in rows:
                ts = _safe_int_or_zero(row.get("time", 0))
                if ts <= since_ts:
                    continue
                newest_ts = max(newest_ts, ts)
                sender = row.get("sender", {}) if isinstance(row.get("sender"), dict) else {}
                sender_id = normalize_text(str(sender.get("user_id", "")))
                if sender_id and sender_id == bot_id:
                    continue
                sender_name = (
                    normalize_text(str(sender.get("card", "")))
                    or normalize_text(str(sender.get("nickname", "")))
                    or sender_id
                    or "unknown"
                )
                segments = row.get("message", [])
                if not isinstance(segments, list):
                    segments = []
                content = _render_history_message_text(row.get("raw_message", ""), segments)
                if not content:
                    continue
                try:
                    engine.memory.add_message(
                        conversation_id=conversation_id,
                        user_id=sender_id or f"peer:{peer_id}",
                        role="user",
                        content=content,
                        timestamp=datetime.fromtimestamp(ts if ts > 0 else now_ts, tz=timezone.utc),
                        user_name=sender_name,
                    )
                    imported_count += 1
                    imported_lines.append(f"[{conversation_id}] {sender_name}: {clip_text(content, 120)}")
                except Exception as exc:
                    _log.debug(
                        "login_backlog_add_memory_failed | conv=%s | sender=%s | err=%s",
                        conversation_id,
                        sender_id or "-",
                        exc,
                    )
            conv_state[conversation_id] = max(_safe_int_or_zero(conv_state.get(conversation_id, 0)), newest_ts)

        bot_state["conversations"] = conv_state
        bot_state["last_sync_ts"] = now_ts
        per_bot[bot_id] = bot_state
        state["by_bot"] = per_bot
        _save_login_backlog_state(state)

        if imported_count <= 0:
            _log.info("login_backlog_import | bot=%s | reason=%s | imported=0", bot_id or "-", reason)
            return

        if opts["llm_summary_enable"]:
            try:
                sample_lines = imported_lines[-80:]
                summary_prompt = "\n".join(sample_lines)
                summary = normalize_text(
                    await engine.model_client.chat_text(
                        messages=[
                            {
                                "role": "system",
                                "content": "你是离线聊天归档助手。请把输入聊天记录提炼成简短中文要点，不要虚构，不要给建议。",
                            },
                            {
                                "role": "user",
                                "content": (
                                    "请总结这批离线期间的新消息（最多6条要点，尽量提取人名/偏好/计划/待办）：\n"
                                    f"{summary_prompt}"
                                ),
                            },
                        ],
                        max_tokens=320,
                    )
                )
                if summary:
                    engine.memory.add_message(
                        conversation_id=f"system:login_backlog:{bot_id or 'default'}",
                        user_id=bot_id or "system",
                        role="system",
                        content=f"离线消息摘要（{reason}）：{summary}",
                        timestamp=datetime.now(timezone.utc),
                        user_name="system",
                    )
            except Exception as exc:
                _log.debug("login_backlog_summary_failed | bot=%s | err=%s", bot_id or "-", exc)

        _log.info(
            "login_backlog_import | bot=%s | reason=%s | imported=%d",
            bot_id or "-",
            reason,
            imported_count,
        )

    async def _run_login_backlog_import(reason: str, bot: Bot | None = None) -> None:
        nonlocal _login_backlog_last_run
        opts = _resolve_login_backlog_options()
        if not opts["enable"]:
            return
        now_mono = time.monotonic()
        if now_mono - _login_backlog_last_run < float(opts["min_interval_seconds"]):
            return
        if _login_backlog_lock.locked():
            return
        async with _login_backlog_lock:
            now_mono = time.monotonic()
            if now_mono - _login_backlog_last_run < float(opts["min_interval_seconds"]):
                return
            _login_backlog_last_run = now_mono

            targets: list[Bot] = []
            if bot is not None:
                targets = [bot]
            else:
                bots_map = nonebot.get_bots()
                targets = [item for item in bots_map.values() if isinstance(item, Bot)]
            for target in targets:
                try:
                    await _import_login_backlog_for_bot(target, reason=reason, opts=opts)
                except Exception as exc:
                    _log.warning(
                        "login_backlog_import_failed | bot=%s | reason=%s | err=%s",
                        normalize_text(str(getattr(target, "self_id", ""))) or "-",
                        reason,
                        exc,
                    )

    def _resolve_runtime_send_options() -> dict[str, Any]:
        cfg = engine.config if isinstance(engine.config, dict) else {}
        bot_cfg_rt = cfg.get("bot", {})
        if not isinstance(bot_cfg_rt, dict):
            bot_cfg_rt = {}
        music_cfg_rt = cfg.get("music", {})
        if not isinstance(music_cfg_rt, dict):
            music_cfg_rt = {}
        chat_split_cfg_rt = cfg.get("chat_split", {})
        if not isinstance(chat_split_cfg_rt, dict):
            chat_split_cfg_rt = {}
        control_cfg_rt = cfg.get("control", {})
        if not isinstance(control_cfg_rt, dict):
            control_cfg_rt = {}
        voice_limit_default = _as_int(music_cfg_rt.get("max_voice_duration_seconds", 60), 60)
        voice_limit_raw = bot_cfg_rt.get("voice_send_max_seconds", voice_limit_default)
        voice_send_max_seconds = max(0, _as_int(voice_limit_raw, voice_limit_default))
        # 默认策略与 config/templates/master.template.yml + _built_in_config_defaults 对齐：
        # 长音频（>60s）默认走分段（每段 60s silk），短音频整段直发。
        voice_send_try_full_first = bool(bot_cfg_rt.get("voice_send_try_full_first", False))
        voice_send_split_enable = bool(bot_cfg_rt.get("voice_send_split_enable", True))
        voice_send_split_max_segments = max(1, min(20, _as_int(bot_cfg_rt.get("voice_send_split_max_segments", 8), 8)))
        # 点歌语音默认允许分段，不强制整段直发。
        voice_send_music_force_full = bool(bot_cfg_rt.get("voice_send_music_force_full", False))
        voice_send_music_disable_split = bool(bot_cfg_rt.get("voice_send_music_disable_split", False))
        send_rate_max_per_window, send_rate_window_seconds, send_rate_warn_threshold, send_rate_enable = (
            _resolve_send_rate_profile(cfg)
        )
        return {
            "reply_with_quote": bool(bot_cfg_rt.get("reply_with_quote", True)),
            "reply_with_at": bool(bot_cfg_rt.get("reply_with_at", False)),
            "multi_reply_enable": bool(bot_cfg_rt.get("multi_reply_enable", True)),
            "multi_reply_max_chunks": max(1, _as_int(bot_cfg_rt.get("multi_reply_max_chunks", 4), 4)),
            "multi_reply_max_lines": max(1, _as_int(bot_cfg_rt.get("multi_reply_max_lines", 1), 1)),
            "multi_reply_max_chars": max(160, _as_int(bot_cfg_rt.get("multi_reply_max_chars", 520), 520)),
            "multi_reply_chat_max_lines": max(
                1,
                _as_int(
                    bot_cfg_rt.get(
                        "multi_reply_chat_max_lines",
                        bot_cfg_rt.get("multi_reply_max_lines", 4),
                    ),
                    _as_int(bot_cfg_rt.get("multi_reply_max_lines", 4), 4),
                ),
            ),
            "multi_reply_chat_max_chars": max(120, _as_int(bot_cfg_rt.get("multi_reply_chat_max_chars", 320), 320)),
            "multi_reply_chat_max_chunks": max(1, _as_int(bot_cfg_rt.get("multi_reply_chat_max_chunks", 6), 6)),
            "multi_reply_interval_ms": max(0, _as_int(bot_cfg_rt.get("multi_reply_interval_ms", 260), 260)),
            "multi_image_max_count": max(1, _as_int(bot_cfg_rt.get("multi_image_max_count", 9), 9)),
            "multi_image_interval_ms": max(0, _as_int(bot_cfg_rt.get("multi_image_interval_ms", 150), 150)),
            "video_send_strategy": normalize_text(str(bot_cfg_rt.get("video_send_strategy", "direct_first"))).lower(),
            "napcat_media_stage_dir": normalize_text(str(bot_cfg_rt.get("napcat_media_stage_dir", ""))),
            "chat_split_mode": normalize_text(
                str(chat_split_cfg_rt.get("mode", control_cfg_rt.get("split_mode", "semantic")))
            ).lower() or "semantic",
            "send_rate_max_per_window": send_rate_max_per_window,
            "send_rate_window_seconds": send_rate_window_seconds,
            "send_rate_warn_threshold": send_rate_warn_threshold,
            "send_rate_enable": send_rate_enable,
            "voice_send_max_seconds": voice_send_max_seconds,
            "voice_send_try_full_first": voice_send_try_full_first,
            "voice_send_split_enable": voice_send_split_enable,
            "voice_send_split_max_segments": voice_send_split_max_segments,
            "voice_send_music_force_full": voice_send_music_force_full,
            "voice_send_music_disable_split": voice_send_music_disable_split,
        }

    # 启动时验证各平台 cookie 有效性
    @nonebot.get_driver().on_startup
    async def _check_cookies_on_startup():
        nonlocal _platform_manager
        try:
            await engine.async_init()
        except Exception as e:
            _log.warning("engine_async_init_failed | %s", e)
        try:
            from core.cookie_auth import check_all_cookies
            results = await check_all_cookies(engine.config)
            for platform, valid in results.items():
                if not valid:
                    _log.warning("cookie_expired | %s | 建议重新登录: /yuki cookie %s", platform, platform)
        except Exception as e:
            _log.debug("cookie_check_skip | %s", e)
        # 登录离线消息回填：只读导入 memory，不做任何自动回复。
        asyncio.create_task(_run_login_backlog_import(reason="startup"))
        # Phase 2 平台路径：配置 `platform.onebot11.access_token` 时双轨启用
        # （OneBot11Adapter → engine 消息管线 → adapter 发送）。与 NoneBot 并存，
        # 完整切换（关闭 NoneBot）待真机 smoke 后由 owner 决定。
        try:
            platform_cfg = engine.config.get("platform", {}) if isinstance(engine.config, dict) else {}
            onebot_cfg = platform_cfg.get("onebot11", {}) if isinstance(platform_cfg, dict) else {}
            if isinstance(onebot_cfg, dict) and onebot_cfg.get("access_token"):
                from core.platform.bridge import register_onebot11_platform

                enabled_cfg = dict(onebot_cfg)
                enabled_cfg["enabled"] = True
                _platform_manager = await register_onebot11_platform(
                    engine,
                    dispatcher,
                    config=enabled_cfg,
                    trace_builder=_build_trace_id,
                )
        except Exception as e:
            _log.warning("platform_onebot11_startup_failed | %s", e)

    @nonebot.get_driver().on_shutdown
    async def _flush_memory_on_shutdown():
        if _platform_manager is not None:
            try:
                await _platform_manager.stop()
                _log.info("platform_stopped_on_shutdown | ok")
            except Exception as e:
                _log.warning("platform_stop_failed | %s", e)
        try:
            memory = getattr(engine, "memory", None)
            if memory is not None and hasattr(memory, "close") and callable(getattr(memory, "close")):
                memory.close()
                _log.info("memory_close_on_shutdown | ok")
            elif memory is not None and hasattr(memory, "flush") and callable(getattr(memory, "flush")):
                memory.flush()
                _log.info("memory_flush_on_shutdown | ok")
        except Exception as e:
            _log.warning("memory_flush_on_shutdown_failed | %s", e)
        finally:
            _RUNTIME_WEBUI_BRIDGE["queue"] = None
            _RUNTIME_WEBUI_BRIDGE["latest_ctx"] = {}

    @router.handle()
    async def handle_message(bot: Bot, event: MessageEvent) -> None:
        raw_segments = _extract_raw_segments(event)
        event_payload = _event_to_dict(event)
        _log_qq_message_event(
            event=event,
            raw_segments=raw_segments,
            bot_id=str(bot.self_id),
            raw_payload=event_payload,
        )
        if str(event.get_user_id()) == str(bot.self_id):
            return

        # ── 去重：同一用户短时间内发送完全相同的消息只处理一次 ──
        import hashlib as _hl
        import time as _time
        _dedup_uid = str(event.get_user_id())
        _dedup_conv = _build_conversation_id(event)
        _dedup_key = f"{_dedup_conv}:{_dedup_uid}"
        _dedup_raw = str(event.get_message())
        _dedup_hash = _hl.md5(_dedup_raw.encode("utf-8", errors="replace")).hexdigest()
        _dedup_now = _time.monotonic()
        _prev = _recent_msg_hashes.get(_dedup_key)
        if _prev and _prev[0] == _dedup_hash and (_dedup_now - _prev[1]) < _DEDUP_WINDOW_SECONDS:
            _log.debug("dedup_skip | conv=%s | user=%s", _dedup_conv, _dedup_uid)
            return
        _recent_msg_hashes[_dedup_key] = (_dedup_hash, _dedup_now)
        # 清理过期条目（惰性清理，避免内存泄漏）
        if len(_recent_msg_hashes) > 2000:
            expired = [k for k, v in _recent_msg_hashes.items() if _dedup_now - v[1] > 10]
            for k in expired:
                _recent_msg_hashes.pop(k, None)
            if len(_recent_msg_hashes) > 2000:
                sorted_keys = sorted(_recent_msg_hashes, key=lambda k: _recent_msg_hashes[k][1])
                for k in sorted_keys[:len(_recent_msg_hashes) - 1500]:
                    _recent_msg_hashes.pop(k, None)

        conversation_id = _dedup_conv
        at_targets = _extract_at_targets(event)
        msg_type = str(getattr(event, "message_type", ""))
        is_group_message = msg_type == "group"
        is_private_message = msg_type == "private"
        event_group_id = int(getattr(event, "group_id", 0) or 0)

        # 预缓存入站媒体：即使当前消息被门禁跳过，也保留“先发图后提问”的上下文。
        if _has_media_segments(raw_segments):
            try:
                remember_media = getattr(getattr(engine, "tools", None), "remember_incoming_media", None)
                if callable(remember_media):
                    remember_media(conversation_id, raw_segments)
                    if is_group_message and event_group_id > 0:
                        scoped_conversation = f"group:{event_group_id}:user:{event.get_user_id()}"
                        if scoped_conversation != conversation_id:
                            remember_media(scoped_conversation, raw_segments)
            except Exception as exc:
                _log.warning("pre_gate_media_cache_failed | conv=%s | err=%s", conversation_id, exc)

        allow_non_to_me_rt, runtime_bot_cfg, _, _ = _resolve_runtime_matcher_flags()
        raw_text = _extract_text_segments(raw_segments) or event.get_plaintext().strip()
        admin_command = engine.admin.is_admin_command(raw_text)
        alias_prefix_call = _starts_with_bot_alias(raw_text, runtime_bot_cfg)
        explicit_command = _looks_like_explicit_user_command(raw_text)

        # 私聊开关：off | whitelist | all（管理员命令保留兜底入口）。
        if is_private_message and not admin_command:
            sender_uid = str(event.get_user_id())
            if not _allow_private_chat_for_user(runtime_bot_cfg, sender_uid):
                _log.debug("private_chat_blocked | user=%s | mode=%s", sender_uid, runtime_bot_cfg.get("private_chat_mode", "off"))
                return

        # 最外层 matcher 硬门禁：非 @ 群消息不进入 agent/queue（除非显式开启 allow_non_to_me）。
        if is_group_message and not allow_non_to_me_rt and not admin_command:
            event_to_me = bool(getattr(event, "to_me", False))
            fast_at_me = any(item in {"all", str(bot.self_id)} for item in at_targets)
            if (
                not event_to_me
                and not fast_at_me
                and not _extract_reply_message_id(event)
                and not alias_prefix_call
                and not explicit_command
            ):
                _log.debug("matcher_skip_non_to_me | conv=%s | user=%s", conversation_id, event.get_user_id())
                return

        reply_to_message_id = _extract_reply_message_id(event)
        reply_to_user_id = ""
        reply_to_user_name = ""
        reply_to_text = ""
        reply_media_segments: list[dict[str, Any]] = []
        if reply_to_message_id:
            (
                reply_to_user_id,
                reply_to_user_name,
                reply_to_text,
                reply_media_segments,
            ) = await _resolve_reply_context(bot, reply_to_message_id, event=event)

        mentioned = _is_mentioned(bot, event, at_targets=at_targets) or (
            reply_to_user_id and str(reply_to_user_id) == str(bot.self_id)
        )
        at_other_user_ids, at_other_user_only = _resolve_other_user_targets(
            bot_id=str(bot.self_id),
            at_targets=at_targets,
            reply_to_user_id=reply_to_user_id,
            mentioned=mentioned,
        )
        has_media = _has_media_segments(raw_segments)
        text = raw_text
        # nickname 前缀命中：显式别名调用总是允许；其余场景仅在 allow_non_to_me=true 时启用。
        if (allow_non_to_me_rt or alias_prefix_call) and not mentioned and text:
            _nick_lower = text.lower()
            _bot_nicks = {str(n).lower() for n in runtime_bot_cfg.get("nicknames", []) if n}
            _bot_nicks.add(str(runtime_bot_cfg.get("name", "")).lower())
            _bot_nicks.discard("")
            for _nick in _bot_nicks:
                if _nick_lower.startswith(_nick):
                    mentioned = True
                    text = text[len(_nick):].lstrip()
                    break
        if (
            is_group_message
            and not allow_non_to_me_rt
            and not mentioned
            and not admin_command
            and not alias_prefix_call
            and not explicit_command
        ):
            _log.debug("matcher_skip_non_to_me_resolved | conv=%s | user=%s", conversation_id, event.get_user_id())
            return
        if not text and mentioned and not has_media:
            # Mention-only prompt fallback.
            text = "__mention_only__"
        if has_media:
            # 尝试解析语音消息为文字
            voice_text = await _try_extract_voice_text(bot, raw_segments)
            # Normalize media placeholders produced by adapter text rendering.
            clean_text = _strip_media_placeholder_text(text)
            media_event = _build_multimodal_text(raw_segments, mentioned=mentioned)
            if voice_text:
                media_event = f"{media_event}\n[语音内容] {voice_text}"
            text = f"{media_event}\n{clean_text}" if clean_text else media_event
        if not text:
            return

        sender_uid = str(event.get_user_id())
        sender_role = _extract_sender_role(event)

        async def api_call(api: str, **kwargs: Any) -> Any:
            return await call_napcat_bot_api(bot, api, **kwargs)

        ignored = engine.admin.is_user_ignored(sender_uid, event_group_id)
        allow_admin_recovery = engine.admin.is_admin_command(text) and _is_group_admin_sender(
            user_id=sender_uid,
            group_id=event_group_id,
            sender_role=sender_role,
        )
        if ignored and not allow_admin_recovery:
            ignore_policy = normalize_text(getattr(engine.admin, "ignore_policy", "silent")).lower() or "silent"
            _log.info(
                "ignored_user_blocked | conv=%s | group=%s | user=%s | policy=%s",
                conversation_id,
                event_group_id,
                sender_uid,
                ignore_policy,
            )
            if ignore_policy == "soft":
                await _safe_send(
                    bot=bot,
                    event=event,
                    message=Message("当前不会处理你的消息。"),
                )
            return

        # ── 管理员指令拦截 ──
        if engine.admin.is_admin_command(text):
            if (
                is_group_message
                and engine.admin.enabled
                and engine.admin.non_whitelist_mode == "silent"
                and not engine.admin.is_group_whitelisted(event_group_id)
            ):
                admin_action = _resolve_admin_action(text)
                # silent 模式下放行“加白”引导命令，避免新群无法自助纳管。
                if admin_action != "white_add":
                    _log.debug(
                        "admin_command_silent_skip | group=%s | user=%s | action=%s",
                        event_group_id,
                        event.get_user_id(),
                        admin_action or "-",
                    )
                    return
                _log.debug(
                    "admin_command_silent_allow_bootstrap | group=%s | user=%s | action=%s",
                    event_group_id,
                    event.get_user_id(),
                    admin_action,
                )
            admin_reply = await engine.admin.handle_command(
                text=text,
                user_id=sender_uid,
                group_id=event_group_id,
                sender_role=sender_role,
                engine=engine,
                api_call=api_call,
            )
            if admin_reply:
                await _safe_send(bot=bot, event=event, message=Message(admin_reply))
            return

        queue_cfg_rt = engine.config.get("queue", {}) if isinstance(engine.config, dict) else {}
        if not isinstance(queue_cfg_rt, dict):
            queue_cfg_rt = {}

        seq = dispatcher.next_seq(conversation_id)
        trace_id = _build_trace_id(conversation_id=conversation_id, seq=seq)

        payload = EngineMessage(
            conversation_id=conversation_id,
            user_id=str(event.get_user_id()),
            user_name=_extract_user_name(event),
            text=text,
            message_id=str(getattr(event, "message_id", "")),
            seq=seq,
            raw_segments=raw_segments,
            queue_depth=dispatcher.pending_count(conversation_id),
            mentioned=mentioned,
            is_private=getattr(event, "message_type", "") == "private",
            timestamp=_event_timestamp(event),
            group_id=event_group_id,
            bot_id=str(bot.self_id),
            at_other_user_only=at_other_user_only,
            at_other_user_ids=at_other_user_ids,
            reply_to_message_id=reply_to_message_id,
            reply_to_user_id=reply_to_user_id,
            reply_to_user_name=reply_to_user_name,
            reply_to_text=reply_to_text,
            reply_media_segments=reply_media_segments,
            api_call=api_call,
            trace_id=trace_id,
            sender_role=_extract_sender_role(event),
            event_payload=event_payload,
        )
        video_pre_ack_sent = False
        queue_cfg = queue_cfg_rt
        video_heavy_request = _looks_like_video_heavy_request(text=text, raw_segments=raw_segments)
        download_heavy_request = _looks_like_download_heavy_request(text=text, raw_segments=raw_segments)
        web_heavy_request = _looks_like_web_heavy_request(text=text, raw_segments=raw_segments)
        default_web_timeout = max(dispatcher.process_timeout_seconds + 90, 270)
        web_process_timeout = max(
            dispatcher.process_timeout_seconds,
            int(queue_cfg.get("web_process_timeout_seconds", default_web_timeout)),
        )
        default_video_timeout = max(dispatcher.process_timeout_seconds + 180, 300)
        video_process_timeout = max(
            dispatcher.process_timeout_seconds,
            int(queue_cfg.get("video_process_timeout_seconds", default_video_timeout)),
        )
        default_download_timeout = max(dispatcher.process_timeout_seconds + 180, 300)
        download_process_timeout = max(
            dispatcher.process_timeout_seconds,
            int(queue_cfg.get("download_process_timeout_seconds", default_download_timeout)),
        )
        process_timeout_override = None
        if web_heavy_request:
            process_timeout_override = web_process_timeout
        if video_heavy_request:
            process_timeout_override = max(process_timeout_override or 0, video_process_timeout)
        if download_heavy_request:
            process_timeout_override = max(process_timeout_override or 0, download_process_timeout)

        async def process() -> Any:
            return await engine.handle_message(payload)

        async def send_response(result: Any) -> None:
            nonlocal video_pre_ack_sent
            send_opts = _resolve_runtime_send_options()
            reply_with_quote = bool(send_opts["reply_with_quote"])
            reply_with_at = bool(send_opts["reply_with_at"])
            multi_reply_enable = bool(send_opts["multi_reply_enable"])
            multi_reply_max_chunks = int(send_opts["multi_reply_max_chunks"])
            multi_reply_max_lines = int(send_opts["multi_reply_max_lines"])
            multi_reply_max_chars = int(send_opts["multi_reply_max_chars"])
            multi_reply_chat_max_lines = int(send_opts["multi_reply_chat_max_lines"])
            multi_reply_chat_max_chars = int(send_opts["multi_reply_chat_max_chars"])
            multi_reply_chat_max_chunks = int(send_opts["multi_reply_chat_max_chunks"])
            multi_reply_interval_ms = int(send_opts["multi_reply_interval_ms"])
            multi_image_max_count = int(send_opts["multi_image_max_count"])
            multi_image_interval_ms = int(send_opts["multi_image_interval_ms"])
            video_send_strategy = str(send_opts["video_send_strategy"])
            napcat_media_stage_dir = str(send_opts["napcat_media_stage_dir"])
            chat_split_mode = str(send_opts["chat_split_mode"])
            send_rate_max_per_window = int(send_opts["send_rate_max_per_window"])
            send_rate_window_seconds = int(send_opts["send_rate_window_seconds"])
            send_rate_warn_threshold = int(send_opts["send_rate_warn_threshold"])
            send_rate_enable = bool(send_opts["send_rate_enable"])
            latest_ctx = _latest_queue_task_ctx.get(payload.conversation_id, {})
            latest_trace = normalize_text(str(latest_ctx.get("trace_id", ""))) if isinstance(latest_ctx, dict) else ""
            if getattr(result, "action", "") == "ignore":
                _log.info(
                    "send_skip_ignore | trace=%s | conversation=%s | reason=%s | mentioned=%s | private=%s | text=%s",
                    payload.trace_id,
                    payload.conversation_id,
                    normalize_text(str(getattr(result, "reason", "") or "")) or "-",
                    bool(payload.mentioned),
                    bool(payload.is_private),
                    clip_text(text, 80),
                )
                return

            (
                action,
                is_music_voice_action,
                reply_text,
                image_url,
                image_urls,
                video_url,
                cover_url,
                record_b64,
                audio_file,
            ) = _normalize_send_media_fields(result)
            reply_to_bot = bool(
                normalize_text(str(payload.reply_to_user_id or ""))
                and normalize_text(str(payload.reply_to_user_id or ""))
                == normalize_text(str(payload.bot_id or ""))
            )
            (
                latest_seq,
                stale_trace,
                same_user_newer_turn,
                cancel_newer_turn,
                stale_plain_reply,
            ) = _classify_stale_send(
                latest_ctx,
                latest_trace,
                payload,
                action,
                reply_text,
                image_urls,
                video_url,
                cover_url,
                record_b64,
                audio_file,
            )
            # 只在**真被打断**时丢（新消息带明确的取消/更正意图）。
            #
            # 原来的条件是 `same_user_newer_turn or cancel_newer_turn` ——
            # 同一个人在 bot 思考期间又说了一句话就丢掉整条回答。实测（2026-08-06
            # 业主的群，15126 行日志）：**87 条已生成的回复被这样丢掉**，
            # 占生成量的 26%，而实际发出去的只有 243 条。
            # 根因是时间尺度不匹配：群里消息间隔 3-4 秒，而回合 p50 27 秒，
            # 所以「思考期间对方又说了一句」几乎是必然事件，不是打断信号。
            # 用户看到的现象就是「机器人老是不理人」—— 它明明决定要回、也写完了。
            #
            # `same_user_newer_turn` 仍保留给下面那条 send_allow_stale_trace 日志，
            # 便于回看「发出去的回复里有多少是滞后的」。
            if stale_plain_reply and cancel_newer_turn:
                _log.info(
                    "send_drop_stale_plain_reply | trace=%s | latest=%s | conversation=%s | seq=%s | latest_seq=%s | text=%s",
                    payload.trace_id,
                    latest_trace,
                    payload.conversation_id,
                    payload.seq,
                    latest_seq or "-",
                    clip_text(reply_text, 120),
                )
                return
            if stale_trace:
                _log.info(
                    "send_allow_stale_trace | trace=%s | latest=%s | conversation=%s | seq=%s | latest_seq=%s | has_media=%s | same_user=%s",
                    payload.trace_id,
                    latest_trace,
                    payload.conversation_id,
                    payload.seq,
                    latest_seq or "-",
                    bool(image_urls or video_url or cover_url or record_b64 or audio_file),
                    same_user_newer_turn,
                )
            # 音乐缓存路径推断已迁入 core.response_delivery.deliver_response（E4）：
            # 未显式标记点歌时按 audio 路径落在 storage/cache/music/ 或 music_* 命名推断。
            delivered = False
            send_attempts = 0
            send_success = 0
            rate_limited = False
            chunk_count = 0
            video_issue = ""
            video_analysis_requested = bool(getattr(result, "pre_ack", ""))
            prefix = _build_reply_prefix(
                event=event,
                quote_message_id=str(getattr(event, "message_id", "") or ""),
                sender_user_id=str(event.get_user_id()),
                enable_quote=reply_with_quote,
                enable_at=reply_with_at,
            )
            prefixed_sent = False

            # 统一发送核心：限流/熔断/暂停经 core.response_delivery.build_send_guard
            # （架构收敛任务 B3）。失败标记保留在 _safe_send 内部（no-op 注入，
            # 见 _noop_send_failure_mark），不重复熔断。
            from core.response_delivery import build_send_guard, deliver_response

            async def _raw_platform_send(msg: Message) -> bool:
                return await _safe_send(bot=bot, event=event, message=msg)

            send_guard = build_send_guard(
                engine.config,
                _raw_platform_send,
                conversation_id=payload.conversation_id,
                group_id=payload.group_id,
                bot_id=str(getattr(bot, "self_id", "") or ""),
                mark_failure_fn=_noop_send_failure_mark,
            )

            async def send_msg(msg: Message) -> bool:
                nonlocal delivered, send_attempts, send_success, rate_limited
                send_attempts += 1
                ok = await send_guard(msg)
                if ok:
                    send_success += 1
                    delivered = True
                return ok

            if video_url and video_analysis_requested and not video_pre_ack_sent:
                progress = Message()
                if not prefixed_sent:
                    progress += prefix
                    prefixed_sent = True
                pre_ack_text = str(getattr(result, "pre_ack", "") or "OK，我现在去深度分析这个视频，稍等。")
                progress += Message(pre_ack_text)
                await send_msg(progress)
                video_pre_ack_sent = True
                delivered = True

            # ── 语音/音频消息（点歌功能）：文案前缀 + 统一发送核心 ──
            # 点歌语音 4 特性（silk 源互换 / music_force_full / music_disable_split /
            # 裁剪兜底+整段去重）已迁入 core.response_delivery._send_voice（E4）。
            if record_b64 or audio_file:
                voice_msg = Message()
                if reply_text:
                    if not prefixed_sent:
                        voice_msg += prefix
                        prefixed_sent = True
                    voice_msg += Message(reply_text)
                    text_ok = await send_msg(voice_msg)
                    delivered = delivered or text_ok
                    # 仅在文案发送成功时清空，避免失败后文本丢失。
                    if text_ok:
                        reply_text = ""
                # 语音本体走统一发送核心；文本/媒体已在此路径外发送，这里只带语音字段。
                voice_response = EngineResponse(
                    action=action,
                    reason=str(getattr(result, "reason", "") or ""),
                    record_b64=record_b64,
                    audio_file=audio_file,
                )

                async def _voice_platform_send(chain: Any) -> bool:
                    segments = chain.to_onebot_segments() if hasattr(chain, "to_onebot_segments") else []
                    msg = Message()
                    for seg in segments:
                        msg.append(MessageSegment(type=seg.get("type", "text"), data=seg.get("data", {})))
                    return await _safe_send(bot=bot, event=event, message=msg)

                try:
                    voice_ok = await deliver_response(
                        engine.config,
                        voice_response,
                        _voice_platform_send,
                        conversation_id=payload.conversation_id,
                        group_id=payload.group_id,
                        bot_id=str(getattr(bot, "self_id", "") or ""),
                        mark_failure_fn=_noop_send_failure_mark,
                        is_music_voice_action=is_music_voice_action,
                    )
                except Exception as _voice_core_err:
                    _log.warning("voice_send_core_fail | %s", _voice_core_err)
                    voice_ok = False
                send_attempts += 1
                if voice_ok:
                    send_success += 1
                    delivered = True
                else:
                    fallback = Message()
                    if not prefixed_sent:
                        fallback += prefix
                        prefixed_sent = True
                    fallback += Message(reply_text or "语音发送失败了。")
                    await send_msg(fallback)
                    delivered = True

            if video_url:
                video_issue = await _inspect_video_issue(video_url)
                if video_issue:
                    warn = Message()
                    if not prefixed_sent:
                        warn += prefix
                        prefixed_sent = True
                    warn += Message(f"意外日志：{video_issue}")
                    if "无音轨" in video_issue or "没声音" in video_issue:
                        warn += Message("\n意外发现：这个视频没有可用音轨，发送后会没声音。我先走降级方案给你来源链接。")
                    else:
                        warn += Message("\n意外发现：视频资源疑似损坏，我先走降级方案给你来源链接。")
                    await send_msg(warn)
                    delivered = True

            text_chunks, rate_limited, chunk_count = _plan_reply_text_chunks(
                reply_text,
                video_url,
                action,
                multi_reply_enable,
                multi_reply_max_lines,
                multi_reply_max_chars,
                multi_reply_max_chunks,
                multi_reply_chat_max_lines,
                multi_reply_chat_max_chars,
                multi_reply_chat_max_chunks,
                video_analysis_requested,
                chat_split_mode,
                send_rate_enable,
                send_rate_max_per_window,
                send_rate_window_seconds,
                send_rate_warn_threshold,
                payload.conversation_id,
                payload.group_id,
            )

            async def _retry_send_remaining_text(remaining_text: str) -> bool:
                nonlocal prefixed_sent, delivered
                pending = _normalize_reply_text(remaining_text)
                if not pending:
                    return False
                base_chunk_chars = (
                    multi_reply_chat_max_chars if action == "reply" else multi_reply_max_chars
                )
                safe_chunk_chars = max(240, min(920, base_chunk_chars + 140))
                approx_needed = max(1, math.ceil(len(pending) / max(120, safe_chunk_chars)))
                retry_max_chunks = max(8, min(48, approx_needed + 4))
                retry_chunks = _split_reply_chunks(
                    pending,
                    max_lines=max(6, (multi_reply_chat_max_lines if action == "reply" else multi_reply_max_lines)),
                    max_chars=safe_chunk_chars,
                    max_chunks=retry_max_chunks,
                )
                retry_chunks = _rebalance_text_chunks_for_send(
                    retry_chunks or [pending],
                    max_chars=safe_chunk_chars,
                )
                if not retry_chunks:
                    return False
                _log.warning(
                    "text_send_retry_start | trace=%s | chunks=%d | chars=%d",
                    payload.trace_id,
                    len(retry_chunks),
                    len(pending),
                )
                for idx, chunk in enumerate(retry_chunks):
                    msg = Message()
                    if not prefixed_sent:
                        msg += prefix
                        prefixed_sent = True
                    if idx == 0:
                        msg += Message("（补发剩余内容）\n")
                    msg += Message(chunk)
                    ok = await send_msg(msg)
                    if not ok:
                        _log.warning(
                            "text_send_retry_fail | trace=%s | chunk=%d/%d",
                            payload.trace_id,
                            idx + 1,
                            len(retry_chunks),
                        )
                        return False
                    delivered = True
                    if idx < len(retry_chunks) - 1 and multi_reply_interval_ms > 0:
                        await asyncio.sleep(multi_reply_interval_ms / 1000)
                _log.info(
                    "text_send_retry_ok | trace=%s | chunks=%d",
                    payload.trace_id,
                    len(retry_chunks),
                )
                return True

            # ── 有视频时：先发视频（或文件）确认可达，再发文本/封面 ──
            if video_url:
                cover_seg = None
                if cover_url:
                    cover_seg = await _build_image_segment(cover_url)
                if not cover_seg and not video_issue:
                    # 没有远程封面，用本地生成的缩略图
                    local_vp = Path(video_url) if not video_url.startswith(("http://", "https://", "file://")) else None
                    if local_vp and local_vp.exists():
                        thumb = await _generate_video_thumbnail(local_vp)
                        if thumb:
                            cover_seg = await _build_image_segment(str(thumb.resolve()))

                # 1) 先尝试发视频本体（或文件上传）
                prefer_upload_first = (
                    _video_strategy_upload_first(video_send_strategy)
                    or _should_upload_video_file_first(video_url)
                )
                upload_only = _video_strategy_upload_only(video_send_strategy)
                allow_upload_fallback = _video_strategy_allows_upload_fallback(video_send_strategy)
                video_delivered = False
                fallback_sent = False
                if prefer_upload_first:
                    uploaded = await _try_upload_video_file(
                        bot=bot,
                        event=event,
                        video_url=video_url,
                        stage_dir=napcat_media_stage_dir,
                        trace_id=payload.trace_id,
                    )
                    if uploaded:
                        _log.info("video_send_via_upload_file | strategy=%s", video_send_strategy)
                        delivered = True
                        video_delivered = True
                    if upload_only:
                        if not video_delivered:
                            fallback = Message()
                            if not prefixed_sent:
                                fallback += prefix
                                prefixed_sent = True
                            fallback += Message("视频文件上传失败了，你稍后重试或换个链接。")
                            await send_msg(fallback)
                            delivered = True
                            fallback_sent = True

                if video_delivered or upload_only:
                    seg = None
                else:
                    seg = None if video_issue else await _build_video_segment(
                        video_url,
                        stage_dir=napcat_media_stage_dir,
                        prefer_plain_path=False,
                        trace_id=payload.trace_id,
                    )
                if video_delivered:
                    pass
                elif seg is not None:
                    sent_video = False
                    send_exc: Exception | None = None
                    try:
                        _log.info(
                            "media_delivery_inline_attempt | channel=video_segment_file_uri | ref=%s",
                            clip_text(str((getattr(seg, "data", {}) or {}).get("file", "")), 180),
                        )
                        sent_video = await send_msg(Message(seg))
                    except Exception as exc:
                        send_exc = exc
                    if not sent_video:
                        uri_seg = None if video_issue else await _build_video_segment(
                            video_url,
                            stage_dir=napcat_media_stage_dir,
                            prefer_plain_path=True,
                            trace_id=payload.trace_id,
                        )
                        if uri_seg is not None:
                            try:
                                _log.info(
                                    "media_delivery_inline_attempt | channel=video_segment_plain_path | ref=%s",
                                    clip_text(str((getattr(uri_seg, "data", {}) or {}).get("file", "")), 180),
                                )
                                sent_video = await send_msg(Message(uri_seg))
                            except Exception as exc:
                                send_exc = exc
                        if sent_video:
                            video_delivered = True
                            delivered = True
                            seg = None
                    if not sent_video:
                        if send_exc is not None:
                            _log.warning(
                                "video_send_fail | %s | upload_fallback_allowed=%s",
                                send_exc,
                                allow_upload_fallback,
                            )
                        else:
                            _log.warning(
                                "video_send_fail | safe_send_failed | upload_fallback_allowed=%s",
                                allow_upload_fallback,
                            )
                        # 可选降级到 NapCat 文件 API；direct_first 默认不把视频悄悄变群文件。
                        if allow_upload_fallback:
                            uploaded = await _try_upload_video_file(
                                bot=bot,
                                event=event,
                                video_url=video_url,
                                stage_dir=napcat_media_stage_dir,
                                trace_id=payload.trace_id,
                            )
                            if uploaded:
                                _log.info("video_send_via_upload_file | strategy=fallback")
                                video_delivered = True
                                delivered = True
                        else:
                            _log.warning(
                                "media_delivery_upload_skip | trace=%s | strategy=%s | reason=inline_failed_no_file_fallback",
                                payload.trace_id,
                                video_send_strategy,
                            )
                    else:
                        video_delivered = True
                        delivered = True

                if not video_delivered and not fallback_sent:
                    direct_url = str(video_url or "").strip()
                    fallback_msg = Message()
                    if not prefixed_sent:
                        fallback_msg += prefix
                        prefixed_sent = True
                    if re.match(r"^https?://", direct_url, flags=re.IGNORECASE):
                        fallback_msg += Message(f"视频暂时不能直发，来源链接：{direct_url}")
                    else:
                        _log.warning(
                            "media_delivery_failed_exact | channel=video_all | video=%s",
                            clip_text(direct_url, 180),
                        )
                        if allow_upload_fallback or prefer_upload_first:
                            fallback_msg += Message("视频解析成功了，但 NapCat/QQ 发送失败：我已经尝试视频直发和文件上传。请检查 NapCat 对 QQ 容器暂存目录的读取权限。")
                        else:
                            fallback_msg += Message("视频解析成功了，但 QQ 可预览视频直发失败；当前策略不自动降级成群文件。需要发文件时把 video_send_strategy 改成 direct_with_file_fallback 或 upload_file_first。")
                    await send_msg(fallback_msg)
                    delivered = True
                    fallback_sent = True

                # 2) 视频有稳定投递后，再发文本与封面。
                #    如果视频没投递成功，仅发文本，不发封面，避免“只有坏缩略图”。
                send_cover_seg = cover_seg if video_delivered else None
                first_chunk = True
                failed_index = -1
                for idx, chunk in enumerate(text_chunks):
                    msg = Message()
                    if not prefixed_sent:
                        msg += prefix
                        prefixed_sent = True
                    msg += Message(chunk)
                    attach_cover = first_chunk and send_cover_seg is not None
                    if attach_cover:
                        msg += send_cover_seg
                    ok = await send_msg(msg)
                    if ok:
                        delivered = True
                        if attach_cover:
                            send_cover_seg = None
                    else:
                        failed_index = idx
                        _log.warning(
                            "text_chunk_send_fail | trace=%s | chunk=%d/%d | with_video=true",
                            payload.trace_id,
                            idx + 1,
                            len(text_chunks),
                        )
                        break
                    first_chunk = False
                    if idx < len(text_chunks) - 1 and multi_reply_interval_ms > 0:
                        await asyncio.sleep(multi_reply_interval_ms / 1000)
                if failed_index >= 0:
                    remaining_text = _normalize_reply_text(
                        "\n".join(text_chunks[failed_index:])
                    )
                    if remaining_text:
                        await _retry_send_remaining_text(remaining_text)

                # 无文本但视频成功且有封面，补发封面
                if not text_chunks and send_cover_seg is not None:
                    msg = Message()
                    if not prefixed_sent:
                        msg += prefix
                        prefixed_sent = True
                    msg += send_cover_seg
                    await send_msg(msg)
                    delivered = True

            else:
                # ── 无视频：正常发文本+图片 ──
                # 单图时：把图片合并到最后一条文本消息里，减少刷屏
                single_image_seg = None
                if image_urls and len(image_urls) == 1:
                    single_image_seg = await _build_image_segment(image_urls[0])

                failed_index = -1
                last_chunk_idx = len(text_chunks) - 1
                for idx, chunk in enumerate(text_chunks):
                    msg = Message()
                    if not prefixed_sent:
                        msg += prefix
                        prefixed_sent = True
                    msg += Message(chunk)
                    # 单图场景：把图片段合并到最后一条文本消息里（一条消息 = 文字 + 图片）
                    attached_single_image = False
                    if idx == last_chunk_idx and single_image_seg is not None:
                        msg += single_image_seg
                        attached_single_image = True
                    ok = await send_msg(msg)
                    if ok:
                        delivered = True
                        if attached_single_image:
                            single_image_seg = None
                    else:
                        failed_index = idx
                        _log.warning(
                            "text_chunk_send_fail | trace=%s | chunk=%d/%d | with_video=false",
                            payload.trace_id,
                            idx + 1,
                            len(text_chunks),
                        )
                        break
                    if idx < len(text_chunks) - 1 and multi_reply_interval_ms > 0:
                        await asyncio.sleep(multi_reply_interval_ms / 1000)
                if failed_index >= 0:
                    remaining_text = _normalize_reply_text(
                        "\n".join(text_chunks[failed_index:])
                    )
                    if remaining_text:
                        await _retry_send_remaining_text(remaining_text)

                # 单图已合并到文本消息中；若没合并成功（无文本），单独发
                if single_image_seg is not None:
                    msg = Message()
                    if not prefixed_sent:
                        msg += prefix
                        prefixed_sent = True
                    msg += single_image_seg
                    if await send_msg(msg):
                        delivered = True
                    else:
                        _log.warning("image_send_fail | trace=%s | mode=single", payload.trace_id)

                # 多图场景：保持原逻辑，合并多图到单条消息
                if image_urls and len(image_urls) > 1:
                    send_count = min(len(image_urls), multi_image_max_count)
                    if send_count > 1:
                        merged_text = normalize_text(" ".join(text_chunks))
                        if "图文" not in merged_text and "共" not in merged_text:
                            tip = Message()
                            if not prefixed_sent:
                                tip += prefix
                                prefixed_sent = True
                            tip += Message(f"识别到这是图文作品，共 {len(image_urls)} 张，先发你 {send_count} 张。")
                            await send_msg(tip)
                            delivered = True

                    # 合并多图到单条消息（OneBot V11 支持多 image 段）
                    combined_msg = Message()
                    if not prefixed_sent:
                        combined_msg += prefix
                        prefixed_sent = True
                    failed_count = 0
                    for idx, item_url in enumerate(image_urls[:send_count], 1):
                        seg = await _build_image_segment(item_url)
                        if seg is not None:
                            combined_msg += seg
                        else:
                            failed_count += 1
                    if failed_count > 0:
                        combined_msg += Message(f"\n（{failed_count} 张图片发送失败，链接受限）")
                    if await send_msg(combined_msg):
                        delivered = True
                    else:
                        _log.warning(
                            "image_send_fail | trace=%s | mode=multi | requested=%d | build_failed=%d",
                            payload.trace_id,
                            send_count,
                            failed_count,
                        )

                    if len(image_urls) > send_count:
                        tip_more = Message()
                        if not prefixed_sent:
                            tip_more += prefix
                            prefixed_sent = True
                        tip_more += Message(f"其余 {len(image_urls) - send_count} 张先省略。")
                        await send_msg(tip_more)
                        delivered = True

            delivered = send_success > 0
            if delivered:
                engine.on_delivery_success(
                    conversation_id=payload.conversation_id,
                    user_id=payload.user_id,
                    action=action,
                )
            _log.info(
                "send_final | trace=%s | conversation=%s | seq=%s | action=%s | delivered=%s | has_video=%s | has_image=%s | send_attempts=%d | send_success=%d | chunk_count=%d | rate_limited=%s",
                payload.trace_id,
                payload.conversation_id,
                payload.seq,
                action,
                delivered,
                bool(video_url),
                bool(image_url or image_urls),
                send_attempts,
                send_success,
                chunk_count,
                rate_limited,
            )

        async def send_overload_notice(text_notice: str) -> None:
            if not text_notice:
                return
            await _safe_send(bot=bot, event=event, message=Message(text_notice))

        async def on_dispatch_complete(dispatch: Any) -> None:
            status = str(getattr(dispatch, "status", ""))
            reason = str(getattr(dispatch, "reason", ""))
            dispatch_seq = int(getattr(dispatch, "seq", seq) or seq)
            dispatch_trace = str(getattr(dispatch, "trace_id", payload.trace_id))
            engine.logger.info(
                "queue_final | trace=%s | conversation=%s | seq=%s | status=%s | reason=%s | pending=%s",
                dispatch_trace,
                str(getattr(dispatch, "conversation_id", conversation_id)),
                str(getattr(dispatch, "seq", seq)),
                status,
                reason,
                str(getattr(dispatch, "pending_count", 0)),
            )
            pending_count = int(getattr(dispatch, "pending_count", 0) or 0)
            latest_ctx = _latest_queue_task_ctx.get(conversation_id)
            if isinstance(latest_ctx, dict):
                latest_seq_for_cleanup = int(latest_ctx.get("seq", -1) or -1)
                if latest_seq_for_cleanup == dispatch_seq:
                    latest_ctx["completed_at"] = datetime.now(timezone.utc)
                    latest_ctx["completed_status"] = status
                    latest_ctx["completed_reason"] = reason
                elif (
                    latest_seq_for_cleanup > dispatch_seq
                    and pending_count <= 0
                    and latest_ctx.get("completed_at")
                ):
                    _latest_queue_task_ctx.pop(conversation_id, None)
            if status == "cancelled" and reason in _QUEUE_CANCEL_NOTICE_REASONS:
                # 防刷屏按会话去重，不再按 pending_count 一刀切（见 _should_send_queue_cancel_notice）。
                # high_priority / reply_to_bot 在本函数体后面才赋值，但这个回调只会在
                # dispatcher.submit(...) 之后触发，闭包取值发生在调用时，所以已绑定。
                notice_directed = bool(high_priority or reply_to_bot)
                notice_conversation = str(getattr(dispatch, "conversation_id", conversation_id))
                allow_notice, notice_gate = _should_send_queue_cancel_notice(
                    conversation_id=notice_conversation,
                    reason=reason,
                    directed=notice_directed,
                    dedup_window_seconds=queue_cfg.get(
                        "cancel_notice_dedup_seconds",
                        _QUEUE_CANCEL_NOTICE_DEDUP_DEFAULT_SECONDS,
                    ),
                )
                if not allow_notice:
                    engine.logger.info(
                        "queue_error_notice_skip | trace=%s | conversation=%s | reason=%s "
                        "| gate=%s | directed=%s | pending=%d",
                        dispatch_trace,
                        notice_conversation,
                        reason,
                        notice_gate,
                        notice_directed,
                        pending_count,
                    )
                    return
                engine.logger.info(
                    "queue_error_notice_send | trace=%s | conversation=%s | reason=%s "
                    "| gate=%s | directed=%s | pending=%d",
                    dispatch_trace,
                    notice_conversation,
                    reason,
                    notice_gate,
                    notice_directed,
                    pending_count,
                )
                msg = Message()
                runtime_send_opts = _resolve_runtime_send_options()
                msg += _build_reply_prefix(
                    event=event,
                    quote_message_id=str(getattr(event, "message_id", "") or ""),
                    sender_user_id=str(event.get_user_id()),
                    enable_quote=bool(runtime_send_opts["reply_with_quote"]),
                    enable_at=bool(runtime_send_opts["reply_with_at"]),
                )
                msg += Message(_queue_cancel_notice_text(reason))
                await _safe_send(bot=bot, event=event, message=msg)

        high_priority = bool(payload.mentioned or payload.is_private or text.startswith("/"))
        legacy_cancel_only_high_priority = bool(queue_cfg.get("cancel_previous_only_high_priority", True))
        cancel_mode_raw = normalize_text(str(queue_cfg.get("cancel_previous_mode", ""))).lower()
        if cancel_mode_raw in {"always", "high_priority", "interrupt"}:
            cancel_mode = cancel_mode_raw
        elif "cancel_previous_only_high_priority" in queue_cfg and not legacy_cancel_only_high_priority:
            # 兼容旧配置：旧参数显式关闭“仅高优先级打断”时，等价于 always。
            cancel_mode = "always"
        else:
            # 新默认：不因普通新消息打断当前任务，避免同会话乱序与上下文撕裂
            cancel_mode = "interrupt"

        reply_to_bot = bool(
            bool(payload.reply_to_user_id)
            and str(payload.reply_to_user_id) == str(payload.bot_id)
        )
        interrupt_force_enabled = bool(queue_cfg.get("cancel_previous_on_interrupt_request", True))
        force_cancel_previous = False
        cancel_previous_reason = "cancelled_by_new_trace"
        if cancel_mode == "always":
            allow_cancel_previous = True
        elif cancel_mode == "high_priority":
            allow_cancel_previous = bool(high_priority or reply_to_bot)
        else:
            allow_cancel_previous = _looks_like_cancel_previous_request(text)
            if allow_cancel_previous:
                force_cancel_previous = interrupt_force_enabled
                cancel_previous_reason = "cancelled_by_interrupt_request"
                engine.logger.info(
                    "queue_interrupt_requested | trace=%s | conversation=%s | text=%s",
                    payload.trace_id,
                    payload.conversation_id,
                    clip_text(text, 80),
                )
        if not allow_cancel_previous and bool(queue_cfg.get("smart_interrupt_enable", True)):
            previous_ctx = _latest_queue_task_ctx.get(conversation_id, {})
            previous_uid = str(previous_ctx.get("user_id", "")) if isinstance(previous_ctx, dict) else ""
            previous_text = str(previous_ctx.get("text", "")) if isinstance(previous_ctx, dict) else ""
            smart_interrupt, smart_reason = engine.should_interrupt_previous_task(
                message=payload,
                previous_user_id=previous_uid,
                previous_text=previous_text,
                pending_count=int(payload.queue_depth),
                high_priority=high_priority,
                reply_to_bot=reply_to_bot,
            )
            if smart_interrupt:
                allow_cancel_previous = True
                force_cancel_previous = interrupt_force_enabled
                cancel_previous_reason = "cancelled_by_smart_interrupt"
                engine.logger.info(
                    "queue_smart_interrupt | trace=%s | conversation=%s | reason=%s | prev_user=%s | user=%s | text=%s",
                    payload.trace_id,
                    payload.conversation_id,
                    smart_reason,
                    previous_uid or "-",
                    payload.user_id,
                    clip_text(text, 80),
                )

        engine.logger.debug(
            "queue_cancel_policy | trace=%s | mode=%s | allow=%s | force=%s | reason=%s | high_priority=%s | reply_to_bot=%s | text=%s",
            payload.trace_id,
            cancel_mode,
            allow_cancel_previous,
            force_cancel_previous,
            cancel_previous_reason,
            high_priority,
            reply_to_bot,
            clip_text(text, 80),
        )
        dispatch_interruptible = not _looks_like_sticker_learning_request(
            text=text,
            raw_segments=raw_segments,
            reply_media_segments=reply_media_segments,
        )
        engine.logger.info(
            "queue_submit | trace=%s | conversation=%s | seq=%d | pending_before=%d | high_priority=%s | interruptible=%s",
            payload.trace_id,
            payload.conversation_id,
            int(payload.seq or 0),
            int(payload.queue_depth or 0),
            high_priority,
            dispatch_interruptible,
        )
        dispatch_result = await dispatcher.submit(
            conversation_id=conversation_id,
            seq=seq,
            created_at=payload.timestamp,
            process=process,
            send=send_response,
            high_priority=high_priority,
            allow_cancel_previous=allow_cancel_previous,
            interruptible=dispatch_interruptible,
            process_timeout_seconds=process_timeout_override,
            trace_id=payload.trace_id,
            send_overload_notice=send_overload_notice,
            on_complete=on_dispatch_complete,
            force_cancel_previous=force_cancel_previous,
            cancel_previous_reason=cancel_previous_reason,
        )
        if dispatch_result.status == "queued":
            _latest_queue_task_ctx[conversation_id] = {
                "seq": seq,
                "trace_id": payload.trace_id,
                "user_id": payload.user_id,
                "text": text,
                "timestamp": payload.timestamp,
                "high_priority": high_priority,
                "reply_to_bot": reply_to_bot,
            }
        if dispatch_result.status == "cancelled":
            engine.logger.info(
                "queue_cancelled | trace=%s | conversation=%s | seq=%d | reason=%s",
                dispatch_result.trace_id,
                dispatch_result.conversation_id,
                dispatch_result.seq,
                dispatch_result.reason,
            )

    @notice_router.handle()
    async def handle_notice(bot: Bot, event: Event) -> None:
        _log_qq_generic_event(kind="qq_notice", event=event, bot_id=str(bot.self_id))
        payload = _event_to_dict(event)
        notice_type = normalize_text(str(payload.get("notice_type", ""))).lower()
        sub_type = normalize_text(str(payload.get("sub_type", ""))).lower()
        group_id = str(payload.get("group_id", "")).strip()
        user_id = str(payload.get("user_id", "")).strip()
        operator_id = str(payload.get("operator_id", "")).strip()
        bot_id = str(bot.self_id)

        # ── 成员加群 ──
        if notice_type == "group_increase":
            nickname = ""
            try:
                info = await call_napcat_bot_api(bot, "get_stranger_info", user_id=int(user_id))
                nickname = str((info or {}).get("nickname", "")).strip()
            except Exception:
                pass
            display = nickname or f"用户{user_id}"
            _log.info("group_increase | group=%s | user=%s (%s) | sub=%s | operator=%s",
                       group_id, user_id, display, sub_type, operator_id)
            if hasattr(engine, "memory") and group_id:
                engine.memory.add_message(
                    conversation_id=f"group_{group_id}",
                    user_id="system",
                    user_name="系统通知",
                    content=f"[系统] {display}({user_id}) 加入了群聊" + (f"（由 {operator_id} 邀请）" if operator_id and operator_id != user_id else ""),
                    role="system",
                )
            if hasattr(engine, "affinity") and user_id:
                engine.affinity.get_user(user_id)
                if nickname:
                    engine.affinity.get_user(user_id).nickname = nickname

        # ── 成员退群/被踢 ──
        elif notice_type == "group_decrease":
            _log.info("group_decrease | group=%s | user=%s | sub=%s | operator=%s",
                       group_id, user_id, sub_type, operator_id)
            action_text = "被移出群聊" if sub_type == "kick" else "退出了群聊"
            if sub_type == "kick" and operator_id:
                action_text += f"（操作者: {operator_id}）"
            if hasattr(engine, "memory") and group_id:
                engine.memory.add_message(
                    conversation_id=f"group_{group_id}",
                    user_id="system",
                    user_name="系统通知",
                    content=f"[系统] {user_id} {action_text}",
                    role="system",
                )

        # ── 管理员变动 ──
        elif notice_type == "group_admin":
            action = "被设为管理员" if sub_type == "set" else "被取消管理员"
            _log.info("group_admin | group=%s | user=%s | action=%s", group_id, user_id, action)
            if hasattr(engine, "memory") and group_id:
                engine.memory.add_message(
                    conversation_id=f"group_{group_id}",
                    user_id="system",
                    user_name="系统通知",
                    content=f"[系统] {user_id} {action}",
                    role="system",
                )

        # ── 群禁言 ──
        elif notice_type == "group_ban":
            duration = int(payload.get("duration", 0))
            if sub_type == "ban" and duration > 0:
                action = f"被禁言 {duration} 秒"
            elif sub_type == "lift_ban":
                action = "被解除禁言"
            else:
                action = "禁言状态变更"
            _log.info("group_ban | group=%s | user=%s | operator=%s | action=%s | duration=%d",
                       group_id, user_id, operator_id, action, duration)
            if hasattr(engine, "memory") and group_id:
                engine.memory.add_message(
                    conversation_id=f"group_{group_id}",
                    user_id="system",
                    user_name="系统通知",
                    content=f"[系统] {user_id} {action}" + (f"（操作者: {operator_id}）" if operator_id else ""),
                    role="system",
                )

        # ── 消息撤回 ──
        elif notice_type == "group_recall":
            message_id = str(payload.get("message_id", "")).strip()
            _log.info("group_recall | group=%s | user=%s | operator=%s | msg_id=%s",
                       group_id, user_id, operator_id, message_id)
            if hasattr(engine, "memory") and group_id:
                engine.memory.add_message(
                    conversation_id=f"group_{group_id}",
                    user_id="system",
                    user_name="系统通知",
                    content=f"[系统] {user_id} 撤回了一条消息" + (f"（由 {operator_id} 操作）" if operator_id and operator_id != user_id else ""),
                    role="system",
                )

        elif notice_type == "friend_recall":
            message_id = str(payload.get("message_id", "")).strip()
            _log.info("friend_recall | user=%s | msg_id=%s", user_id, message_id)

        # ── 戳一戳 ──
        elif notice_type == "notify" and sub_type == "poke":
            target_id = str(payload.get("target_id", "")).strip()
            _log.info("poke | group=%s | user=%s -> target=%s", group_id, user_id, target_id)
            # 如果戳的是bot，可以回应
            if target_id == bot_id and group_id:
                try:
                    await call_napcat_bot_api(bot, "group_poke", group_id=int(group_id), user_id=int(user_id))
                except Exception:
                    pass

        # ── 群文件上传 ──
        elif notice_type == "group_upload":
            file_info = payload.get("file", {})
            file_name = str(file_info.get("name", "")).strip() if isinstance(file_info, dict) else ""
            file_size = int(file_info.get("size", 0)) if isinstance(file_info, dict) else 0
            _log.info("group_upload | group=%s | user=%s | file=%s | size=%d",
                       group_id, user_id, file_name, file_size)

        # ── 群名片变更 ──
        elif notice_type == "group_card":
            card_new = str(payload.get("card_new", "")).strip()
            card_old = str(payload.get("card_old", "")).strip()
            _log.info("group_card | group=%s | user=%s | old=%s | new=%s",
                       group_id, user_id, card_old, card_new)

        # ── 好友添加 ──
        elif notice_type == "friend_add":
            _log.info("friend_add | user=%s", user_id)
            if hasattr(engine, "affinity") and user_id:
                engine.affinity.get_user(user_id)
                engine.affinity.add_affinity(user_id, 5.0, "new_friend")

    @request_router.handle()
    async def handle_request(bot: Bot, event: Event) -> None:
        _log_qq_generic_event(kind="qq_request", event=event, bot_id=str(bot.self_id))
        payload = _event_to_dict(event)
        request_type = normalize_text(str(payload.get("request_type", ""))).lower()

        # 从配置读取自动接受开关
        request_cfg = {}
        if engine and isinstance(getattr(engine, "config", None), dict):
            request_cfg = engine.config.get("request", {}) or {}
        auto_accept_friend = bool(request_cfg.get("auto_accept_friend", True))
        auto_accept_group_invite = bool(request_cfg.get("auto_accept_group_invite", False))

        # 自动同意好友请求（可配置）
        if request_type == "friend" and auto_accept_friend:
            flag = str(payload.get("flag", "")).strip()
            user_id = str(payload.get("user_id", "")).strip()
            if flag:
                try:
                    await bot.set_friend_add_request(flag=flag, approve=True)
                    _log.info(
                        "auto_accept_friend | bot=%s | user=%s | flag=%s",
                        bot.self_id, user_id, flag[:40],
                    )
                except Exception as exc:
                    _log.warning(
                        "auto_accept_friend_failed | bot=%s | user=%s | error=%s",
                        bot.self_id, user_id, str(exc)[:120],
                    )
        # 自动同意加群邀请（默认关闭，需手动开启）
        if request_type == "group" and auto_accept_group_invite:
            flag = str(payload.get("flag", "")).strip()
            sub_type = normalize_text(str(payload.get("sub_type", ""))).lower()
            group_id = str(payload.get("group_id", "")).strip()
            user_id = str(payload.get("user_id", "")).strip()
            if flag and sub_type == "invite":
                try:
                    await bot.set_group_add_request(flag=flag, sub_type="invite", approve=True)
                    _log.info(
                        "auto_accept_group_invite | bot=%s | group=%s | inviter=%s",
                        bot.self_id, group_id, user_id,
                    )
                except Exception as exc:
                    _log.warning(
                        "auto_accept_group_invite_failed | bot=%s | group=%s | error=%s",
                        bot.self_id, group_id, str(exc)[:120],
                    )

    @meta_router.handle()
    async def handle_meta(bot: Bot, event: Event) -> None:
        _log_qq_generic_event(kind="qq_meta", event=event, bot_id=str(bot.self_id))
        payload = _event_to_dict(event)
        meta_event_type = normalize_text(str(payload.get("meta_event_type", ""))).lower()
        sub_type = normalize_text(str(payload.get("sub_type", ""))).lower()
        if meta_event_type == "lifecycle" and sub_type == "connect":
            asyncio.create_task(_run_login_backlog_import(reason="meta_connect", bot=bot))



# ── 辅助函数 (拆分至 app_helpers.py) ──
import app_helpers as _app_helpers

_app_helpers.bind_runtime_dependencies(
    _FFMPEG_BIN=_FFMPEG_BIN,
    _FFPROBE_BIN=_FFPROBE_BIN,
    _check_bot_send_suspended=_check_bot_send_suspended,
    _check_group_send_block=_check_group_send_block,
    _generate_video_thumbnail=_generate_video_thumbnail,
    _is_hard_send_channel_error=_is_hard_send_channel_error,
    _is_payload_send_error=_is_payload_send_error,
    _is_transient_send_error=_is_transient_send_error,
    # 新加的发送错误判定也必须在这里注册。app_helpers.py 靠这份注入拿 app.py 的符号
    # （见 CLAUDE.md 的循环导入规避说明），漏注册的表现是运行时 NameError，
    # 而且只在真实发送路径上炸 —— 单独测 app.py 里的函数是测不出来的。
    _is_unretryable_send_error=_is_unretryable_send_error,
    _maybe_block_group_send_on_error=_maybe_block_group_send_on_error,
    _resume_bot_send=_resume_bot_send,
    _suspend_bot_send=_suspend_bot_send,
)
from app_helpers import *  # noqa: F401, F403
