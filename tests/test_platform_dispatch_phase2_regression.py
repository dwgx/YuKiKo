"""Phase 2 延伸：OneBot11 adapter 真实发送 dispatch 回归测试。

锁三件事：
1. _dispatch_api 对已知动作上行 API 并返回 data，未知动作返回空。
2. send_by_session 按会话构造正确的 action/params（group/private）。
3. 无效会话返回 False。
"""
from __future__ import annotations

import unittest
from typing import Any

from core.platform.components import MessageChain, Plain
from core.platform.onebot11 import OneBot11Adapter


class OneBot11DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = OneBot11Adapter({})
        self.sent_calls: list[tuple[str, dict[str, Any]]] = []

        async def fake_send_api(action: str, params: dict[str, Any]) -> dict[str, Any]:
            self.sent_calls.append((action, params))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        self.adapter._send_api = fake_send_api  # type: ignore[method-assign]

    async def test_dispatch_send_group_msg(self) -> None:
        data = await self.adapter._dispatch_api(
            "send_group_msg", {"group_id": 42, "message": [{"type": "text", "data": {"text": "hi"}}]}
        )
        self.assertEqual(data, {"message_id": 1})
        self.assertEqual(self.sent_calls[0][0], "send_group_msg")

    async def test_unknown_action_returns_empty(self) -> None:
        data = await self.adapter._dispatch_api("some_unknown", {})
        self.assertEqual(data, {})
        self.assertEqual(self.sent_calls, [])

    async def test_send_by_session_group(self) -> None:
        ok = await self.adapter.send_by_session("group:42", MessageChain([Plain("hi")]))
        self.assertTrue(ok)
        action, params = self.sent_calls[0]
        self.assertEqual(action, "send_group_msg")
        self.assertEqual(params["group_id"], 42)
        self.assertEqual(params["message"], [{"type": "text", "data": {"text": "hi"}}])

    async def test_send_by_session_private(self) -> None:
        ok = await self.adapter.send_by_session("private:10001", MessageChain([Plain("hi")]))
        self.assertTrue(ok)
        action, params = self.sent_calls[0]
        self.assertEqual(action, "send_private_msg")
        self.assertEqual(params["user_id"], 10001)

    async def test_send_by_session_invalid_session(self) -> None:
        ok = await self.adapter.send_by_session("bad-session", MessageChain())
        self.assertFalse(ok)
        self.assertEqual(self.sent_calls, [])


if __name__ == "__main__":
    unittest.main()
