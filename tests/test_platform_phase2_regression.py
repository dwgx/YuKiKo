"""Phase 2：平台层骨架回归测试。

锁四件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4）：
1. MessageChain 组件与 OneBot V11 段双向转换（roundtrip）。
2. OneBot11Adapter 事件解析（group/private message → 统一事件 → commit_event）。
3. OneBot11Adapter 反连 WS 鉴权（X-Self-ID + access_token，header/query 两路）。
4. PlatformManager 生命周期（start 跑 run()、stop 终止）。
"""
from __future__ import annotations

import asyncio
import unittest

from core.platform.base import Platform, PlatformMetadata, PlatformStatus
from core.platform.components import At, Image, MessageChain, Plain
from core.platform.manager import PlatformManager
from core.platform.onebot11 import OneBot11Adapter


class MessageChainTests(unittest.TestCase):
    def test_roundtrip_onebot_segments(self) -> None:
        chain = MessageChain([Plain("hi "), At("123"), Plain(" 你好")])
        segments = chain.to_onebot_segments()
        self.assertEqual(segments[0], {"type": "text", "data": {"text": "hi "}})
        self.assertEqual(segments[1], {"type": "at", "data": {"qq": "123"}})
        restored = MessageChain.from_onebot_segments(segments)
        self.assertEqual(restored.get_plain_text(), "hi  你好")

    def test_get_plain_text_skips_non_plain(self) -> None:
        chain = MessageChain([Plain("你好"), Image(file="x.png")])
        self.assertEqual(chain.get_plain_text(), "你好")

    def test_squash_plain_merges_consecutive(self) -> None:
        chain = MessageChain([Plain("a"), Plain("b"), At("1"), Plain("c")])
        chain.squash_plain()
        self.assertEqual(chain.get_plain_text(), "abc")
        plain_count = sum(1 for c in chain.components if isinstance(c, Plain))
        self.assertEqual(plain_count, 2)

    def test_image_base64_segment(self) -> None:
        chain = MessageChain([Image(base64="abc")])
        segments = chain.to_onebot_segments()
        self.assertEqual(segments[0]["data"]["file"], "base64://abc")


class OneBot11AdapterTests(unittest.TestCase):
    def test_handle_group_message_event(self) -> None:
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
        self.assertIsNotNone(event)
        self.assertEqual(event["conversation_id"], "group:42")
        self.assertEqual(event["user_id"], "10001")
        self.assertEqual(event["text"], "hi")
        self.assertEqual(queue.qsize(), 1)  # commit_event 投进队列

    def test_handle_private_message_event(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        adapter = OneBot11Adapter({"bot_id": "123"}, event_queue=queue)
        event = adapter._handle_event(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": "10001",
                "message_id": "1",
                "message": [{"type": "text", "data": {"text": "私聊"}}],
            }
        )
        self.assertEqual(event["conversation_id"], "private:10001")
        self.assertTrue(event["is_private"])

    def test_non_message_event_returns_none(self) -> None:
        adapter = OneBot11Adapter({})
        self.assertIsNone(
            adapter._handle_event({"post_type": "notice", "notice_type": "group_upload"})
        )

    def test_auth_matches_header_token_and_self_id(self) -> None:
        adapter = OneBot11Adapter({"access_token": "secret", "bot_id": "123"})
        self.assertTrue(
            adapter._check_auth({"X-Self-ID": "123", "Authorization": "Bearer secret"}, {})
        )
        self.assertFalse(
            adapter._check_auth({"X-Self-ID": "123", "Authorization": "Bearer wrong"}, {})
        )
        self.assertFalse(
            adapter._check_auth({"X-Self-ID": "999", "Authorization": "Bearer secret"}, {})
        )

    def test_auth_token_in_query(self) -> None:
        adapter = OneBot11Adapter({"access_token": "secret", "bot_id": ""})
        self.assertTrue(adapter._check_auth({}, {"access_token": "secret"}))
        self.assertFalse(adapter._check_auth({}, {"access_token": "wrong"}))


class PlatformManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_cycles_lifecycle(self) -> None:
        class _Fake(Platform):
            def meta(self) -> PlatformMetadata:
                return PlatformMetadata(name="fake")

            async def run(self) -> None:
                self.status = PlatformStatus.RUNNING
                await asyncio.sleep(0.1)

        manager = PlatformManager()
        fake = _Fake()
        manager.register("fake", fake)
        await manager.start()
        await asyncio.sleep(0.05)
        self.assertEqual(fake.status, PlatformStatus.RUNNING)
        await manager.stop()
        self.assertEqual(fake.status, PlatformStatus.STOPPED)


if __name__ == "__main__":
    unittest.main()
