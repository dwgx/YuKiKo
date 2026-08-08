"""H3 记忆强化：FTS5 会话全文检索镜像 + agent 自主整理提示。

对照 Hermes 的全量会话 FTS5 检索，锁四件事（判据落在真实调用与 SQLite 行为上）：
1. add_message / add_memory_record 写入后，search_message_fts 能按关键词搜到
   （trigram 子串命中；不足 3 字符的查询词退化为 LIKE，仍能搜到）。
2. 多词查询是 AND 语义；delete / update 后 FTS5 镜像同步移除/替换。
3. search_related 向量检索无结果时兜底走 FTS5（recall_intent 车道 2 的补充），
   且显式传入 min_score 时不被关键词结果绕过。
4. memory_list 条目数超限（>200）时 display 附「可用 memory_delete 删除」提示。
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

import core.memory as memory_module
from core.agent_tools_memory import _register_memory_tools
from core.agent_tools_registry import AgentToolRegistry
from core.memory import MemoryEngine


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


def _make_memory(root: Path, **config: object) -> MemoryEngine:
    cfg: dict[str, object] = {"enable_daily_log": False}
    cfg.update(config)
    return MemoryEngine(
        cfg,
        root / "memory",
        global_config={"control": {"heuristic_rules_enable": True}},
    )


class MemoryFtsMirrorTests(unittest.TestCase):
    """FTS5 镜像：写入/删除/更新同步 + 关键词检索。"""

    def setUp(self) -> None:
        _reset_thread_conns()
        self._tmp = tempfile.TemporaryDirectory()
        self.memory = _make_memory(Path(self._tmp.name))

    def tearDown(self) -> None:
        _reset_thread_conns()
        self._tmp.cleanup()

    def test_fts_table_and_triggers_exist_after_init(self) -> None:
        with self.memory._connect() as conn:
            table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings_fts';").fetchone()
            self.assertIsNotNone(table, "FTS5 镜像表没建起来")
            for trigger in ("embeddings_fts_ai", "embeddings_fts_ad", "embeddings_fts_au"):
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?;", (trigger,)
                ).fetchone()
                self.assertIsNotNone(row, f"同步触发器 {trigger} 缺失")

    def test_add_message_is_searchable_via_fts(self) -> None:
        self.memory.add_message("group:1", "10001", "user", "用户喜欢喝冰美式")
        hits = self.memory.search_message_fts("冰美式")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["conversation_id"], "group:1")
        self.assertEqual(hits[0]["user_id"], "10001")
        self.assertIn("冰美式", hits[0]["content"])

    def test_add_memory_record_is_searchable_via_fts(self) -> None:
        ok, message, _ = self.memory.add_memory_record(
            conversation_id="group:1",
            user_id="10001",
            role="user",
            content="用户明年考研计划",
            actor="test",
            note="test",
        )
        self.assertTrue(ok, message)
        hits = self.memory.search_message_fts("考研计划")
        self.assertEqual(len(hits), 1)
        self.assertIn("考研计划", hits[0]["content"])

    def test_short_query_falls_back_to_like(self) -> None:
        """trigram 无法匹配不足 3 字符的查询词，此时必须退化为 LIKE 仍能搜到。"""
        self.memory.add_message("group:1", "10001", "user", "用户在准备考研")
        hits = self.memory.search_message_fts("考研")
        self.assertEqual(len(hits), 1)
        self.assertIn("考研", hits[0]["content"])

    def test_multi_token_query_is_and_semantics(self) -> None:
        self.memory.add_message("group:1", "10001", "user", "用户喜欢喝冰美式")
        self.memory.add_message("group:1", "10001", "user", "用户喜欢喝拿铁咖啡")
        self.memory.add_message("group:1", "10001", "user", "冰美式配拿铁咖啡")
        hits = self.memory.search_message_fts("冰美式 拿铁咖啡")
        self.assertEqual(len(hits), 1)
        self.assertIn("配", hits[0]["content"])
        self.assertNotIn("用户喜欢喝冰美式", [h["content"] for h in hits])

    def test_fts_scope_filters_conversation_and_user(self) -> None:
        self.memory.add_message("group:1", "10001", "user", "用户喜欢喝冰美式")
        self.memory.add_message("group:2", "10001", "user", "用户喜欢喝冰美式")
        self.memory.add_message("group:1", "10002", "user", "用户喜欢喝冰美式")
        self.assertEqual(len(self.memory.search_message_fts("冰美式", conversation_id="group:1")), 2)
        self.assertEqual(
            len(self.memory.search_message_fts("冰美式", conversation_id="group:1", user_id="10001")),
            1,
        )

    def test_delete_syncs_fts_entry(self) -> None:
        self.memory.add_message("group:1", "10001", "user", "用户喜欢喝冰美式")
        hit = self.memory.search_message_fts("冰美式")[0]
        ok, message, _ = self.memory.delete_memory_record(record_id=int(hit["id"]), actor="test", note="测试删除")
        self.assertTrue(ok, message)
        self.assertEqual(self.memory.search_message_fts("冰美式"), [])

    def test_update_syncs_fts_entry(self) -> None:
        self.memory.add_message("group:1", "10001", "user", "用户喜欢喝冰美式")
        hit = self.memory.search_message_fts("冰美式")[0]
        ok, message, _ = self.memory.update_memory_record(
            record_id=int(hit["id"]),
            content="用户喜欢喝拿铁咖啡",
            actor="test",
            note="测试更新",
        )
        self.assertTrue(ok, message)
        self.assertEqual(self.memory.search_message_fts("冰美式"), [])
        hits = self.memory.search_message_fts("拿铁咖啡")
        self.assertEqual(len(hits), 1)
        self.assertIn("拿铁咖啡", hits[0]["content"])

    def test_legacy_rows_are_backfilled_on_first_init(self) -> None:
        """旧库（建 FTS 前已有消息）首次初始化时，存量行必须回填进镜像。"""
        root = Path(self._tmp.name)
        first = _make_memory(root)
        first.add_message("group:1", "10001", "user", "存量消息提到银河护卫队")
        first._flush_vector_buffer()
        first.close()
        _reset_thread_conns()

        # 模拟旧库：删掉 FTS 表与触发器，让重开时走「新建 + rebuild 回填」路径。
        with sqlite3.connect(str(first.db_path)) as conn:
            conn.execute("DROP TRIGGER IF EXISTS embeddings_fts_ai;")
            conn.execute("DROP TRIGGER IF EXISTS embeddings_fts_ad;")
            conn.execute("DROP TRIGGER IF EXISTS embeddings_fts_au;")
            conn.execute("DROP TABLE IF EXISTS embeddings_fts;")
        _reset_thread_conns()

        reopened = _make_memory(root)
        hits = reopened.search_message_fts("银河护卫队")
        self.assertEqual(len(hits), 1)
        self.assertIn("银河护卫队", hits[0]["content"])


class MemoryFtsFallbackTests(unittest.TestCase):
    """search_related 向量无结果时的 FTS5 兜底。"""

    def setUp(self) -> None:
        _reset_thread_conns()
        self._tmp = tempfile.TemporaryDirectory()
        self.memory = _make_memory(Path(self._tmp.name))

    def tearDown(self) -> None:
        _reset_thread_conns()
        self._tmp.cleanup()

    def test_search_related_falls_back_to_fts_when_vector_empty(self) -> None:
        # 无空格英文串在词袋向量里是单一 token，与 "morph" 零重叠 → 向量必然为空；
        # trigram 子串 "morph" 能命中 → 兜底生效。
        self.memory.add_message("group:1", "10001", "user", "xenomorphlaserquest")
        results = self.memory.search_related("group:1", "morph", roles=("user",), user_id="10001")
        self.assertEqual(results, ["xenomorphlaserquest"])

    def test_search_related_fts_fallback_keeps_non_match_empty(self) -> None:
        self.memory.add_message("group:1", "10001", "user", "xenomorphlaserquest")
        results = self.memory.search_related("group:1", "zzzqqqwww", roles=("user",), user_id="10001")
        self.assertEqual(results, [])

    def test_explicit_min_score_skips_fts_fallback(self) -> None:
        """显式抬高 min_score 表示要求严格语义召回，关键词结果不得绕过。"""
        self.memory.add_message("group:1", "10001", "user", "xenomorphlaserquest")
        results = self.memory.search_related("group:1", "morph", roles=("user",), user_id="10001", min_score=0.9)
        self.assertEqual(results, [])

    def test_vector_hit_takes_priority_over_fts(self) -> None:
        self.memory.add_message("group:1", "10001", "user", "封我，试试")
        results = self.memory.search_related("group:1", "封我，这个人", roles=("user",), user_id="10001")
        self.assertEqual(results, ["封我，试试"])


class MemoryListOverflowHintTests(unittest.TestCase):
    """memory_list 超限提示（agent 自主整理入口）。"""

    @classmethod
    def setUpClass(cls) -> None:
        registry = AgentToolRegistry()
        _register_memory_tools(registry)
        cls.registry = registry

    def setUp(self) -> None:
        _reset_thread_conns()
        self._tmp = tempfile.TemporaryDirectory()
        self.memory = _make_memory(Path(self._tmp.name))

    def tearDown(self) -> None:
        _reset_thread_conns()
        self._tmp.cleanup()

    def _call(self, args: dict[str, object]) -> object:
        context = {
            "permission_level": "super_admin",
            "user_id": "10001",
            "conversation_id": "group:1",
            "memory_engine": self.memory,
        }
        return asyncio.run(self.registry.call("memory_list", args, context))

    def test_memory_list_shows_overflow_hint_when_over_200(self) -> None:
        for i in range(201):
            ok, message, _ = self.memory.add_memory_record(
                conversation_id="group:1",
                user_id="10001",
                role="user",
                content=f"测试记忆条目 {i}",
                actor="test",
                note="test",
            )
            self.assertTrue(ok, message)
        result = self._call({"limit": 30, "page": 1})
        self.assertTrue(result.ok)
        self.assertIn("记忆条目过多", result.display)
        self.assertIn("memory_delete", result.display)
        self.assertIn("memory_compact", result.display)

    def test_memory_list_no_hint_below_threshold(self) -> None:
        for i in range(10):
            self.memory.add_memory_record(
                conversation_id="group:1",
                user_id="10001",
                role="user",
                content=f"测试记忆条目 {i}",
                actor="test",
                note="test",
            )
        result = self._call({"limit": 30, "page": 1})
        self.assertTrue(result.ok)
        self.assertNotIn("记忆条目过多", result.display)


if __name__ == "__main__":
    unittest.main()
