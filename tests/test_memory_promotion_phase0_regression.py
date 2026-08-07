"""Phase 0.5a：记忆晋升门回归测试。

锁四件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.1（2））：
1. rank_promotion_candidates 结构性排除 untrusted/system/非 interactive 会话。
2. 评分排序：freq/recency/importance 高的排在前面。
3. promotionable_candidates 过滤（min_signal / max_age / min_score）。
4. validate_consolidated_memory + consolidate_memory 的 bounded-loss（失败回退 append-only）。

判据落在真实调用上，不做源码子串匹配。
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from core.memory_promotion import (
    PROMOTION_MIN_SCORE,
    PromotionCandidate,
    apply_operations,
    build_consolidation_prompt,
    consolidate_memory,
    promotionable_candidates,
    rank_promotion_candidates,
    validate_consolidated_memory,
)

_NOW = datetime(2026, 8, 8, tzinfo=UTC)


def _candidate(**over: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "content": "小明住在杭州",
        "origin_class": "user",
        "session_kind": "interactive",
        "created_at": _NOW.isoformat(),
        "signal_count": 5,
        "recall_days": 4,
        "context_diversity": 3,
        "importance": 8.0,
    }
    row.update(over)
    return row


class RankPromotionTests(unittest.TestCase):
    def test_untrusted_and_system_are_blocked(self) -> None:
        rows = [
            _candidate(content="路过一句话", origin_class="untrusted"),
            _candidate(content="系统生成的", origin_class="system"),
        ]
        ranked = rank_promotion_candidates(rows, now=_NOW)
        self.assertTrue(all(c.is_blocked for c in ranked))
        self.assertEqual({c.block_reason for c in ranked}, {"origin_untrusted", "origin_system"})

    def test_non_interactive_session_is_blocked(self) -> None:
        rows = [_candidate(session_kind="cron"), _candidate(session_kind="subagent")]
        ranked = rank_promotion_candidates(rows, now=_NOW)
        self.assertTrue(all(c.is_blocked for c in ranked))
        self.assertEqual({c.block_reason for c in ranked}, {"session_cron", "session_subagent"})

    def test_user_and_private_are_promotionable(self) -> None:
        rows = [
            _candidate(content="小明喜欢摄影"),
            _candidate(content="私聊事实", origin_class="user", session_kind="interactive"),
        ]
        ranked = rank_promotion_candidates(rows, now=_NOW)
        self.assertFalse(any(c.is_blocked for c in ranked))

    def test_higher_signal_scores_higher(self) -> None:
        rows = [
            _candidate(content="低频事实", signal_count=3),
            _candidate(content="高频事实", signal_count=20),
        ]
        ranked = rank_promotion_candidates(rows, now=_NOW)
        self.assertEqual(ranked[0].content, "高频事实")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_recent_scores_higher_than_stale(self) -> None:
        stale = (_NOW - timedelta(days=29)).isoformat()
        rows = [
            _candidate(content="新事实", created_at=_NOW.isoformat()),
            _candidate(content="旧事实", created_at=stale),
        ]
        ranked = rank_promotion_candidates(rows, now=_NOW)
        self.assertEqual(ranked[0].content, "新事实")

    def test_importance_affects_score(self) -> None:
        rows = [
            _candidate(content="低重要性", importance=1.0),
            _candidate(content="高重要性", importance=10.0),
        ]
        ranked = rank_promotion_candidates(rows, now=_NOW)
        self.assertEqual(ranked[0].content, "高重要性")

    def test_promotionable_filters_blocks_signal_and_age(self) -> None:
        stale = (_NOW - timedelta(days=31)).isoformat()
        rows = [
            _candidate(content="应该晋升", signal_count=5),
            _candidate(content="信号不足", signal_count=1),
            _candidate(content="太旧了", created_at=stale, signal_count=10),
            _candidate(content="untrusted", origin_class="untrusted", signal_count=20),
        ]
        out = promotionable_candidates(rows, now=_NOW)
        contents = [c.content for c in out]
        self.assertEqual(contents, ["应该晋升"])

    def test_blocked_candidates_never_reach_promotionable_even_with_high_score(self) -> None:
        rows = [_candidate(content="很热门的起哄", origin_class="untrusted", signal_count=999)]
        self.assertEqual(promotionable_candidates(rows, now=_NOW), [])


class ValidateConsolidationTests(unittest.TestCase):
    EXISTING = ["小明住在杭州", "小明喜欢摄影"]

    def test_valid_operations_pass(self) -> None:
        ops = [
            {"candidateKey": "新事实", "action": "added", "resultEntry": "小明养了一只猫", "priorEntries": []},
            {"candidateKey": "合并", "action": "merged", "resultEntry": "小明住在杭州西湖区", "priorEntries": ["小明住在杭州"]},
        ]
        ok, reason = validate_consolidated_memory(ops, 2, self.EXISTING)
        self.assertTrue(ok, reason)

    def test_operation_count_must_match(self) -> None:
        ops = [{"candidateKey": "a", "action": "added", "resultEntry": "x", "priorEntries": []}]
        ok, reason = validate_consolidated_memory(ops, 2, self.EXISTING)
        self.assertFalse(ok)
        self.assertIn("operation_count_mismatch", reason)

    def test_invalid_action_rejected(self) -> None:
        ops = [{"candidateKey": "a", "action": "deleted", "resultEntry": "x", "priorEntries": []}]
        ok, reason = validate_consolidated_memory(ops, 1, self.EXISTING)
        self.assertFalse(ok)
        self.assertIn("invalid_action", reason)

    def test_prior_must_exist_in_curated(self) -> None:
        ops = [
            {"candidateKey": "a", "action": "superseded", "resultEntry": "新", "priorEntries": ["不存在的条目"]}
        ]
        ok, reason = validate_consolidated_memory(ops, 1, self.EXISTING)
        self.assertFalse(ok)
        self.assertIn("prior_not_in_existing", reason)

    def test_prior_loss_fraction_bounded(self) -> None:
        # 2 条现有，superseded 2 条 = 丢失比 1.0 > 0.25
        ops = [
            {"candidateKey": "a", "action": "superseded", "resultEntry": "新1", "priorEntries": ["小明住在杭州"]},
            {"candidateKey": "b", "action": "superseded", "resultEntry": "新2", "priorEntries": ["小明喜欢摄影"]},
        ]
        ok, reason = validate_consolidated_memory(ops, 2, self.EXISTING)
        self.assertFalse(ok)
        self.assertIn("prior_loss_exceeded", reason)

    def test_curated_budget_exceeded(self) -> None:
        ops = [{"candidateKey": "a", "action": "added", "resultEntry": "很" * 6000, "priorEntries": []}]
        ok, reason = validate_consolidated_memory(ops, 1, self.EXISTING, max_chars=100)
        self.assertFalse(ok)
        self.assertIn("curated_budget_exceeded", reason)


class ConsolidateMemoryTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, payload: dict[str, Any]) -> Any:
        class _Fake:
            async def chat_json(self, messages):  # type: ignore[no-untyped-def]
                self.messages = messages
                return payload

        return _Fake()

    async def test_success_path(self) -> None:
        candidates = [
            PromotionCandidate(content="小明养了一只猫", origin_class="user", importance=8.0)
        ]
        client = self._client(
            {"operations": [{"candidateKey": "小明养了一只猫", "action": "added", "resultEntry": "小明养了一只猫", "priorEntries": []}]}
        )
        result = await consolidate_memory(candidates, ["小明住在杭州"], model_client=client)
        self.assertTrue(result["ok"])
        self.assertFalse(result["append_only"])

    async def test_model_failure_falls_back_append_only(self) -> None:
        candidates = [PromotionCandidate(content="事实", origin_class="user")]

        class _Boom:
            async def chat_json(self, messages):  # type: ignore[no-untyped-def]
                raise RuntimeError("provider down")

        result = await consolidate_memory(candidates, ["现有"], model_client=_Boom())
        self.assertFalse(result["ok"])
        self.assertTrue(result["append_only"])
        self.assertIn("consolidation_call_failed", result["error"])

    async def test_invalid_output_falls_back_append_only(self) -> None:
        candidates = [PromotionCandidate(content="事实", origin_class="user")]
        client = self._client({"operations": []})  # 数量不匹配
        result = await consolidate_memory(candidates, ["现有"], model_client=client)
        self.assertFalse(result["ok"])
        self.assertTrue(result["append_only"])

    async def test_prompt_contains_candidates_and_existing(self) -> None:
        candidates = [PromotionCandidate(content="小明养猫", origin_class="user")]
        prompt = build_consolidation_prompt(candidates, ["小明住在杭州"])
        self.assertIn("小明养猫", prompt)
        self.assertIn("小明住在杭州", prompt)


class ApplyOperationsTests(unittest.TestCase):
    def test_apply_added_and_superseded(self) -> None:
        ops = [
            {"candidateKey": "a", "action": "added", "resultEntry": "新事实", "priorEntries": []},
            {"candidateKey": "b", "action": "superseded", "resultEntry": "小明住在杭州西湖区", "priorEntries": ["小明住在杭州"]},
        ]
        out = apply_operations(ops, ["小明住在杭州", "小明喜欢摄影"])
        self.assertIn("新事实", out)
        self.assertNotIn("小明住在杭州", out)
        self.assertIn("小明住在杭州西湖区", out)
        self.assertIn("小明喜欢摄影", out)


if __name__ == "__main__":
    unittest.main()
