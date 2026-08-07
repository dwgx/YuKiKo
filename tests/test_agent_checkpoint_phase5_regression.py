"""Phase 5b：AgentLoop 轻量 checkpoint 回归测试。

锁三件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md Phase 5b）：
1. AgentStepJournal 记录每步工具调用，内存快照可回溯。
2. JSONL 落盘后可重载（跨重启持久）。
3. 落盘失败不抛（不阻塞主流程）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.agent_checkpoint import AgentStepJournal


class AgentStepJournalTests(unittest.TestCase):
    def test_record_and_snapshot(self) -> None:
        journal = AgentStepJournal()
        journal.record(trace_id="t1", step=0, tool="search", ok=True)
        journal.record(trace_id="t1", step=1, tool="final_answer", ok=True, error="")
        rows = journal.snapshot()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tool"], "search")
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[1]["trace_id"], "t1")

    def test_persists_to_jsonl_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steps.jsonl"
            journal = AgentStepJournal(path)
            journal.record(trace_id="t9", step=0, tool="search", ok=False, error="boom")
            reloaded = AgentStepJournal(path)
            rows = reloaded.load()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["tool"], "search")
            self.assertFalse(rows[0]["ok"])
            self.assertEqual(rows[0]["error"], "boom")

    def test_memory_lines_bounded(self) -> None:
        journal = AgentStepJournal(max_memory_lines=2)
        for i in range(5):
            journal.record(trace_id="t", step=i, tool="x", ok=True)
        self.assertEqual(len(journal.snapshot()), 2)

    def test_write_failure_does_not_raise(self) -> None:
        # path 指向不可写位置：record 不应抛。
        journal = AgentStepJournal(Path("/nonexistent_dir_xyz/steps.jsonl"))
        journal.record(trace_id="t", step=0, tool="x", ok=True)  # 不抛
        self.assertEqual(len(journal.snapshot()), 1)

    def test_empty_load_returns_snapshot(self) -> None:
        journal = AgentStepJournal()
        self.assertEqual(journal.load(), [])


if __name__ == "__main__":
    unittest.main()
