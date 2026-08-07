"""Phase 0.5b：类型学补强回归测试。

锁两件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.6）：
1. 记忆注入 token 预算护栏：budget_text_parts 按优先级截断多段拼接，防上下文失控。
2. episodic 结构化范例：LangMem 类型学里 YuKiKo 最弱的 episodic —— 存 query→reply
   有效回复对，供 few-shot 注入。

判据落在真实调用与 SQLite 结构上。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import core.memory as memory_module
from core.memory import MemoryEngine, budget_text_parts


def _reset_thread_conns() -> None:
    conns = getattr(memory_module._db_local, "conns", None)
    if not conns:
        return
    for conn in list(conns.values()):
        try:
            conn.close()
        except Exception:
            pass
    conns.clear()


def _make_memory(root: Path) -> MemoryEngine:
    return MemoryEngine(
        {"enable_daily_log": False},
        root / "memory",
    )


class BudgetTextPartsTests(unittest.TestCase):
    def test_under_budget_returns_all_parts(self) -> None:
        parts = ["A" * 100, "B" * 100]
        out = budget_text_parts(parts, max_chars=1000)
        self.assertEqual(out, "A" * 100 + " " + "B" * 100)

    def test_over_budget_truncates_tail_and_stops(self) -> None:
        parts = ["A" * 100, "B" * 100]
        out = budget_text_parts(parts, max_chars=150)
        self.assertLessEqual(len(out), 150)
        self.assertTrue(out.startswith("A"), out)

    def test_priority_order_keeps_first_parts(self) -> None:
        # 段顺序即优先级：profile 在最前，预算小时 profile 保留、kb/ks 被裁。
        parts = ["profile", "kb" * 50, "ks" * 50]
        out = budget_text_parts(parts, max_chars=20)
        self.assertTrue(out.startswith("profile"), out)

    def test_zero_budget_returns_empty(self) -> None:
        self.assertEqual(budget_text_parts(["abc"], max_chars=0), "")

    def test_negative_budget_returns_empty(self) -> None:
        self.assertEqual(budget_text_parts(["abc"], max_chars=-5), "")

    def test_empty_and_blank_parts_are_skipped(self) -> None:
        self.assertEqual(budget_text_parts(["", "   ", "x"], max_chars=10), "x")

    def test_all_blank_parts_returns_empty(self) -> None:
        self.assertEqual(budget_text_parts([" ", ""], max_chars=10), "")


class EpisodicExampleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    def test_add_and_get_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            mid = memory.add_episodic_example(
                conversation_id="group:1",
                user_id="10001",
                query="怎么请假",
                reply="你可以和主管说一声，然后填请假单。",
                source="agent",
            )
            self.assertGreater(mid, 0)
            rows = memory.get_episodic_examples(conversation_id="group:1", user_id="10001")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["query"], "怎么请假")
            self.assertEqual(rows[0]["reply"], "你可以和主管说一声，然后填请假单。")
            self.assertEqual(rows[0]["source"], "agent")
            self.assertEqual(rows[0]["conversation_id"], "group:1")

    def test_get_filters_by_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            memory.add_episodic_example(
                conversation_id="group:1", user_id="10001", query="a", reply="ra"
            )
            memory.add_episodic_example(
                conversation_id="group:1", user_id="20002", query="b", reply="rb"
            )
            rows = memory.get_episodic_examples(user_id="20002", limit=5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["user_id"], "20002")

    def test_get_returns_most_recent_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            for i in range(3):
                memory.add_episodic_example(
                    conversation_id="group:1", user_id="10001", query=f"q{i}", reply=f"r{i}"
                )
            rows = memory.get_episodic_examples(conversation_id="group:1", limit=2)
            self.assertEqual([r["query"] for r in rows], ["q2", "q1"])

    def test_empty_input_not_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            self.assertEqual(
                memory.add_episodic_example(conversation_id="", user_id="10001", query="a", reply="b"),
                0,
            )
            self.assertEqual(
                memory.add_episodic_example(conversation_id="group:1", user_id="10001", query="", reply="b"),
                0,
            )
            self.assertEqual(
                memory.add_episodic_example(conversation_id="group:1", user_id="10001", query="a", reply=""),
                0,
            )


if __name__ == "__main__":
    unittest.main()
