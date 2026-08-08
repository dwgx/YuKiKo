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


def _event_to_engine_message(
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
    reply_to_message_id = ""
    if chain is not None:
        try:
            raw_segments = chain.to_onebot_segments()
            for comp in chain.components:
                comp_type = type(comp).__name__
                if comp_type == "At":
                    qq = str(getattr(comp, "qq", "") or "")
                    if qq == bot_id:
                        mentioned = True
                    elif qq and qq != "all":
                        at_other_user_ids.append(qq)
                elif comp_type == "Reply":
                    reply_to_message_id = str(getattr(comp, "message_id", "") or "")
        except Exception:
            _log.warning("platform_chain_parse_fail", exc_info=True)
    return EngineMessage(
        conversation_id=conversation_id,
        user_id=str(event.get("user_id", "")),
        user_name="",
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
        at_other_user_ids=at_other_user_ids,
        trace_id=trace_id,
        reply_to_message_id=reply_to_message_id,
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
            payload = _event_to_engine_message(
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
