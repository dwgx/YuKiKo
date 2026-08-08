"""Phase 2：OneBot V11 反连 WS adapter（自建 WS server，不依赖 NoneBot/aiocqhttp）。

- run()：`websockets.serve` 起 WS server，NapCat 反向连接 `/onebot/v11/ws`。
- 鉴权：`X-Self-ID` 匹配 bot_id + `Authorization: Bearer <token>`（或 `?access_token=`）。
- 入站：OneBot event → MessageChain（`components.from_onebot_segments`）→ commit_event。
- 出站：`{action, params, echo}` API 分派，回 `{status, retcode, data, echo}`。

事件解析与鉴权是纯逻辑（可测）；WS 连接循环留骨架。对应 docs/zh-CN/
RECONSTRUCTION-BLUEPRINT.md §4.4（2）与 §9 附录。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any


def _mask_sensitive_response(text: str) -> str:
    """截断并脱敏 OneBot 响应里的敏感键（token/cookie/凭证），供日志诊断。"""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in list(data.keys()):
                if any(s in str(key).lower() for s in ("token", "secret", "cookie", "credential")):
                    data[key] = "[redacted]"
            return json.dumps(data, ensure_ascii=False)[:400]
    except Exception:
        pass
    return text[:400]

import websockets

from core.platform.base import Platform, PlatformMetadata, PlatformStatus
from core.platform.components import MessageChain

_log = logging.getLogger("yukiko.platform.onebot11")


class OneBot11Adapter(Platform):
    """OneBot V11 反向 WebSocket 平台适配器。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        event_queue: Any = None,
        message_handler: Any = None,
    ) -> None:
        super().__init__(config, event_queue)
        # 消息处理回调：`async (event: dict) -> None`，由桥接层注入（投 GroupQueueDispatcher）。
        self.message_handler = message_handler
        self.host = str(config.get("host", "0.0.0.0"))
        self.port = int(config.get("port", 8081))
        self.access_token = str(config.get("access_token", ""))
        self.bot_id = str(config.get("bot_id", ""))
        self._server: Any = None
        self._active_ws: Any = None
        # NapCat 反连连接（第一个）：固定用于 API 转发与事件接收；额外连接不替换它。
        self._napcat_ws: Any = None
        self._napcat_send: Any = None
        self._authenticated = False
        self._pending_api: dict[str, asyncio.Future[Any]] = {}
        # 发送抽象：websockets 库接口 vs Starlette WebSocket（挂 uvicorn 时用）。
        self._ws_send_text: Any = None

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="onebot11",
            support_streaming_message=True,
            support_proactive_message=True,
        )

    async def run(self) -> None:
        self._server = await websockets.serve(self._handle_ws, self.host, self.port)
        _log.info("onebot11_listening | host=%s | port=%d", self.host, self.port)
        await self._server.wait_closed()

    async def terminate(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.status = PlatformStatus.STOPPED

    # ── 鉴权（可测）──

    def _check_auth(self, headers: Mapping[str, str], query: Mapping[str, str]) -> bool:
        """OneBot 反连 WS 握手鉴权。**fail-closed**：未配置 access_token 拒绝一切连接，
        配置了必须严格匹配；bot_id 配置了必须匹配 X-Self-ID。防止 0.0.0.0 裸连被劫持。
        """
        headers_lower = {str(k).lower(): str(v) for k, v in headers.items()}
        self_id = headers_lower.get("x-self-id", "")
        token = headers_lower.get("authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        if not token:
            token = str(query.get("access_token", ""))
        if not self.access_token:
            # fail-closed：无 token 配置时宁可不接，也不能让任意对端直连。
            return False
        if token != self.access_token:
            return False
        if self.bot_id and self_id and self_id != self.bot_id:
            return False
        return True

    # ── 入站：事件解析（可测）──

    def _handle_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """把 OneBot event 转成统一 dict 事件并 commit_event；非 message 事件返回 None。"""
        if payload.get("post_type") != "message":
            return None
        message_type = payload.get("message_type")
        user_id = str(payload.get("user_id", ""))
        group_id = payload.get("group_id")
        message_id = str(payload.get("message_id", ""))
        chain = MessageChain.from_onebot_segments(payload.get("message"))
        sender = payload.get("sender")
        sender_role = (
            str(sender.get("role", "")).lower()
            if isinstance(sender, dict) and sender.get("role")
            else ""
        )
        if message_type == "group":
            conversation_id = f"group:{group_id}"
        else:
            conversation_id = f"private:{user_id}"
        event: dict[str, Any] = {
            "type": "message",
            "conversation_id": conversation_id,
            "user_id": user_id,
            "group_id": int(group_id) if group_id else 0,
            "message_id": message_id,
            "is_private": message_type != "group",
            "sender_role": sender_role,
            "text": chain.get_plain_text(),
            "chain": chain,
        }
        self.commit_event(event)
        return event

    async def _dispatch_event(self, payload: dict[str, Any]) -> None:
        """构造统一事件并投递：先 commit_event（事件队列），再调 message_handler（若注入）。"""
        event = self._handle_event(payload)
        if event is None:
            return
        handler = getattr(self, "message_handler", None)
        if handler is not None:
            try:
                await handler(event)
            except Exception:
                _log.warning("message_handler_error | type=%s", event.get("type"), exc_info=True)

    # ── WS 循环（骨架）──

    async def _handle_ws(self, websocket: Any) -> None:
        headers = getattr(websocket, "request_headers", {}) or {}
        query = dict(getattr(websocket, "query", {}) or {})
        if not self._check_auth(headers, query):
            await websocket.close(code=4401)
            return
        self._authenticated = True
        if self._napcat_ws is not None and self._napcat_ws is not websocket:
            # 只接受 NapCat 单连接；额外连接拒绝（防劫持/自环/槽位占用）。
            _log.warning("onebot_ws_extra_connection_rejected")
            try:
                await websocket.close(code=4001)
            except Exception:
                pass
            return
        self._napcat_ws = websocket
        self._napcat_send = lambda text: websocket.send(text)
        self._active_ws = websocket
        self._ws_send_text = self._napcat_send
        _log.info("onebot_ws_connected | napcat")
        try:
            async for raw in websocket:
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                echo = payload.get("echo")
                if echo is not None and echo in self._pending_api:
                    # 我们上行 {action,params,echo} 的回包：resolve 对应 future。
                    future = self._pending_api.pop(echo, None)
                    if future is not None and not future.done():
                        future.set_result(payload)
                    continue
                if "action" in payload:
                    await self._handle_api(websocket, payload)
                else:
                    await self._dispatch_event(payload)
        except Exception:
            pass
        finally:
            if self._napcat_ws is websocket:
                self._napcat_ws = None
                self._napcat_send = None
            if self._active_ws is websocket:
                self._active_ws = None
                self._ws_send_text = None

    async def handle_starlette_ws(self, websocket: Any) -> None:
        """Starlette WebSocket 入口（挂 uvicorn 与 WebUI 同端口时用）。"""
        headers = dict(getattr(websocket, "headers", {}) or {})
        query = dict(getattr(websocket, "query_params", {}) or {})
        if not self._check_auth(headers, query):
            try:
                await websocket.close(code=4401)
            except Exception:
                pass
            return
        self._authenticated = True
        if self._napcat_ws is not None and self._napcat_ws is not websocket:
            # 只接受 NapCat 单连接。额外连接（调试/劫持）会占用槽位导致 NapCat
            # 事件被丢弃（失聪）——直接拒绝。NapCat 重连时旧连接 finally 清理后再接受。
            _log.warning("onebot_ws_extra_connection_rejected")
            try:
                await websocket.close(code=4001)
            except Exception:
                pass
            return
        self._napcat_ws = websocket
        self._napcat_send = lambda text: websocket.send_text(text)
        self._active_ws = websocket
        self._ws_send_text = self._napcat_send
        _log.info("onebot_ws_connected | napcat")
        try:
            await websocket.accept()
            while True:
                raw = await websocket.receive_text()
                payload = json.loads(raw)
                echo = payload.get("echo")
                if echo is not None and echo in self._pending_api:
                    future = self._pending_api.pop(echo, None)
                    if future is not None and not future.done():
                        future.set_result(payload)
                    continue
                if "action" in payload:
                    await self._handle_api(websocket, payload)
                else:
                    await self._dispatch_event(payload)
        except Exception:
            pass
        finally:
            if self._napcat_ws is websocket:
                self._napcat_ws = None
                self._napcat_send = None
            if self._active_ws is websocket:
                self._active_ws = None
                self._ws_send_text = None

    async def _send_text(self, text: str) -> None:
        """统一发送文本：固定经 NapCat 连接（Starlette 用 send_text，websockets 用 send）。"""
        if self._napcat_send is not None:
            await self._napcat_send(text)
        elif self._active_ws is not None:
            await self._active_ws.send(text)

    async def _handle_api(self, websocket: Any, payload: dict[str, Any]) -> None:
        # 只处理通过鉴权的连接（fail-closed 的补充防线）。
        if not getattr(self, "_authenticated", False):
            return
        action = str(payload.get("action", ""))
        params = payload.get("params") or {}
        echo = payload.get("echo")
        data, retcode = await self._dispatch_api(action, params)
        response = {
            "status": "ok" if retcode == 0 else "failed",
            "retcode": retcode,
            "data": data,
            "echo": echo,
        }
        # 响应必须发回**请求来源连接**（NapCat 的 API 响应走 NapCat 连接，调试连接走调试连接），
        # 不能用 _send_text（那固定发到 NapCat 连接）。
        send_back = getattr(websocket, "send_text", None) or getattr(websocket, "send", None)
        if send_back is not None:
            await send_back(json.dumps(response, ensure_ascii=False))

    async def _dispatch_api(self, action: str, params: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """API 分派：支持 OneBot 核心动作（发送/撤回/改群名片/取消息）。

        固定经 NapCat 连接上行 `{action, params, echo}`，返回 (data, retcode)——
        retcode 透传真实结果，避免调用方谎报成功。
        """
        if action not in {
            "send_group_msg",
            "send_private_msg",
            "send_msg",
            "send_group_forward_msg",
            "delete_msg",
            "set_group_card",
            "get_msg",
        }:
            return {}, 0
        response = await self._send_api(action, params)
        return response.get("data", {}), int(response.get("retcode", 0))

    async def _send_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """上行 OneBot API 调用并等响应（echo 匹配）。固定经 NapCat 连接转发。"""
        if self._napcat_ws is None:
            return {"status": "failed", "retcode": -1, "data": {}}
        echo = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending_api[echo] = future
        try:
            await self._send_text(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
            response = await asyncio.wait_for(future, timeout=30.0)
        except (TimeoutError, Exception):
            self._pending_api.pop(echo, None)
            return {"status": "failed", "retcode": -1, "data": {}}
        return response if isinstance(response, dict) else {}

    async def send_by_session(self, session_id: str, chain: Any) -> bool:
        """按会话发送 MessageChain（OneBot 真实发送，经 NapCat）。"""
        segments = chain.to_onebot_segments()
        if session_id.startswith("group:"):
            try:
                group_id = int(session_id.split(":", 1)[1])
            except ValueError:
                return False
            action, params = "send_group_msg", {"group_id": group_id, "message": segments}
        elif session_id.startswith("private:"):
            try:
                user_id = int(session_id.split(":", 1)[1])
            except ValueError:
                return False
            action, params = "send_private_msg", {"user_id": user_id, "message": segments}
        else:
            return False
        response = await self._send_api(action, params)
        retcode = int(response.get("retcode", -1))
        # 暴露最近一次发送的 retcode，供熔断分类用（-1=无连接不应熔断）。
        self.last_send_retcode = retcode
        ok = retcode == 0
        if not ok:
            # 记录 NapCat 返回的原始响应，定位 send_group_msg 被拒的具体 retcode/错误。
            _log.warning(
                "onebot_send_failed | action=%s | session=%s | retcode=%s | resp=%s",
                action,
                session_id,
                response.get("retcode"),
                _mask_sensitive_response(str(response)[:400]),
            )
        return ok
