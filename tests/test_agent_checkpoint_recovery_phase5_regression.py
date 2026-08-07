"""Phase 5b 延伸：checkpoint 完整恢复回归测试。

锁三件事：
1. AgentTurnCheckpoint 保存/恢复 step_idx/messages/steps（超时重试可续跑）。
2. TTL 过期当作不存在 + cleanup_expired 清理。
3. 缺失/损坏 checkpoint 安全降级（返回 None，不抛）。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from core.agent_checkpoint import AgentTurnCheckpoint


class AgentTurnCheckpointTests(unittest.TestCase):
    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = AgentTurnCheckpoint(Path(tmp))
            ok = cp.save(
                trace_id="t1",
                step_idx=2,
                messages=[{"role": "user", "content": "hi"}],
                steps=[{"step": 0, "tool": "x", "ok": True}],
            )
            self.assertTrue(ok)
            loaded = cp.load("t1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["step_idx"], 2)
            self.assertEqual(loaded["messages"][0]["content"], "hi")
            self.assertEqual(loaded["steps"][0]["tool"], "x")

    def test_load_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = AgentTurnCheckpoint(Path(tmp))
            self.assertIsNone(cp.load("nope"))

    def test_clear_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = AgentTurnCheckpoint(Path(tmp))
            cp.save(trace_id="t1", step_idx=0, messages=[], steps=[])
            self.assertIsNotNone(cp.load("t1"))
            cp.clear("t1")
            self.assertIsNone(cp.load("t1"))

    def test_corrupt_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t1.json"
            path.write_text("{not json", encoding="utf-8")
            cp = AgentTurnCheckpoint(Path(tmp))
            self.assertIsNone(cp.load("t1"))

    def test_ttl_expired_returns_none_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t1.json"
            path.write_text(
                json.dumps({"step_idx": 0, "messages": [], "steps": [], "saved_at": time.time() - 9999}),
                encoding="utf-8",
            )
            cp = AgentTurnCheckpoint(Path(tmp), ttl_seconds=60)
            self.assertIsNone(cp.load("t1"))

    def test_cleanup_expired_removes_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "stale.json"
            fresh = Path(tmp) / "fresh.json"
            stale.write_text(
                json.dumps({"step_idx": 0, "messages": [], "steps": [], "saved_at": time.time() - 9999}),
                encoding="utf-8",
            )
            fresh.write_text(
                json.dumps({"step_idx": 0, "messages": [], "steps": [], "saved_at": time.time()}),
                encoding="utf-8",
            )
            cp = AgentTurnCheckpoint(Path(tmp), ttl_seconds=60)
            removed = cp.cleanup_expired()
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
