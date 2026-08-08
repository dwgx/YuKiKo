"""Phase 2 平台主路径：OneBot11Adapter + WebUI 挂同一 uvicorn，替代 NoneBot。

配置 `platform.onebot11.primary: true` 时，main.py 走此路径：
- OneBot11Adapter 的 WS（/onebot/v11/ws）挂 uvicorn，NapCat 反向连接不变。
- 事件 → `engine.handle_message` → 回复 → `adapter.send_by_session`。
- WebUI（init_webui + SPA）与 WS 同端口。
- 不 init NoneBot。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4（2）（4）。
"""

from __future__ import annotations

import logging
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
    from core.platform.components import Image, MessageChain, Plain, Record, Video
    from core.queue import GroupQueueDispatcher

    dispatcher = GroupQueueDispatcher(
        engine.config.get("queue", {}) if isinstance(engine.config, dict) else {}
    )
    bot_id = str(cfg.get("bot_id", ""))
    voice_max_seconds = _platform_voice_max_seconds(engine)

    async def message_handler(event: dict[str, object]) -> None:
        try:
            payload = _event_to_engine_message(
                event,
                dispatcher=dispatcher,
                bot_id=bot_id,
                trace_builder=lambda c, s: f"platform-{c}-{s}",
            )
            response = await engine.handle_message(payload)
            components: list[Any] = []
            # EngineResponse 的文本字段是 reply_text（不是 text）。
            text = str(getattr(response, "reply_text", "") or "")
            if text:
                components.append(Plain(text))
            # 语音：点歌/音频产物 → 转 silk 发 record segment。
            record_ref = await _resolve_record_ref(response, voice_max_seconds)
            if record_ref:
                components.append(Record(file=record_ref))
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
                chain = MessageChain(components)
                await adapter.send_by_session(
                    str(event.get("conversation_id", "")), chain
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
