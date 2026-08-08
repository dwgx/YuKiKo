"""Phase 2 接线：把 OneBot11Adapter 接进 engine 消息管线。

事件 → 简化 EngineMessage → `engine.handle_message()` → 回复 → `adapter.send_by_session`。

这是 NoneBot 主路径之外的可选平台路径（配置 `platform.onebot11.enabled` 启用）。
完整替换 app.py 的 NoneBot 绑定（含媒体/回复/发送保护全量对齐）需真机验证后再切。
对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4（2）（4）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.engine_types import EngineMessage, EngineResponse
from core.platform.manager import PlatformManager
from core.platform.onebot11 import OneBot11Adapter

_log = logging.getLogger("yukiko.platform.bridge")


async def _resolve_platform_reply_context(
    api_call: Any, reply_to_message_id: str
) -> tuple[str, str, str, list[dict[str, Any]]]:
    """经平台 api_call 的 get_msg 解析被引用消息 → (user_id, user_name, text, media)。

    与 app.py 的 `_resolve_reply_context` 对齐：sender 取 user_id/card/nickname，
    message 段里提取文本与图片/视频/语音媒体（供 engine 缓存 reply 媒体）。
    无 api_call / 无回复 / 取不到数据时静默返回空，不阻断入站。
    """
    mid = str(reply_to_message_id or "").strip()
    if not api_call or not mid:
        return "", "", "", []
    payload: dict[str, Any] | None = None
    for value in (int(mid) if mid.isdigit() else mid, mid):
        try:
            response = await api_call("get_msg", message_id=value)
        except Exception:
            continue
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, dict):
            payload = data
            break
    if not isinstance(payload, dict):
        return "", "", "", []

    sender = payload.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    user_id = str(sender.get("user_id", "") or "")
    user_name = str(sender.get("card") or sender.get("nickname") or "")
    text_parts: list[str] = []
    media: list[dict[str, Any]] = []
    message_content = payload.get("message")
    if isinstance(message_content, list):
        for seg in message_content:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type", "")).lower()
            seg_data = seg.get("data")
            if not isinstance(seg_data, dict):
                seg_data = {}
            if seg_type == "text":
                piece = str(seg_data.get("text", ""))
                if piece:
                    text_parts.append(piece)
            elif seg_type in {"image", "video", "record", "audio"}:
                media.append({"type": seg_type, "data": dict(seg_data)})
    return user_id, user_name, "\n".join(text_parts).strip(), media


async def _event_to_engine_message(
    event: dict[str, Any],
    *,
    dispatcher: Any,
    bot_id: str,
    trace_builder: Any,
    api_call: Any = None,
) -> EngineMessage:
    conversation_id = str(event.get("conversation_id", ""))
    seq = dispatcher.next_seq(conversation_id)
    trace_id = trace_builder(conversation_id=conversation_id, seq=seq)
    # 从 MessageChain 提取 @/媒体/回复，避免平台路径丢失群聊结构信息
    # （bot 是否被 @、有没有图片、at 了谁）。
    chain = event.get("chain")
    raw_segments: list[Any] = []
    mentioned = False
    at_other_user_ids: list[str] = []
    raw_at_targets: list[str] = []
    reply_to_message_id = ""
    if chain is not None:
        try:
            raw_segments = chain.to_onebot_segments()
            for comp in chain.components:
                comp_type = type(comp).__name__
                if comp_type == "At":
                    qq = str(getattr(comp, "qq", "") or "")
                    raw_at_targets.append(qq)
                    if qq == bot_id:
                        mentioned = True
                    elif qq and qq != "all":
                        at_other_user_ids.append(qq)
                elif comp_type == "Reply":
                    reply_to_message_id = str(getattr(comp, "message_id", "") or "")
        except Exception:
            _log.warning("platform_chain_parse_fail", exc_info=True)
    at_other_user_ids = list(dict.fromkeys(at_other_user_ids))
    # 回复上下文（reply_to_user_id / reply_media_segments）需 get_msg 才能拿到，
    # 经平台 api_call 解析；失败静默置空。回复的是 bot 视同被 @（与 app.py 对齐）。
    (
        reply_to_user_id,
        reply_to_user_name,
        reply_to_text,
        reply_media_segments,
    ) = await _resolve_platform_reply_context(api_call, reply_to_message_id)
    if reply_to_user_id and str(reply_to_user_id) == str(bot_id):
        mentioned = True
    at_other_user_only = (bool(raw_at_targets) and not mentioned) or (
        bool(reply_to_user_id) and str(reply_to_user_id) != str(bot_id)
    )
    return EngineMessage(
        conversation_id=conversation_id,
        user_id=str(event.get("user_id", "")),
        user_name="",
        sender_role=str(event.get("sender_role", "") or ""),
        text=str(event.get("text", "")),
        message_id=str(event.get("message_id", "")),
        seq=seq,
        raw_segments=raw_segments,
        queue_depth=dispatcher.pending_count(conversation_id),
        mentioned=mentioned,
        is_private=bool(event.get("is_private", False)),
        timestamp=datetime.now(UTC),
        group_id=int(event.get("group_id", 0) or 0),
        bot_id=bot_id,
        at_other_user_only=at_other_user_only,
        at_other_user_ids=at_other_user_ids,
        trace_id=trace_id,
        reply_to_message_id=reply_to_message_id,
        reply_to_user_id=reply_to_user_id,
        reply_to_user_name=reply_to_user_name,
        reply_to_text=reply_to_text,
        reply_media_segments=reply_media_segments,
        api_call=api_call,
        event_payload={},
    )


async def _reply_to_session(
    adapter: OneBot11Adapter,
    session_id: str,
    response: EngineResponse,
    voice_max_seconds: int = 60,
    *,
    group_id: int = 0,
    bot_id: str = "",
    config: dict[str, Any] | None = None,
) -> None:
    """发送 EngineResponse 到会话，经统一发送核心（文本/图片/视频/语音 + 发送保护）。"""
    from core.response_delivery import deliver_response

    cfg = dict(config or {})
    bot_cfg = cfg.get("bot")
    if not isinstance(bot_cfg, dict):
        cfg["bot"] = {}
    if "voice_send_max_seconds" not in cfg["bot"]:
        cfg["bot"]["voice_send_max_seconds"] = voice_max_seconds

    async def sender(chain: Any) -> bool:
        return await adapter.send_by_session(session_id, chain)

    await deliver_response(
        cfg,
        response,
        sender,
        conversation_id=session_id,
        group_id=group_id,
        bot_id=bot_id,
    )


async def register_onebot11_platform(
    engine: Any,
    dispatcher: Any,
    *,
    config: dict[str, Any] | None = None,
    trace_builder: Any = None,
) -> PlatformManager | None:
    """创建并启动 OneBot11Adapter 平台路径（事件 → engine 消息管线）。

    `config` 含 host/port/access_token/bot_id；未配置 `platform.onebot11.enabled=true`
    时不启动（避免无真机时改变生产路径）。返回 PlatformManager（可 stop）。
    """
    cfg = dict(config or {})
    if not cfg.get("enabled"):
        _log.info("onebot11_platform_disabled | config gate off")
        return None

    def _default_trace_builder(conversation_id: str, seq: int) -> str:
        return f"platform-{conversation_id}-{seq}"

    builder = trace_builder or _default_trace_builder
    adapter = OneBot11Adapter(
        {
            "host": cfg.get("host", "0.0.0.0"),
            "port": int(cfg.get("port", 8082)),
            "access_token": cfg.get("access_token", ""),
            "bot_id": cfg.get("bot_id", ""),
        }
    )
    bot_id = str(cfg.get("bot_id", ""))

    async def _platform_api_call(api: str, **kwargs: Any) -> Any:
        # 供工具层经 NapCat 调用 OneBot API（music_play 依赖 api_call 才携带 audio_file）。
        return await adapter._send_api(api, kwargs)

    async def message_handler(event: dict[str, Any]) -> None:
        try:
            payload = await _event_to_engine_message(
                event,
                dispatcher=dispatcher,
                bot_id=bot_id,
                trace_builder=builder,
                api_call=_platform_api_call,
            )
            response: EngineResponse = await engine.handle_message(payload)
            await _reply_to_session(
                adapter,
                str(event.get("conversation_id", "")),
                response,
                group_id=int(event.get("group_id", 0) or 0),
                bot_id=bot_id,
                config=engine.config if isinstance(engine.config, dict) else None,
            )
        except Exception:
            _log.warning("platform_message_error", exc_info=True)

    adapter.message_handler = message_handler
    manager = PlatformManager()
    manager.register("onebot11", adapter)
    await manager.start()
    _log.info(
        "onebot11_platform_started | host=%s | port=%d",
        cfg.get("host", "0.0.0.0"),
        int(cfg.get("port", 8082)),
    )
    return manager
