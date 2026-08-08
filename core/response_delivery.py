"""传输无关的回复发送核心（架构收敛任务 B + E4）。

把「语义拆分 → 限流/熔断/暂停 → 媒体 → 语音 silk 分段」收成单一发送管线，
供所有平台路径复用。调用方只提供一个窄接口：

    async def send(chain: MessageChain) -> bool

发送保护（token-bucket 限流、按群熔断、bot 级暂停、失败标记）由本核心统一实现；
调用方无需自行拼装。点歌语音 4 特性（silk 源互换 / music_force_full /
music_disable_split / 裁剪兜底+整段去重）也已从 app.py 迁入 `_send_voice`。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4（2）（4）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from core.platform.components import Image, MessageChain, Plain, Record, Video

_log = logging.getLogger("yukiko.response_delivery")

SendFn = Callable[[Any], Awaitable[bool]]


def _resolve_local_audio_path(audio_file: str) -> Path | None:
    """把 `response.audio_file` 解析成本地文件路径；远程/内联源返回 None。"""
    audio_source = str(audio_file or "").strip()
    if not audio_source:
        return None
    audio_l = audio_source.lower()
    if audio_l.startswith("file://"):
        try:
            candidate = Path(audio_source[len("file://") :]).expanduser().resolve()
            if candidate.exists() and candidate.is_file():
                return candidate
        except Exception:
            return None
        return None
    if audio_l.startswith(("http://", "https://", "base64://", "data:")):
        return None
    try:
        candidate = Path(audio_source).expanduser().resolve()
        if candidate.exists() and candidate.is_file():
            return candidate
    except Exception:
        return None
    return None


async def _delivery_sleep(seconds: float) -> None:
    """默认限流等待钩子；测试/平台路径可注入替换。"""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _mark_delivery_failure(group_id: int, bot_id: str, reason: str) -> None:
    """发送失败时标记按群熔断 + bot 暂停（保守回退）。"""
    from datetime import UTC, datetime, timedelta

    from app import _mark_group_send_block, _suspend_bot_send

    now = datetime.now(UTC)
    if group_id > 0:
        _mark_group_send_block(group_id, now + timedelta(seconds=180), reason)
    _suspend_bot_send(bot_id, 120, reason)


def _resolve_send_options(config: Any) -> dict[str, Any]:
    """从 config 解析发送相关选项（与 app.py `_resolve_runtime_send_options` 对齐）。"""
    from app import _resolve_send_rate_profile
    from utils.text import normalize_text

    cfg = config if isinstance(config, dict) else {}
    bot_cfg = cfg.get("bot", {})
    if not isinstance(bot_cfg, dict):
        bot_cfg = {}
    chat_split_cfg = cfg.get("chat_split", {})
    if not isinstance(chat_split_cfg, dict):
        chat_split_cfg = {}
    control_cfg = cfg.get("control", {})
    if not isinstance(control_cfg, dict):
        control_cfg = {}
    max_per_window, window_seconds, warn_threshold, rate_enable = _resolve_send_rate_profile(cfg)
    return {
        "send_rate_max_per_window": max_per_window,
        "send_rate_window_seconds": window_seconds,
        "send_rate_warn_threshold": warn_threshold,
        "send_rate_enable": rate_enable,
        "voice_send_max_seconds": max(0, int(bot_cfg.get("voice_send_max_seconds", 60) or 60)),
        "voice_send_try_full_first": bool(bot_cfg.get("voice_send_try_full_first", False)),
        "voice_send_split_enable": bool(bot_cfg.get("voice_send_split_enable", True)),
        "voice_send_split_max_segments": max(
            1, min(20, int(bot_cfg.get("voice_send_split_max_segments", 8) or 8))
        ),
        "voice_send_music_force_full": bool(bot_cfg.get("voice_send_music_force_full", False)),
        "voice_send_music_disable_split": bool(bot_cfg.get("voice_send_music_disable_split", False)),
        "multi_reply_enable": bool(bot_cfg.get("multi_reply_enable", True)),
        "multi_reply_max_lines": max(1, int(bot_cfg.get("multi_reply_max_lines", 1) or 1)),
        "multi_reply_max_chars": max(160, int(bot_cfg.get("multi_reply_max_chars", 520) or 520)),
        "multi_reply_max_chunks": max(1, int(bot_cfg.get("multi_reply_max_chunks", 4) or 4)),
        "chat_split_mode": normalize_text(
            str(chat_split_cfg.get("mode", control_cfg.get("split_mode", "semantic")))
        ).lower()
        or "semantic",
    }


def build_send_guard(
    config: Any,
    sender: SendFn,
    *,
    conversation_id: str,
    group_id: int,
    bot_id: str,
    sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    mark_failure_fn: Callable[[int, str, str], None] | None = None,
) -> SendFn:
    """构造发送保护闭包：token-bucket 限流 + 按群熔断 + bot 暂停 + 失败标记。

    `sender` 是调用方传入的窄接口；`sleep_fn` / `mark_failure_fn` 允许平台路径
    注入自己的等待与失败标记实现（测试与钩子共用）。
    """
    from app import (
        _check_bot_send_suspended,
        _check_group_send_block,
        _get_send_bucket,
        _resolve_send_rate_profile,
    )

    max_per_window, window_seconds, warn_threshold, rate_enable = _resolve_send_rate_profile(config)
    sleep = sleep_fn or _delivery_sleep
    mark_failure = mark_failure_fn or _mark_delivery_failure

    async def guard_send(chain: Any) -> bool:
        suspended, suspend_reason = _check_bot_send_suspended(bot_id)
        if suspended:
            _log.warning(
                "deliver_send_skipped_bot_suspended | bot=%s | conversation=%s | reason=%s",
                bot_id or "-",
                conversation_id,
                suspend_reason,
            )
            return False
        blocked, block_reason = _check_group_send_block(group_id)
        if blocked:
            _log.warning(
                "deliver_send_skipped_group_blocked | conversation=%s | reason=%s",
                conversation_id,
                block_reason,
            )
            return False
        if rate_enable:
            bucket = _get_send_bucket(
                conversation_id=conversation_id,
                group_id=group_id,
                max_per_window=max_per_window,
                refill_seconds=window_seconds,
                warn_threshold=warn_threshold,
            )
            wait_seconds, _rate_flag = bucket.reserve()
            if wait_seconds > 0:
                _log.warning(
                    "deliver_send_rate_limit_wait | conversation=%s | wait=%.2fs | used=%d/%d",
                    conversation_id,
                    wait_seconds,
                    bucket.used_in_window(),
                    bucket.capacity,
                )
                await sleep(wait_seconds)
        try:
            ok = await sender(chain)
        except Exception as exc:
            _log.warning("deliver_send_fail | conversation=%s | err=%s", conversation_id, exc)
            mark_failure(group_id, bot_id, str(exc))
            return False
        if not ok:
            _log.warning("deliver_send_rejected | conversation=%s", conversation_id)
            mark_failure(group_id, bot_id, "send_rejected")
        return ok

    return guard_send


def _resolve_image_urls(response: Any) -> list[str]:
    """合并 `image_url` 与 `image_urls`，去重后保持 image_url 优先。"""
    image_url = str(getattr(response, "image_url", "") or "")
    raw_urls = getattr(response, "image_urls", []) or []
    urls = [str(u) for u in raw_urls if str(u)]
    if image_url and image_url not in urls:
        urls.insert(0, image_url)
    return urls


async def _resolve_record_ref(response: Any, voice_max_seconds: int) -> str:
    """把 EngineResponse 的音频产物转成 OneBot record 的 file 引用（silk）。"""
    audio_file = str(getattr(response, "audio_file", "") or "")
    record_b64 = str(getattr(response, "record_b64", "") or "")
    if audio_file:
        try:
            from app import _silk_encode_for_record

            from core.napcat_compat import build_napcat_file_reference

            silk = await _silk_encode_for_record(Path(audio_file), voice_max_seconds)
            if silk is not None:
                return build_napcat_file_reference(silk)
        except Exception:
            _log.warning("deliver_voice_silk_fail | audio=%s", audio_file, exc_info=True)
    if record_b64:
        return f"base64://{record_b64}"
    return ""


async def _send_voice(
    response: Any,
    guard_send: SendFn,
    opts: dict[str, Any],
    *,
    conversation_id: str,
    is_music_voice_action: bool = False,
) -> bool:
    """发送语音产物：silk 源互换 → 整段直发 → 长音频分段 → 裁剪兜底（含发送保护）。

    覆盖点歌语音 4 特性（与 app.py 旧语音段行为等价）：
    - silk 源互换：输入是 .silk 时找同目录 sibling mp3 当探测/切分源；
    - music_force_full / music_disable_split：点歌强制整段直发 / 禁分段；
    - 裁剪兜底 + 整段去重：全部失败后裁到 max_seconds 再发，同一路径不重复发。
    返回是否至少成功发出一条 record。
    """
    audio_file = str(getattr(response, "audio_file", "") or "")
    record_b64 = str(getattr(response, "record_b64", "") or "")
    if not audio_file and not record_b64:
        return False
    from app import (
        _prepare_voice_audio_file,
        _probe_audio_duration_seconds_sync,
        _silk_encode_for_record,
        _split_voice_audio_file,
    )

    from core.napcat_compat import build_napcat_file_reference

    max_seconds = int(opts.get("voice_send_max_seconds", 60) or 60)
    split_enable = bool(opts.get("voice_send_split_enable", True))
    split_max_segments = int(opts.get("voice_send_split_max_segments", 8) or 8)
    try_full_first = bool(opts.get("voice_send_try_full_first", False))
    music_force_full = bool(opts.get("voice_send_music_force_full", False))
    music_disable_split = bool(opts.get("voice_send_music_disable_split", False))
    segment_seconds = max_seconds if max_seconds > 0 else 60

    local_path = _resolve_local_audio_path(audio_file)
    effective_audio_path = local_path
    source_is_silk = local_path is not None and local_path.suffix.lower() == ".silk"
    # silk 源互换：ffmpeg 无法切分 silk 且 NapCat 对 silk 时长识别差，
    # 同目录存在可切分的 sibling 音频时用它当探测/切分源（直发仍优先原 silk）。
    if source_is_silk and local_path is not None:
        for ext in (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"):
            candidate = local_path.with_suffix(ext)
            try:
                if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 1024:
                    effective_audio_path = candidate
                    _log.info(
                        "deliver_voice_silk_source_swap | conversation=%s | silk=%s | split_src=%s",
                        conversation_id,
                        local_path.name,
                        candidate.name,
                    )
                    break
            except Exception:
                continue

    duration = 0.0
    if effective_audio_path is not None:
        duration = await asyncio.to_thread(_probe_audio_duration_seconds_sync, effective_audio_path)
    is_long_audio = max_seconds > 0 and duration > float(max_seconds) + 0.8

    try_full_for_current = try_full_first
    split_enable_for_current = split_enable
    if is_music_voice_action and music_force_full:
        try_full_for_current = True
    if is_music_voice_action and music_disable_split:
        split_enable_for_current = False
    # silk 源互换后切分源是 sibling mp3：长音频只走分段，不整段直发（避免预编 60s silk）。
    if source_is_silk and effective_audio_path is not None and effective_audio_path != local_path:
        if is_long_audio and split_enable_for_current:
            try_full_for_current = False

    full_audio_uri = ""
    tried_full_direct = False
    sent_voice = False
    if try_full_for_current or not is_long_audio:
        # QQ 语音必须 silk 编码：只在"将直发"分支做整段编码——长音频若不直发（等切片）不白编。
        record_audio_path = effective_audio_path
        if effective_audio_path is not None:
            silk_candidate = await _silk_encode_for_record(effective_audio_path, max_seconds)
            if silk_candidate is not None:
                record_audio_path = silk_candidate
        full_audio_uri = build_napcat_file_reference(
            record_audio_path if record_audio_path is not None else audio_file
        )
        if full_audio_uri:
            tried_full_direct = True
            _log.info(
                "deliver_voice_try_full | conversation=%s | src=%s | duration=%.2fs | max=%ss | long=%s",
                conversation_id,
                effective_audio_path.name if effective_audio_path is not None else str(audio_file)[:80],
                duration,
                max_seconds,
                is_long_audio,
            )
            sent_voice = await guard_send(MessageChain([Record(file=full_audio_uri)]))
            if sent_voice:
                _log.info("deliver_voice_try_full_ok | conversation=%s", conversation_id)
            else:
                _log.warning("deliver_voice_try_full_fail | conversation=%s", conversation_id)

    # 长音频：整段发送失败（或未尝试）后按段切分逐条发送。
    if (
        not sent_voice
        and effective_audio_path is not None
        and is_long_audio
        and split_enable_for_current
    ):
        _log.info(
            "deliver_voice_split_start | conversation=%s | src=%s | duration=%.2fs | segment=%ss | max_segments=%d",
            conversation_id,
            effective_audio_path.name,
            duration,
            segment_seconds,
            split_max_segments,
        )
        split_parts = await _split_voice_audio_file(
            effective_audio_path,
            segment_seconds=segment_seconds,
            max_segments=split_max_segments,
        )
        if split_parts:
            sent_count = 0
            for part_idx, part_path in enumerate(split_parts, start=1):
                part_silk = await _silk_encode_for_record(part_path, segment_seconds)
                part_uri = build_napcat_file_reference(
                    part_silk if part_silk is not None else part_path
                )
                part_ok = await guard_send(MessageChain([Record(file=part_uri)]))
                if not part_ok:
                    _log.warning(
                        "deliver_voice_split_part_fail | conversation=%s | part=%d/%d | file=%s",
                        conversation_id,
                        part_idx,
                        len(split_parts),
                        part_path.name,
                    )
                    break
                sent_count += 1
            if sent_count > 0:
                sent_voice = True
                _log.info(
                    "deliver_voice_split_done | conversation=%s | sent=%d/%d",
                    conversation_id,
                    sent_count,
                    len(split_parts),
                )
        else:
            _log.warning(
                "deliver_voice_split_no_parts | conversation=%s | src=%s",
                conversation_id,
                audio_file,
            )

    # 兜底：按最大秒数裁剪后再发一条，避免整段/分段都失败。
    if not sent_voice:
        send_audio_path = effective_audio_path
        allow_trim_fallback = not (is_music_voice_action and music_force_full)
        if effective_audio_path is not None and max_seconds > 0 and allow_trim_fallback:
            prepared_path, prepared_duration, trimmed = await _prepare_voice_audio_file(
                effective_audio_path,
                max_seconds,
            )
            send_audio_path = prepared_path
            _log.info(
                "deliver_voice_prepare | conversation=%s | src=%s | send=%s | duration=%.2fs | max=%ss | trimmed=%s",
                conversation_id,
                effective_audio_path.name,
                prepared_path.name,
                prepared_duration,
                max_seconds,
                trimmed,
            )
        fallback_silk = None
        if send_audio_path is not None:
            fallback_silk = await _silk_encode_for_record(send_audio_path, max_seconds)
        fallback_uri = build_napcat_file_reference(
            fallback_silk
            if fallback_silk is not None
            else (send_audio_path if send_audio_path is not None else audio_file)
        )
        # 已完整尝试过同一路径则不重复发送。
        if fallback_uri and (not tried_full_direct or fallback_uri != full_audio_uri):
            sent_voice = await guard_send(MessageChain([Record(file=fallback_uri)]))
        elif not fallback_uri:
            _log.warning(
                "deliver_voice_file_uri_empty | conversation=%s | audio=%s",
                conversation_id,
                str(audio_file)[:120],
            )
    if not sent_voice and record_b64:
        sent_voice = await guard_send(MessageChain([Record(file=f"base64://{record_b64}")]))
    if not sent_voice:
        _log.warning("deliver_voice_send_fail | conversation=%s | src=%s", conversation_id, audio_file)
    return sent_voice


def _looks_like_music_cache_audio(audio_file: str) -> bool:
    """判断 audio 是否落在音乐缓存目录/命名（点歌语音策略的路径推断）。"""
    from utils.text import normalize_text

    audio_hint = normalize_text(audio_file).replace("\\", "/").lower()
    return bool(
        "/storage/cache/music/" in audio_hint
        or audio_hint.startswith("storage/cache/music/")
        or re.search(r"(?:^|/)(?:netease_|music_)[^/]*\.(?:mp3|m4a|wav|ogg|flac|silk)$", audio_hint)
    )


async def deliver_response(
    config: Any,
    response: Any,
    sender: SendFn,
    *,
    conversation_id: str,
    group_id: int,
    bot_id: str,
    sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    mark_failure_fn: Callable[[int, str, str], None] | None = None,
    is_music_voice_action: bool = False,
) -> bool:
    """发送 EngineResponse：语义拆分文本 + 限流/熔断/暂停 + 图片/视频 + 语音 silk 分段。

    `is_music_voice_action` 标记点歌语音（force_full / disable_split / silk 源互换生效）；
    未显式传入时按 audio 路径落在音乐缓存目录/命名推断。返回是否至少成功发出一条消息。
    传输无关：所有发送经调用方传入的 `sender(chain)` 落地。
    """
    from app import _get_send_bucket

    from core.chat_splitter import coalesce_for_rate_limit, split_semantic_text

    opts = _resolve_send_options(config)
    guard_send = build_send_guard(
        config,
        sender,
        conversation_id=conversation_id,
        group_id=group_id,
        bot_id=bot_id,
        sleep_fn=sleep_fn,
        mark_failure_fn=mark_failure_fn,
    )

    bucket = None
    if opts["send_rate_enable"]:
        bucket = _get_send_bucket(
            conversation_id=conversation_id,
            group_id=group_id,
            max_per_window=opts["send_rate_max_per_window"],
            refill_seconds=opts["send_rate_window_seconds"],
            warn_threshold=opts["send_rate_warn_threshold"],
        )

    text = str(getattr(response, "reply_text", "") or "")
    audio_file = str(getattr(response, "audio_file", "") or "")
    has_voice = bool(
        str(getattr(response, "record_b64", "") or "") or audio_file
    )
    video_url = str(getattr(response, "video_url", "") or "")

    # 点歌路径推断：audio 落在音乐缓存目录/命名时按点歌语音策略发送。
    if has_voice and not is_music_voice_action and audio_file and _looks_like_music_cache_audio(audio_file):
        is_music_voice_action = True
        _log.info(
            "deliver_voice_music_action_infer | conversation=%s | audio=%s",
            conversation_id,
            audio_file[:120],
        )

    def _chunks_for(current_text: str) -> list[str]:
        if not current_text:
            return []
        if video_url or not opts["multi_reply_enable"]:
            return [current_text]
        chunks = split_semantic_text(
            current_text,
            max_lines=opts["multi_reply_max_lines"],
            max_chars=opts["multi_reply_max_chars"],
            max_chunks=opts["multi_reply_max_chunks"],
        )
        if not chunks:
            chunks = [current_text]
        if bucket is not None and bucket.near_warn() and len(chunks) > 1:
            chunks = coalesce_for_rate_limit(
                chunks,
                max_chars=max(220, opts["multi_reply_max_chars"] + 80),
                short_chunk_chars=90,
            )
        return chunks

    # 文本：语义拆分逐条发送。语音路径下作为语音的文案前缀（app.py 顺序）。
    # 文本失败不中断：语音/媒体仍要发（NapCat 拒文本时点歌语音不应整个丢失）。
    delivered_any = False
    for chunk in _chunks_for(text):
        if await guard_send(MessageChain([Plain(chunk)])):
            delivered_any = True
        else:
            _log.warning("deliver_text_chunk_fail | conversation=%s", conversation_id)

    # 语音：silk 源互换 / 整段直发 / 长音频分段 / 裁剪兜底（含发送保护）。
    if has_voice and await _send_voice(
        response,
        guard_send,
        opts,
        conversation_id=conversation_id,
        is_music_voice_action=is_music_voice_action,
    ):
        delivered_any = True

    # 媒体：图片 + 视频一条链。
    media: list[Any] = []
    for url in _resolve_image_urls(response)[:4]:
        media.append(Image(file=url))
    if video_url:
        media.append(Video(file=video_url))
    if media and await guard_send(MessageChain(media)):
        delivered_any = True
    return delivered_any
