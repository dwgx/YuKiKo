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
from unittest.mock import MagicMock

from core.engine import YukikoEngine
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


if __name__ == "__main__":
    unittest.main()
