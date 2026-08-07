"""Phase 4a：上下文插件槽回归测试。

锁四件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.1（5））：
1. DefaultContextEngine 满足 ContextEngine Protocol（四生命周期）。
2. assemble 复用 MemoryEngine 的最近文本，估算 token。
3. compact 返回 no_compactor（YuKiKo 无压缩机制，占位不谎报）。
4. 方法异常 → 记录 quarantine 并降级，不抛。
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from core.context import ContextEngine, DefaultContextEngine


class DefaultContextEngineTests(unittest.TestCase):
    def test_implements_protocol(self) -> None:
        self.assertIsInstance(DefaultContextEngine(None), ContextEngine)

    def test_assemble_uses_memory_recent_texts(self) -> None:
        memory = MagicMock()
        memory.get_recent_texts.return_value = ["最近1", "最近2"]
        engine = DefaultContextEngine(memory)
        result = engine.assemble("group:1", [])
        self.assertIn("最近1", result.messages)
        self.assertIn("最近2", result.messages)
        self.assertGreater(result.estimated_tokens, 0)

    def test_no_memory_assemble_returns_empty(self) -> None:
        engine = DefaultContextEngine(None)
        result = engine.assemble("group:1", [])
        self.assertEqual(result.messages, [])
        self.assertEqual(result.prompt_authority, "no_memory")

    def test_compact_returns_no_compactor(self) -> None:
        engine = DefaultContextEngine(None)
        result = engine.compact("group:1")
        self.assertTrue(result.ok)
        self.assertFalse(result.compacted)
        self.assertEqual(result.reason, "no_compactor")

    def test_ingest_without_memory_is_noop(self) -> None:
        engine = DefaultContextEngine(None)
        self.assertFalse(engine.ingest("group:1", "msg"))

    def test_memory_error_quarantines_and_degrades(self) -> None:
        memory = MagicMock()
        memory.get_recent_texts.side_effect = RuntimeError("boom")
        engine = DefaultContextEngine(memory)
        result = engine.assemble("group:1", [])
        self.assertIn("group:1", engine.quarantined)
        self.assertEqual(result.messages, [])

    def test_quarantine_is_per_session(self) -> None:
        memory = MagicMock()
        memory.get_recent_texts.side_effect = RuntimeError("boom")
        engine = DefaultContextEngine(memory)
        engine.assemble("group:1", [])
        self.assertIn("group:1", engine.quarantined)
        self.assertNotIn("private:1", engine.quarantined)


if __name__ == "__main__":
    unittest.main()
