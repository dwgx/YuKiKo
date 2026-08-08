"""Phase 2 平台主路径：OneBot11Adapter + WebUI 挂同一 uvicorn，替代 NoneBot。

配置 `platform.onebot11.primary: true` 时，main.py 走此路径：
- OneBot11Adapter 的 WS（/onebot/v11/ws）挂 uvicorn，NapCat 反向连接不变。
- 事件 → `engine.handle_message` → 回复 → `adapter.send_by_session`。
- WebUI（init_webui + SPA）与 WS 同端口。
- 不 init NoneBot。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4（2）（4）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

_log = logging.getLogger("yukiko.platform.primary")


def _build_app(engine: Any) -> Any:  # type: ignore[no-untyped-def]
    """构建 FastAPI 应用：OneBot WS 路由 + WebUI + SPA。"""
    from core.platform.onebot11 import OneBot11Adapter

    cfg: dict[str, object] = {}
    if isinstance(engine.config, dict):
        platform_cfg = engine.config.get("platform", {})
        if isinstance(platform_cfg, dict) and isinstance(platform_cfg.get("onebot11"), dict):
            cfg = dict(platform_cfg["onebot11"])  # type: ignore[arg-type]
    adapter = OneBot11Adapter(
        {
            "host": cfg.get("host", "0.0.0.0"),
            "port": int(cfg.get("port", 8081)),
            "access_token": cfg.get("access_token", ""),
            "bot_id": cfg.get("bot_id", ""),
        }
    )
    _log.info("platform_primary_adapter | ws=/onebot/v11/ws")

    app = FastAPI()

    async def _onebot_ws(websocket: Any) -> None:
        await adapter.handle_starlette_ws(websocket)

    # FastAPI 0.141 的 @app.websocket 在真实 uvicorn 下对 ws 升级会提前 close(403)
    # （与 starlette 1.3 / uvicorn 0.50 的兼容问题，TestClient 测不出，只影响真实连接）。
    # 改用 Starlette WebSocketRoute 挂同端口：HTTP 走 FastAPI（WebUI），ws 走此路由。
    from starlette.routing import WebSocketRoute

    app.router.routes.append(WebSocketRoute("/onebot/v11/ws", _onebot_ws))

    from core.webui import init_webui

    app.include_router(init_webui(engine))

    _webui_dist = Path(__file__).resolve().parents[2] / "webui" / "dist"
    _webui_index = _webui_dist / "index.html"
    _webui_assets = _webui_dist / "assets"
    if _webui_assets.is_dir():
        app.mount("/webui/assets", StaticFiles(directory=str(_webui_assets)), name="webui-assets")

    async def _webui_missing() -> Response:
        return Response(
            "WebUI 静态页面未构建。请先 cd webui && npm install && npm run build",
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/webui/{path:path}")
    async def _webui_spa(path: str) -> Response:
        if ".." in path:
            return Response("Not found", status_code=404)
        fp = _webui_dist / path
        if fp.is_file():
            return FileResponse(fp)
        if _webui_index.exists():
            return FileResponse(_webui_index)
        return await _webui_missing()

    @app.get("/webui")
    async def _webui_root() -> Response:
        if _webui_index.exists():
            return FileResponse(_webui_index)
        return await _webui_missing()

    # 挂 bridge：事件 → engine 消息管线 → 回复 → adapter 发送。
    _wire_bridge(engine, adapter, cfg)
    return app


def _platform_voice_max_seconds(engine: Any) -> int:
    if isinstance(engine.config, dict):
        bot_cfg = engine.config.get("bot", {})
        if isinstance(bot_cfg, dict):
            return max(0, int(bot_cfg.get("voice_send_max_seconds", 60) or 60))
    return 60


def _platform_voice_options(engine: Any) -> dict[str, Any]:
    """平台路径语音发送选项：与 app.py `_resolve_runtime_send_options` 的 `bot.*` 对齐。"""
    bot_cfg = engine.config.get("bot", {}) if isinstance(engine.config, dict) else {}
    if not isinstance(bot_cfg, dict):
        bot_cfg = {}
    return {
        "voice_send_max_seconds": _platform_voice_max_seconds(engine),
        "voice_send_try_full_first": bool(bot_cfg.get("voice_send_try_full_first", False)),
        "voice_send_split_enable": bool(bot_cfg.get("voice_send_split_enable", True)),
        "voice_send_split_max_segments": max(
            1, min(20, int(bot_cfg.get("voice_send_split_max_segments", 8) or 8))
        ),
    }


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


async def _platform_sleep(seconds: float) -> None:
    """可替换的 sleep 钩子：让限流等待在测试里可打桩。"""
    if seconds > 0:
        await asyncio.sleep(seconds)


def _mark_platform_send_failure(group_id: int, bot_id: str, reason: str) -> None:
    """发送被 NapCat 拒绝时标记群熔断 + bot 暂停（app.py `_safe_send` 的保守回退）。"""
    from datetime import UTC, datetime, timedelta

    from app import _mark_group_send_block, _suspend_bot_send

    now = datetime.now(UTC)
    if group_id > 0:
        _mark_group_send_block(group_id, now + timedelta(seconds=180), reason)
    _suspend_bot_send(bot_id, 120, reason)


def _maybe_mark_platform_send_block(group_id: int, bot_id: str, error_text: str) -> None:
    """按 app.py 同款错误模式（299 限频 / 120 禁言）标记群熔断与 bot 暂停。"""
    from datetime import UTC, datetime, timedelta

    from app import _mark_group_send_block, _suspend_bot_send

    err_text = str(error_text or "")
    err_lower = err_text.lower()
    now = datetime.now(UTC)
    is_rate_limited = bool(
        re.search(r'"result"\s*:\s*299\b', err_text)
        or re.search(r"\bresult\s*[:=]\s*299\b", err_lower)
        or "rate limit" in err_lower
        or "发送频率" in err_text
        or "过快" in err_text
    )
    if is_rate_limited:
        if group_id > 0:
            _mark_group_send_block(
                group_id, now + timedelta(seconds=65), "platform_send_error_299_rate_limit"
            )
        _suspend_bot_send(bot_id, 65, "platform_send_error_299_rate_limit")
        return
    is_forbidden = bool(
        re.search(r'"result"\s*:\s*120\b', err_text)
        or re.search(r"\bresult\s*[:=]\s*120\b", err_lower)
        or "forbidden" in err_lower
        or "mute" in err_lower
        or "禁言" in err_text
    )
    if is_forbidden:
        if group_id > 0:
            _mark_group_send_block(
                group_id, now + timedelta(seconds=180), "platform_send_error_120_or_forbidden"
            )
        _suspend_bot_send(bot_id, 120, "platform_send_error_120_or_forbidden")


def _build_send_guard(
    engine: Any,
    adapter: Any,
    *,
    conversation_id: str,
    group_id: int,
    bot_id: str,
) -> Any:
    """构造发送保护闭包：token-bucket 限流 + 按群熔断 + bot 级发送暂停。"""
    from app import (
        _check_bot_send_suspended,
        _check_group_send_block,
        _get_send_bucket,
        _resolve_send_rate_profile,
    )

    config = engine.config if isinstance(engine.config, dict) else {}
    max_per_window, window_seconds, warn_threshold, rate_enable = _resolve_send_rate_profile(
        config
    )

    async def guard_send(chain: Any) -> bool:
        suspended, suspend_reason = _check_bot_send_suspended(bot_id)
        if suspended:
            _log.warning(
                "platform_send_skipped_bot_suspended | bot=%s | conversation=%s | reason=%s",
                bot_id or "-",
                conversation_id,
                suspend_reason,
            )
            return False
        blocked, block_reason = _check_group_send_block(group_id)
        if blocked:
            _log.warning(
                "platform_send_skipped_group_blocked | conversation=%s | reason=%s",
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
                    "platform_send_rate_limit_wait | conversation=%s | wait=%.2fs | used=%d/%d",
                    conversation_id,
                    wait_seconds,
                    bucket.used_in_window(),
                    bucket.capacity,
                )
                await _platform_sleep(wait_seconds)
        try:
            ok = await adapter.send_by_session(conversation_id, chain)
        except Exception as exc:
            _log.warning("platform_send_fail | conversation=%s | err=%s", conversation_id, exc)
            _maybe_mark_platform_send_block(group_id, bot_id, str(exc))
            return False
        if not ok:
            _log.warning("platform_send_rejected | conversation=%s", conversation_id)
            _mark_platform_send_failure(group_id, bot_id, "platform_send_rejected")
        return ok

    return guard_send


async def _send_voice_response(
    response: Any,
    guard_send: Any,
    *,
    conversation_id: str,
    voice_opts: dict[str, Any],
) -> None:
    """发送语音产物：短音频单条 silk record；长音频按段切分逐段发送（含发送保护）。"""
    audio_file = str(getattr(response, "audio_file", "") or "")
    record_b64 = str(getattr(response, "record_b64", "") or "")
    if not audio_file and not record_b64:
        return
    from app import _probe_audio_duration_seconds_sync, _silk_encode_for_record, _split_voice_audio_file

    from core.napcat_compat import build_napcat_file_reference
    from core.platform.components import MessageChain, Record

    max_seconds = int(voice_opts.get("voice_send_max_seconds", 60) or 60)
    split_enable = bool(voice_opts.get("voice_send_split_enable", True))
    split_max_segments = int(voice_opts.get("voice_send_split_max_segments", 8) or 8)
    try_full_first = bool(voice_opts.get("voice_send_try_full_first", False))

    local_path = _resolve_local_audio_path(audio_file)
    duration = 0.0
    if local_path is not None:
        duration = await asyncio.to_thread(_probe_audio_duration_seconds_sync, local_path)
    is_long_audio = max_seconds > 0 and duration > float(max_seconds) + 0.8

    sent_voice = False
    if local_path is not None and is_long_audio and split_enable:
        # 与 app.py 默认策略对齐：长音频默认直接走分段（try_full_first=False 时跳过整段直发）。
        if try_full_first:
            ref = await _resolve_record_ref(response, max_seconds)
            if ref:
                sent_voice = await guard_send(MessageChain([Record(file=ref)]))
        if not sent_voice:
            segment_seconds = max_seconds if max_seconds > 0 else 60
            split_parts = await _split_voice_audio_file(
                local_path,
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
                            "platform_voice_split_part_fail | conversation=%s | part=%d/%d | file=%s",
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
                        "platform_voice_split_done | conversation=%s | sent=%d/%d",
                        conversation_id,
                        sent_count,
                        len(split_parts),
                    )
            else:
                _log.warning(
                    "platform_voice_split_no_parts | conversation=%s | src=%s",
                    conversation_id,
                    audio_file,
                )
    if not sent_voice:
        ref = await _resolve_record_ref(response, max_seconds)
        if ref:
            sent_voice = await guard_send(MessageChain([Record(file=ref)]))
        elif record_b64:
            sent_voice = await guard_send(MessageChain([Record(file=f"base64://{record_b64}")]))
    if not sent_voice:
        _log.warning("platform_voice_send_fail | conversation=%s", conversation_id)


async def _resolve_record_ref(response: Any, voice_max_seconds: int) -> str:
    """把 EngineResponse 的音频产物转成 OneBot record 的 file 引用（silk）。"""
    audio_file = str(getattr(response, "audio_file", "") or "")
    record_b64 = str(getattr(response, "record_b64", "") or "")
    if audio_file:
        try:
            from app import _silk_encode_for_record

            from core.napcat_compat import build_napcat_file_reference

            silk = await _silk_encode_for_record(
                Path(audio_file), voice_max_seconds
            )
            if silk is not None:
                return build_napcat_file_reference(silk)
        except Exception:
            _log.warning("platform_voice_silk_fail | audio=%s", audio_file, exc_info=True)
    if record_b64:
        return f"base64://{record_b64}"
    return ""


def _wire_bridge(engine: Any, adapter: Any, cfg: dict[str, object]) -> None:
    """把 adapter 事件接进 engine 消息管线（与 NoneBot 主路径的 process/send 对齐）。"""
    from core.platform.bridge import _event_to_engine_message
    from core.platform.components import Image, MessageChain, Plain, Video
    from core.queue import GroupQueueDispatcher

    dispatcher = GroupQueueDispatcher(
        engine.config.get("queue", {}) if isinstance(engine.config, dict) else {}
    )
    bot_id = str(cfg.get("bot_id", ""))

    async def _platform_api_call(api: str, **kwargs: Any) -> Any:
        # 供工具层经 NapCat 调用 OneBot API（如 get_msg / set_group_card 等）。
        return await adapter._send_api(api, kwargs)

    async def message_handler(event: dict[str, object]) -> None:
        try:
            payload = _event_to_engine_message(
                event,
                dispatcher=dispatcher,
                bot_id=bot_id,
                trace_builder=lambda conversation_id, seq: f"platform-{conversation_id}-{seq}",
                api_call=_platform_api_call,
            )
            response = await engine.handle_message(payload)
            conversation_id = str(event.get("conversation_id", ""))
            group_id = int(event.get("group_id", 0) or 0)
            guard_send = _build_send_guard(
                engine,
                adapter,
                conversation_id=conversation_id,
                group_id=group_id,
                bot_id=bot_id,
            )
            # 静态内容：文本 + 图片 + 视频（不含语音），先发。
            components: list[Any] = []
            # EngineResponse 的文本字段是 reply_text（不是 text）。
            text = str(getattr(response, "reply_text", "") or "")
            if text:
                components.append(Plain(text))
            # 图片
            image_url = str(getattr(response, "image_url", "") or "")
            raw_urls = getattr(response, "image_urls", []) or []
            image_urls = [str(u) for u in raw_urls if str(u)]
            if image_url and image_url not in image_urls:
                image_urls.insert(0, image_url)
            for url in image_urls[:4]:
                components.append(Image(file=url))
            # 视频
            video_url = str(getattr(response, "video_url", "") or "")
            if video_url:
                components.append(Video(file=video_url))
            if components:
                await guard_send(MessageChain(components))
            # 语音：短音频单条 silk record；长音频按段切分逐条发送（含发送保护）。
            await _send_voice_response(
                response,
                guard_send,
                conversation_id=conversation_id,
                voice_opts=_platform_voice_options(engine),
            )
        except Exception:
            _log.warning("platform_msg_error", exc_info=True)

    adapter.message_handler = message_handler


def run_primary() -> None:
    """平台主路径入口：构建应用并 uvicorn 启动（阻塞）。"""
    import uvicorn
    from app import create_engine

    engine = create_engine()
    app = _build_app(engine)
    cfg: dict[str, object] = {}
    if isinstance(engine.config, dict):
        platform_cfg = engine.config.get("platform", {})
        if isinstance(platform_cfg, dict) and isinstance(platform_cfg.get("onebot11"), dict):
            cfg = dict(platform_cfg["onebot11"])  # type: ignore[arg-type]
    host = str(cfg.get("host", "127.0.0.1"))
    if host in ("0.0.0.0", "::"):
        host = "0.0.0.0"
    port = int(cfg.get("port", 8081))
    _log.info("platform_primary_start | host=%s | port=%d | ws=/onebot/v11/ws", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
