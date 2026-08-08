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
    """薄封装：委托统一发送核心 `core.response_delivery.build_send_guard`。

    保留此名字供既有调用方/回归测试使用；实现已收敛到 response_delivery。
    """
    from core.response_delivery import build_send_guard as _build_guard

    config = engine.config if isinstance(engine.config, dict) else {}

    async def sender(chain: Any) -> bool:
        return await adapter.send_by_session(conversation_id, chain)

    # 经闭包按调用时解析模块全局，让测试对 `_platform_sleep` /
    # `_mark_platform_send_failure` 的 patch 在 guard 构建之后依然生效。
    async def sleep_fn(seconds: float) -> None:
        await _platform_sleep(seconds)

    def mark_failure_fn(gid: int, bid: str, reason: str) -> None:
        # 兼容既有回归测试对旧 reason 标签的断言。
        if reason == "send_rejected":
            reason = "platform_send_rejected"
        _mark_platform_send_failure(gid, bid, reason)

    return _build_guard(
        config,
        sender,
        conversation_id=conversation_id,
        group_id=group_id,
        bot_id=bot_id,
        sleep_fn=sleep_fn,
        mark_failure_fn=mark_failure_fn,
    )


async def _resolve_record_ref(response: Any, voice_max_seconds: int) -> str:
    """薄封装：委托统一发送核心 `core.response_delivery._resolve_record_ref`。"""
    from core.response_delivery import _resolve_record_ref as _impl

    return await _impl(response, voice_max_seconds)


def _wire_bridge(engine: Any, adapter: Any, cfg: dict[str, object]) -> None:
    """把 adapter 事件接进 engine 消息管线（与 NoneBot 主路径的 process/send 对齐）。"""
    from core.platform.bridge import _event_to_engine_message
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
            from core.response_delivery import deliver_response

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
            config = engine.config if isinstance(engine.config, dict) else {}

            async def sender(chain: Any) -> bool:
                return await adapter.send_by_session(conversation_id, chain)

            # 统一发送核心：语义拆分文本 + 限流/熔断/暂停 + 图片/视频 + 语音 silk 分段。
            await deliver_response(
                config,
                response,
                sender,
                conversation_id=conversation_id,
                group_id=group_id,
                bot_id=bot_id,
                sleep_fn=_platform_sleep,
                mark_failure_fn=_mark_platform_send_failure,
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
