"""D7：平台路径 sender_role 事件解析回归测试。

锁三件事：
1. `OneBot11Adapter._handle_event` 从 OneBot event 的 `sender.role`
   （owner/admin/member）提取 `sender_role`，缺失/非 dict 时置空。
2. `_event_to_engine_message` 把 event 的 `sender_role` 传给 `EngineMessage`。
3. 空 role / 缺失 sender 时保持空串，不阻断入站。
"""
from __future__ import annotations

import asyncio
import unittest

from core.engine_types import EngineMessage
from core.platform.bridge import _event_to_engine_message
from core.platform.onebot11 import OneBot11Adapter


class _FakeDispatcher:
    def next_seq(self, conversation_id: str) -> int:
        return 1

    def pending_count(self, conversation_id: str) -> int:
        return 0


async def _to_message(event: dict) -> EngineMessage:
    return await _event_to_engine_message(
        event,
        dispatcher=_FakeDispatcher(),
        bot_id="123",
        trace_builder=lambda conversation_id, seq: f"trace-{seq}",
        api_call=None,
    )


class HandleEventSenderRoleTests(unittest.TestCase):
    def _handle(self, sender_role: str | None) -> dict:
        queue: asyncio.Queue = asyncio.Queue()
        adapter = OneBot11Adapter({"bot_id": "123"}, event_queue=queue)
        sender: dict = {"user_id": "10001", "nickname": "n"}
        if sender_role is not None:
            sender["role"] = sender_role
        event = adapter._handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 42,
                "user_id": "10001",
                "message_id": "789",
                "sender": sender,
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )
        assert event is not None
        return event

    def test_sender_role_owner(self) -> None:
        self.assertEqual(self._handle("owner")["sender_role"], "owner")

    def test_sender_role_admin(self) -> None:
        self.assertEqual(self._handle("admin")["sender_role"], "admin")

    def test_sender_role_member(self) -> None:
        self.assertEqual(self._handle("member")["sender_role"], "member")

    def test_sender_role_uppercase_normalized(self) -> None:
        self.assertEqual(self._handle("OWNER")["sender_role"], "owner")

    def test_sender_missing_role_is_empty(self) -> None:
        self.assertEqual(self._handle(None)["sender_role"], "")

    def test_sender_absent_is_empty(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        adapter = OneBot11Adapter({"bot_id": "123"}, event_queue=queue)
        event = adapter._handle_event(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 42,
                "user_id": "10001",
                "message_id": "789",
                "message": [{"type": "text", "data": {"text": "hi"}}],
            }
        )
        assert event is not None
        self.assertEqual(event["sender_role"], "")


class EventToEngineMessageSenderRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_role_passed_through(self) -> None:
        msg = await _to_message(
            {
                "conversation_id": "group:42",
                "user_id": "10001",
                "group_id": 42,
                "message_id": "789",
                "is_private": False,
                "text": "hi",
                "sender_role": "admin",
            }
        )
        self.assertEqual(msg.sender_role, "admin")

    async def test_sender_role_default_empty(self) -> None:
        msg = await _to_message(
            {
                "conversation_id": "group:42",
                "user_id": "10001",
                "group_id": 42,
                "message_id": "789",
                "is_private": False,
                "text": "hi",
            }
        )
        self.assertEqual(msg.sender_role, "")


if __name__ == "__main__":
    unittest.main()
