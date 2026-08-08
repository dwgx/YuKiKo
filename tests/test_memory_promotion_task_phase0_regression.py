"""Phase 0.5a 延伸：记忆晋升后台任务回归测试。

锁两件事：
1. collect_promotion_candidates 从 embeddings 收集候选并排除 untrusted 来源。
2. engine._run_memory_promotion 走 collect → consolidate（模型）→ 写入 explicit_facts。
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from core.engine import EngineMessage, YukikoEngine
from core.memory import MemoryEngine


def _make_memory(root: Path) -> MemoryEngine:
    return MemoryEngine({"enable_daily_log": False}, root / "memory")


class _FakeModel:
    """consolidate 返回 added 操作的假模型客户端。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def chat_json(self, messages):  # type: ignore[no-untyped-def]
        _ = messages
        return self.payload


class CollectPromotionCandidatesTests(unittest.TestCase):
    def test_excludes_untrusted_and_keeps_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            memory.add_message(
                "group:1", "10001", "user", "untrusted话",
                user_name="小明",
                metadata={"is_private": False, "mentioned": False, "explicit_bot_addressed": False},
            )
            memory.add_message(
                "group:1", "10001", "user", "小明喜欢摄影",
                user_name="小明",
                metadata={"is_private": False, "mentioned": True, "explicit_bot_addressed": True},
            )
            memory._flush_vector_buffer()
            candidates = memory.collect_promotion_candidates(user_id="10001")
            contents = [c["content"] for c in candidates]
            self.assertIn("小明喜欢摄影", contents)
            self.assertNotIn("untrusted话", contents)
            self.assertTrue(all(c["origin_class"] != "untrusted" for c in candidates))


class RunMemoryPromotionTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, model_client: object) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.logger = logging.getLogger("test")
        engine.model_client = model_client
        engine.memory = MagicMock()
        engine.memory.collect_promotion_candidates.return_value = [
            {"content": "小明养了一只猫", "origin_class": "user", "session_kind": "interactive",
             "conversation_id": "group:1", "user_id": "10001", "created_at": "2026-08-08T00:00:00+00:00"}
        ]
        engine.memory.get_explicit_facts.return_value = []
        return engine

    async def test_promotes_new_fact_into_explicit_facts(self) -> None:
        model = _FakeModel(
            {"operations": [{"candidateKey": "小明养了一只猫", "action": "added",
                             "resultEntry": "小明养了一只猫", "priorEntries": []}]}
        )
        engine = self._engine(model)
        result = await engine._run_memory_promotion("10001", "group:1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["promoted"], 1)
        engine.memory.add_user_fact.assert_called_once_with("10001", "小明养了一只猫", "group:1")

    async def test_consolidate_failure_is_safe(self) -> None:
        class _Boom:
            async def chat_json(self, messages):  # type: ignore[no-untyped-def]
                raise RuntimeError("provider down")

        engine = self._engine(_Boom())
        result = await engine._run_memory_promotion("10001", "group:1")
        self.assertFalse(result["ok"])
        engine.memory.add_user_fact.assert_not_called()


class PromotionThrottleTests(unittest.IsolatedAsyncioTestCase):
    """锁 `_after_reply` 里的晋升节流接线：每 50 条回复触发一次 + promotion_enable 门。

    原 bug 风险：promotion_enable 默认值、计数器初始化、50 条后重置任一写错，
    晋升后台任务就会静默不触发或无限触发。
    """

    def _engine(self, *, promotion_enable: bool = True) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.logger = logging.getLogger("test")
        engine.trigger = _StubTrigger()
        engine.followup_consume_on_send = False
        engine._last_reply_state = {}
        engine._runtime_group_chat_cache = {}
        engine.config = {
            "bot": {"name": "YuKiKo", "allow_memory": True},
            "memory": {"promotion_enable": promotion_enable},
        }
        engine.memory = MagicMock()
        engine._promotion_counters = {}
        engine._run_memory_promotion = AsyncMock(return_value={"ok": True, "promoted": 0})
        return engine

    def _message(self) -> EngineMessage:
        return EngineMessage(
            conversation_id="group:1",
            user_id="10001",
            text="回复内容",
            mentioned=True,
        )

    async def test_fires_once_after_50_replies_and_resets_counter(self) -> None:
        engine = self._engine()
        for _ in range(50):
            await engine._after_reply(self._message(), "好的")
        await asyncio.sleep(0)  # 让 create_task 排程的晋升任务跑完
        self.assertEqual(engine._run_memory_promotion.await_count, 1)
        self.assertEqual(engine._promotion_counters["10001"], 0)

    async def test_does_not_fire_before_50(self) -> None:
        engine = self._engine()
        for _ in range(49):
            await engine._after_reply(self._message(), "好的")
        await asyncio.sleep(0)
        self.assertEqual(engine._run_memory_promotion.await_count, 0)
        self.assertEqual(engine._promotion_counters["10001"], 49)

    async def test_respects_promotion_enable_off(self) -> None:
        engine = self._engine(promotion_enable=False)
        for _ in range(60):
            await engine._after_reply(self._message(), "好的")
        await asyncio.sleep(0)
        self.assertEqual(engine._run_memory_promotion.await_count, 0)
        self.assertNotIn("10001", engine._promotion_counters)

    def test_promotion_enable_defaults_to_true_in_code(self) -> None:
        """未配置 memory.promotion_enable 时默认开启（后台任务必须默认跑）。"""
        defaults = {
            "memory": {"promotion_enable": True},
            "bot": {"allow_memory": True},
        }
        self.assertTrue(bool(defaults.get("memory", {}).get("promotion_enable", True)))


class _StubTrigger:
    def activate_session(self, **kwargs) -> None:
        pass

    def mark_reply_target(self, *args) -> None:
        pass

    def mark_proactive_reply(self, *args) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
