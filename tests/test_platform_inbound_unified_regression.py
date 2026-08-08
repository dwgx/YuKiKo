"""D6：平台路径入站统一回归测试。

锁住平台路径（`bridge._event_to_engine_message` → `engine.handle_message`）与
NoneBot 主路径的差异收敛：
1. 消息去重（`_seen_message_ids`）在 engine 层统一生效——同一 message_id 重复推送被 ignore。
2. 媒体预缓存（`remember_incoming_media` / `_index_message_media`）在 engine 层统一生效——
   平台消息带图被缓存、回复里带的媒体也被索引。
3. `_event_to_engine_message` 经平台 api_call 的 get_msg 正确填充 reply/at 结构事实
   （reply_to_user_id / reply_to_user_name / reply_to_text / reply_media_segments /
   at_other_user_only / mentioned），不再让触发判断和媒体缓存丢信息。
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from typing import Any

from core.engine import YukikoEngine
from core.platform.bridge import _event_to_engine_message
from core.platform.components import At, Image, MessageChain, Plain, Reply


class _D:
    def next_seq(self, conversation_id: str) -> int:
        return 1

    def pending_count(self, conversation_id: str) -> int:
        return 0


class _StubAdmin:
    enabled = False

    def increment_message_count(self) -> None:
        pass


class _StubTools:
    """记录 remember_incoming_media 调用，验证 engine 把媒体交给缓存层。"""

    def __init__(self) -> None:
        self.remembered: list[tuple[str, list[dict[str, Any]]]] = []

    def remember_incoming_media(self, conversation_id: str, raw_segments: list[dict[str, Any]] | None) -> None:
        self.remembered.append((conversation_id, list(raw_segments or [])))


def _minimal_engine() -> YukikoEngine:
    engine = YukikoEngine.__new__(YukikoEngine)
    engine.logger = logging.getLogger("yukiko.test")
    engine._async_init_done = True
    engine.admin = _StubAdmin()
    engine.config = {"bot": {"name": "yuki", "nicknames": []}}
    engine._seen_message_ids = {}
    engine._seen_message_ids_max = 1000
    engine._media_artifact_index = {}
    engine._media_artifact_index_max = 200
    engine._runtime_group_chat_cache = {}
    engine._pending_fragments = {}
    engine.fragment_join_enable = True
    engine.fragment_hold_max_chars = 64
    engine.fragment_join_window_seconds = 8
    engine.fragment_timeout_fallback_seconds = 12
    return engine


class PlatformInboundUnifiedEngineTests(unittest.TestCase):
    """平台路径消息走 engine 后，去重与媒体缓存与 NoneBot 主路径一致。"""

    def test_duplicate_message_id_ignored_after_first_platform_message(self) -> None:
        engine = _minimal_engine()
        from core.engine_types import EngineMessage

        first = EngineMessage(
            conversation_id="group:42",
            user_id="10001",
            text="",
            message_id="dup-1",
        )
        second = EngineMessage(
            conversation_id="group:42",
            user_id="10001",
            text="",
            message_id="dup-1",
        )

        r1 = asyncio.run(engine.handle_message(first))
        r2 = asyncio.run(engine.handle_message(second))

        self.assertEqual(r1.action, "ignore")
        self.assertEqual(r1.reason, "empty_message")
        # 同一 message_id 重复推送在 engine 层被去重，不再进入后续管线。
        self.assertEqual(r2.action, "ignore")
        self.assertEqual(r2.reason, "duplicate_message")

    def test_platform_message_media_cached_via_handle_message(self) -> None:
        from core.engine_types import EngineMessage

        engine = _minimal_engine()
        engine.tools = _StubTools()
        engine._runtime_group_chat_cache["group:42"] = []
        msg = EngineMessage(
            conversation_id="group:42",
            user_id="10001",
            text="abc123",  # 短 token 会走 fragment hold，在媒体缓存之后早退
            message_id="img-m1",
            raw_segments=[
                {"type": "text", "data": {"text": "abc123"}},
                {"type": "image", "data": {"url": "http://x/in.png"}},
            ],
            is_private=False,
            group_id=42,
        )

        resp = asyncio.run(engine.handle_message(msg))

        self.assertEqual(resp.reason, "fragment_waiting_followup")
        # 主消息媒体进了 recent media 缓存。
        remembered_media = [seg for _, segs in engine.tools.remembered for seg in segs]
        self.assertTrue(
            any(seg.get("type") == "image" for seg in remembered_media),
            f"image 未进媒体缓存: {engine.tools.remembered}",
        )
        # message_id -> media artifact 索引建立。
        refs = engine._media_artifact_index.get("img-m1", [])
        self.assertTrue(any(ref["type"] == "image" and ref["url"] for ref in refs))

    def test_platform_message_reply_media_cached_via_handle_message(self) -> None:
        from core.engine_types import EngineMessage

        engine = _minimal_engine()
        engine.tools = _StubTools()
        engine._runtime_group_chat_cache["group:42"] = []
        msg = EngineMessage(
            conversation_id="group:42",
            user_id="10001",
            text="abc123",
            message_id="m2",
            raw_segments=[{"type": "text", "data": {"text": "abc123"}}],
            reply_to_message_id="rep-9",
            reply_to_user_id="555",
            reply_media_segments=[{"type": "image", "data": {"url": "http://x/reply.png"}}],
            is_private=False,
            group_id=42,
        )

        resp = asyncio.run(engine.handle_message(msg))

        self.assertEqual(resp.reason, "fragment_waiting_followup")
        # 回复消息带的媒体也进 recent media 缓存。
        self.assertTrue(any(seg.get("type") == "image" for _, segs in engine.tools.remembered for seg in segs))
        # 回复消息的媒体按 reply_to_message_id 建索引。
        refs = engine._media_artifact_index.get("rep-9", [])
        self.assertTrue(any(ref["type"] == "image" and ref["url"] for ref in refs))


class PlatformInboundUnifiedBridgeTests(unittest.IsolatedAsyncioTestCase):
    """平台路径 `_event_to_engine_message` 补齐 reply/at 结构事实。"""

    async def test_reply_at_media_parsed_from_chain_and_get_msg(self) -> None:
        calls: list[tuple[str, Any]] = []

        async def fake_get_msg(api: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((api, kwargs))
            return {
                "retcode": 0,
                "data": {
                    "message_id": kwargs["message_id"],
                    "sender": {"user_id": "555", "card": "昵称", "nickname": "nick"},
                    "message": [
                        {"type": "text", "data": {"text": "原始文本"}},
                        {"type": "image", "data": {"url": "http://x/rep.png"}},
                    ],
                },
            }

        chain = MessageChain([Plain("hi"), At(qq="123"), Image(url="http://x/main.png"), Reply(message_id="rep-9")])
        event = {
            "conversation_id": "group:1",
            "user_id": "999",
            "text": "hi",
            "message_id": "m1",
            "is_private": False,
            "group_id": 1,
            "chain": chain,
        }
        msg = await _event_to_engine_message(
            event,
            dispatcher=_D(),
            bot_id="2488687937",
            trace_builder=lambda conversation_id, seq: "t",
            api_call=fake_get_msg,
        )

        self.assertEqual(msg.message_id, "m1")
        self.assertEqual(msg.reply_to_message_id, "rep-9")
        self.assertEqual(msg.reply_to_user_id, "555")
        self.assertEqual(msg.reply_to_user_name, "昵称")
        self.assertEqual(msg.reply_to_text, "原始文本")
        self.assertEqual(msg.reply_media_segments, [{"type": "image", "data": {"url": "http://x/rep.png"}}])
        self.assertEqual(msg.at_other_user_ids, ["123"])
        # 回复他人且未 @ bot → at_other_user_only 置真，触发判断不把它当直接对话。
        self.assertTrue(msg.at_other_user_only)
        self.assertFalse(msg.mentioned)
        # 入站媒体段保留（供 engine 缓存 + trigger 结构事实）。
        self.assertTrue(any(s.get("type") == "image" for s in msg.raw_segments))
        self.assertEqual(calls[0][0], "get_msg")
        self.assertEqual(calls[0][1]["message_id"], "rep-9")

    async def test_reply_to_bot_sets_mentioned(self) -> None:
        async def fake_get_msg(api: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "retcode": 0,
                "data": {
                    "message_id": kwargs["message_id"],
                    "sender": {"user_id": "2488687937", "card": "YuKiKo"},
                    "message": [{"type": "text", "data": {"text": "上一条"}}],
                },
            }

        chain = MessageChain([Reply(message_id="rep-9")])
        event = {
            "conversation_id": "group:1",
            "user_id": "999",
            "text": "继续",
            "message_id": "m1",
            "is_private": False,
            "group_id": 1,
            "chain": chain,
        }
        msg = await _event_to_engine_message(
            event,
            dispatcher=_D(),
            bot_id="2488687937",
            trace_builder=lambda conversation_id, seq: "t",
            api_call=fake_get_msg,
        )
        # 回复的是 bot 自己 → 视同被 @，且不算 @ 了别人。
        self.assertTrue(msg.mentioned)
        self.assertFalse(msg.at_other_user_only)
        self.assertEqual(msg.reply_to_user_id, "2488687937")

    async def test_at_other_only_sets_flag(self) -> None:
        chain = MessageChain([Plain("hi"), At(qq="123")])
        event = {
            "conversation_id": "group:1",
            "user_id": "999",
            "text": "hi",
            "message_id": "m1",
            "is_private": False,
            "group_id": 1,
            "chain": chain,
        }
        msg = await _event_to_engine_message(
            event,
            dispatcher=_D(),
            bot_id="2488687937",
            trace_builder=lambda conversation_id, seq: "t",
        )
        self.assertFalse(msg.mentioned)
        self.assertTrue(msg.at_other_user_only)
        self.assertEqual(msg.at_other_user_ids, ["123"])

    async def test_no_api_call_does_not_crash_reply_message(self) -> None:
        chain = MessageChain([Reply(message_id="rep-9")])
        event = {
            "conversation_id": "group:1",
            "user_id": "999",
            "text": "hi",
            "message_id": "m1",
            "is_private": False,
            "group_id": 1,
            "chain": chain,
        }
        msg = await _event_to_engine_message(
            event,
            dispatcher=_D(),
            bot_id="2488687937",
            trace_builder=lambda conversation_id, seq: "t",
        )
        self.assertEqual(msg.reply_to_message_id, "rep-9")
        self.assertEqual(msg.reply_to_user_id, "")
        self.assertEqual(msg.reply_media_segments, [])


if __name__ == "__main__":
    unittest.main()
