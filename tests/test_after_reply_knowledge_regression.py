from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from core.engine import EngineMessage, YukikoEngine


class AfterReplyKnowledgeUpdateRegressionTests(unittest.TestCase):
    """`_after_reply` 曾引用 handle_message 的局部变量 explicit_bot_addressed，
    抛 NameError 后被 except 吞成一条 DEBUG —— 知识自动更新静默失效。"""

    def _build_engine(self, updater: object) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.knowledge_updater = updater
        engine.logger = _SilentLogger()
        engine.trigger = _StubTrigger()
        engine.followup_consume_on_send = False
        engine._last_reply_state = {}
        engine._runtime_group_chat_cache = {}
        engine._get_bot_aliases = lambda: ["yukiko"]
        # allow_memory=False 让本用例只覆盖知识更新分支，不牵进 memory / 摘要 I/O。
        engine.config = {"bot": {"name": "YuKiKo", "allow_memory": False}}
        engine.memory = None
        return engine

    def test_should_pass_explicit_bot_addressed_without_nameerror(self) -> None:
        updater = _RecordingUpdater()
        engine = self._build_engine(updater)
        message = EngineMessage(
            conversation_id="group:1",
            user_id="u1",
            text="YuKiKo 你好",
            mentioned=True,
        )

        asyncio.run(engine._after_reply(message, "回复内容"))

        self.assertEqual(len(updater.calls), 1)
        self.assertTrue(updater.calls[0]["explicit_bot_addressed"])

    def test_should_report_false_for_undirected_group_message(self) -> None:
        updater = _RecordingUpdater()
        engine = self._build_engine(updater)
        message = EngineMessage(
            conversation_id="group:1",
            user_id="u1",
            text="今天天气不错",
            mentioned=False,
        )

        asyncio.run(engine._after_reply(message, "回复内容"))

        self.assertEqual(len(updater.calls), 1)
        self.assertFalse(updater.calls[0]["explicit_bot_addressed"])


class GroupMemberNameCacheEvictionRegressionTests(unittest.TestCase):
    """淘汰逻辑用 time.time() 的 float 去比 datetime 类型的 expires_at：
    time 未 import（NameError），补上 import 后又会 TypeError。"""

    def _build_engine(self) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine._recent_directed_hints = {}
        engine._last_reply_state = {}
        engine._recent_search_cache = {}
        engine._group_member_name_cache = {}
        engine._group_member_name_cache_max = 2
        engine._agent_conversation_locks = {}
        engine._agent_conversation_locks_max = 64
        engine.directed_grace_seconds = 60
        return engine

    def test_should_evict_only_expired_entries(self) -> None:
        engine = self._build_engine()
        now = datetime.now(timezone.utc)
        engine._recent_directed_hints = {"k": now}
        engine._group_member_name_cache = {
            1: {"names": {}, "expires_at": now - timedelta(seconds=10)},
            2: {"names": {}, "expires_at": now + timedelta(seconds=300)},
            3: {"names": {}, "expires_at": now - timedelta(seconds=1)},
        }

        engine._cleanup_directed_hints(now)

        self.assertEqual(sorted(engine._group_member_name_cache), [2])

    def test_should_survive_entries_missing_expires_at(self) -> None:
        engine = self._build_engine()
        now = datetime.now(timezone.utc)
        engine._recent_directed_hints = {"k": now}
        engine._group_member_name_cache = {
            1: {"names": {}},
            2: "not-a-dict",
            3: {"names": {}, "expires_at": now - timedelta(seconds=5)},
        }

        engine._cleanup_directed_hints(now)

        self.assertNotIn(3, engine._group_member_name_cache)


class _RecordingUpdater:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def update_from_turn(self, conv_id, user_id, source, reply, ts, meta) -> None:
        self.calls.append(dict(meta))


class _SilentLogger:
    def debug(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, *args, **kwargs) -> None:
        pass


class _StubTrigger:
    def activate_session(self, **kwargs) -> None:
        pass

    def mark_reply_target(self, *args) -> None:
        pass

    def mark_proactive_reply(self, *args) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
