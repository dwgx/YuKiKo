"""Phase 2 接线：平台桥接层回归测试。

锁两件事：
1. `_event_to_engine_message` 把 OneBot 事件 dict 转成 EngineMessage（走 dispatcher 串行化）。
2. `register_onebot11_platform` 配置 gate 关闭时不启动（避免无真机时改变生产路径）。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from core.platform.bridge import _event_to_engine_message, register_onebot11_platform


class PlatformBridgeTests(unittest.TestCase):
    def test_event_to_engine_message(self) -> None:
        dispatcher = MagicMock()
        dispatcher.next_seq.return_value = 1
        dispatcher.pending_count.return_value = 0
        event = {
            "conversation_id": "group:42",
            "user_id": "10001",
            "message_id": "789",
            "text": "hi",
            "is_private": False,
            "group_id": 42,
        }
        msg = asyncio.run(
            _event_to_engine_message(
                event,
                dispatcher=dispatcher,
                bot_id="bot",
                trace_builder=lambda conversation_id, seq: "t1",
            )
        )
        self.assertEqual(msg.conversation_id, "group:42")
        self.assertEqual(msg.text, "hi")
        self.assertEqual(msg.user_id, "10001")
        self.assertEqual(msg.group_id, 42)
        self.assertEqual(msg.trace_id, "t1")
        dispatcher.next_seq.assert_called_once_with("group:42")

    def test_register_disabled_gate_returns_none(self) -> None:
        engine = MagicMock()
        dispatcher = MagicMock()
        result = asyncio.run(register_onebot11_platform(engine, dispatcher, config={"enabled": False}))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
