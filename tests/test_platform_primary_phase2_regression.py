"""Phase 2 平台主路径：Starlette WebSocket 入口回归测试。

锁两件事：
1. handle_starlette_ws 鉴权成功后 accept + 处理 message 事件（走 message_handler）。
2. 鉴权失败 close 4401（fail-closed）。
"""
from __future__ import annotations

import asyncio
import json
import unittest

from core.platform.onebot11 import OneBot11Adapter


class _FakeStarletteWS:
    def __init__(self, headers: dict, messages: list[str]) -> None:
        self.headers = headers
        self.query_params = {}
        self._messages = list(messages)
        self.accepted = False
        self.closed: int | None = None
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        if not self._messages:
            raise RuntimeError("closed")
        return self._messages.pop(0)

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


class StarletteWSHandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_auth_success_processes_message_event(self) -> None:
        adapter = OneBot11Adapter({"access_token": "secret", "bot_id": "123"})
        handled: list[dict] = []

        async def handler(event: dict) -> None:
            handled.append(event)

        adapter.message_handler = handler
        ws = _FakeStarletteWS(
            {"x-self-id": "123", "authorization": "Bearer secret"},
            [
                json.dumps(
                    {
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 42,
                        "user_id": "10001",
                        "message_id": "1",
                        "message": [{"type": "text", "data": {"text": "hi"}}],
                    }
                )
            ],
        )
        asyncio.run(adapter.handle_starlette_ws(ws))
        self.assertTrue(ws.accepted)
        self.assertIsNone(ws.closed)
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0]["conversation_id"], "group:42")
        self.assertEqual(handled[0]["text"], "hi")

    def test_auth_failure_closes_4401(self) -> None:
        adapter = OneBot11Adapter({"access_token": "secret", "bot_id": "123"})
        ws = _FakeStarletteWS({"x-self-id": "123", "authorization": "Bearer wrong"}, [])
        asyncio.run(adapter.handle_starlette_ws(ws))
        self.assertEqual(ws.closed, 4401)
        self.assertFalse(ws.accepted)


if __name__ == "__main__":
    unittest.main()
