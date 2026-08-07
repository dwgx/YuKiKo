"""Phase 0 记忆来源分级（provenance）：untrusted 结构性排除出 Curated 层。

锁三件事（对应 OpenClaw 的 provenance 分级，见 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.1）：
1. 群聊未指向 bot 的发言（untrusted）不自动学习 explicit_fact / preferred_name。
2. 群聊 @bot / 私聊（user）的发言正常自动学习。
3. embeddings 每条带 `origin_class` 列，untrusted 消息标 untrusted；旧库缺列时迁移补列。

判据落在真实调用与 SQLite 结构上，不做源码子串匹配。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import core.memory as memory_module
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


def _directed_meta() -> dict[str, object]:
    return {
        "is_private": False,
        "mentioned": True,
        "explicit_bot_addressed": True,
        "bot_id": "bot",
    }


def _undirected_meta() -> dict[str, object]:
    return {
        "is_private": False,
        "mentioned": False,
        "explicit_bot_addressed": False,
        "bot_id": "bot",
    }


class MemoryProvenanceCuratedGateTests(unittest.TestCase):
    """untrusted（群聊未指向）不进 Curated 层；user（@bot/私聊）正常进。"""

    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    def test_untrusted_group_message_does_not_learn_explicit_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            memory.add_message(
                "group:1", "10001", "user", "记住，我住在杭州",
                user_name="小明",
                metadata=_undirected_meta(),
            )
            self.assertEqual(memory.get_explicit_facts("10001"), [])

    def test_directed_group_message_learns_explicit_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            memory.add_message(
                "group:1", "10001", "user", "记住，我住在杭州",
                user_name="小明",
                metadata=_directed_meta(),
            )
            self.assertEqual(memory.get_explicit_facts("10001"), ["我住在杭州"])

    def test_private_message_learns_explicit_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            memory.add_message(
                "private:10001", "10001", "user", "记住，我喜欢摄影",
                user_name="小明",
                metadata={"is_private": True},
            )
            self.assertEqual(memory.get_explicit_facts("10001"), ["我喜欢摄影"])

    def test_untrusted_group_message_does_not_learn_preferred_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(
                Path(tmp),
                preferred_name_patterns=[
                    r"(?:以后)?(?:叫我|喊我|称呼我)(?P<name>[^，。！？!?]{1,12})$"
                ],
            )
            memory.add_message(
                "group:1", "10001", "user", "以后叫我阿背",
                user_name="小明",
                metadata=_undirected_meta(),
            )
            self.assertEqual(memory.get_preferred_name("10001", fallback_name="小明"), "小明")

    def test_directed_group_message_learns_preferred_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(
                Path(tmp),
                preferred_name_patterns=[
                    r"(?:以后)?(?:叫我|喊我|称呼我)(?P<name>[^，。！？!?]{1,12})$"
                ],
            )
            memory.add_message(
                "group:1", "10001", "user", "以后叫我阿背",
                user_name="小明",
                metadata=_directed_meta(),
            )
            self.assertEqual(memory.get_preferred_name("10001", fallback_name="小明"), "阿背")

    def test_classify_origin_is_structural(self) -> None:
        """来源判定只看结构（is_private/mentioned/explicit），不碰文本语义。"""
        self.assertEqual(MemoryEngine._classify_origin({"is_private": True}), "user")
        self.assertEqual(MemoryEngine._classify_origin(_directed_meta()), "user")
        self.assertEqual(MemoryEngine._classify_origin(_undirected_meta()), "untrusted")
        self.assertEqual(MemoryEngine._classify_origin(None), "untrusted")


class MemoryProvenanceEmbeddingsColumnTests(unittest.TestCase):
    """embeddings 表带 origin_class 列；untrusted 消息标 untrusted；旧库迁移补列。"""

    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    def test_embeddings_row_records_origin_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            memory.add_message(
                "group:1", "10001", "user", "路过一句话",
                user_name="路人",
                metadata=_undirected_meta(),
            )
            memory._flush_vector_buffer()
            with memory._connect() as conn:
                row = conn.execute(
                    "SELECT origin_class FROM embeddings WHERE content = ?;",
                    ("路过一句话",),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(str(row[0]), "untrusted")

    def test_origin_class_column_exists_on_fresh_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _make_memory(Path(tmp))
            with memory._connect() as conn:
                cols = {
                    str(r[1])
                    for r in conn.execute("PRAGMA table_info(embeddings);").fetchall()
                }
            self.assertIn("origin_class", cols)

    def test_legacy_db_migrates_origin_class_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = _make_memory(root)
            db_path = memory.db_path
            memory.close()
            _reset_thread_conns()
            # 手工删掉 origin_class 列，模拟旧库（Phase 0 之前建的表没有这列）。
            legacy = sqlite3.connect(str(db_path))
            try:
                legacy.execute("DROP INDEX IF EXISTS idx_embeddings_unique;")
                legacy.execute("ALTER TABLE embeddings DROP COLUMN origin_class;")
                legacy.commit()
            finally:
                legacy.close()
            _reset_thread_conns()

            reopened = _make_memory(root)
            with reopened._connect() as conn:
                cols = {
                    str(r[1])
                    for r in conn.execute("PRAGMA table_info(embeddings);").fetchall()
                }
            self.assertIn("origin_class", cols)


if __name__ == "__main__":
    unittest.main()
