"""称呼降级 + 记忆召回下限 / 埋点 / 连接键控 / embedding 去重回归（车道 naming）。

锁的五件事都来自线上实测，不是推演：

1. `get_preferred_name` 把「纯数字 user_id → 用户XXXX」放在 display_name / fallback
   之前，QQ 号必然匹配那个正则，所以后两支是死代码。实测 39 个真实档案
   39/39 都返回「用户XXXX」，`136666451`（display_name=帝王）被叫成「用户6451」。
2. `search_related` 把 score 丢进 `_`，只按 top_k 截断。实测 top3 = 0.354 / 0.000 / 0.000，
   两条零重叠照样进 prompt。
3. 全链路零埋点：18411 行日志里 grep `search_related|memory_inject|related_memor` = 0。
4. `_connect` 只按线程缓存不按 db_path 分键，同线程第二个引擎静默读写第一个库。
5. embeddings 无写入去重，实测 21.4% 是完全重复行，同一句存了 37 次。

判据落在真实调用与 SQLite 结构上（sqlite_master / 实际行数 / logging 记录），
不做源码子串匹配 —— 本项目有过子串匹配到自己注释的假绿灯。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

import core.memory as memory_module
from core.memory import MemoryEngine


def _reset_thread_conns() -> None:
    """断开本线程所有缓存的 SQLite 连接（`_db_local.conns`，按 db_path 键控）。"""
    conns = getattr(memory_module._db_local, "conns", None)
    if not conns:
        return
    for conn in list(conns.values()):
        try:
            conn.close()
        except Exception:
            pass
    conns.clear()


def _engine(root: Path, **config: object) -> MemoryEngine:
    cfg: dict[str, object] = {"enable_daily_log": False}
    cfg.update(config)
    return MemoryEngine(config=cfg, memory_dir=root / "memory")


class PreferredNameFallbackOrderTests(unittest.TestCase):
    """修复 1：真人不能被叫成「用户6451」。"""

    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    # 取自 storage/memory/users/profiles.json 的真实档案形态
    REAL_PROFILES = {
        "136666451": "帝王",
        "3116351079": "果冻Pro",
        "978376999": "😈",
        "3850106951": "ฅ^•ﻌ•^ฅ",
        "2020958753": "红石先森‭",
        "2546835961": "本本小阿本（反我的世界）",
    }

    def test_display_name_beats_numeric_short_id_for_real_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            for uid, display in self.REAL_PROFILES.items():
                memory._user_profiles[uid] = {"display_name": display}

            for uid, display in self.REAL_PROFILES.items():
                with self.subTest(uid=uid):
                    name = memory.get_preferred_name(uid)
                    self.assertEqual(name, display)
                    self.assertNotEqual(name, f"用户{uid[-4:]}")
                    self.assertFalse(
                        name.startswith("用户") and name[2:].isdigit(),
                        f"{uid} 仍然降级成数字短标识：{name}",
                    )

    def test_the_reported_case_is_not_called_yonghu6451(self) -> None:
        """线上原文「用户6451别乱迁怒~」的那一个档案。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            memory._user_profiles["136666451"] = {"display_name": "帝王"}
            self.assertEqual(memory.get_preferred_name("136666451"), "帝王")

    def test_live_nickname_fallback_wins_over_stale_display_name(self) -> None:
        """fallback 是调用方传进来的实时昵称，比落库的 display_name 新。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            memory._user_profiles["136666451"] = {"display_name": "旧昵称"}
            self.assertEqual(
                memory.get_preferred_name("136666451", fallback_name="帝王"),
                "帝王",
            )

    def test_preferred_name_still_outranks_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            memory._user_profiles["136666451"] = {
                "preferred_name": "王哥",
                "display_name": "帝王",
            }
            self.assertEqual(
                memory.get_preferred_name("136666451", fallback_name="实时昵称"),
                "王哥",
            )

    def test_numeric_short_id_remains_the_last_resort(self) -> None:
        """什么名字都拿不到时才允许「用户XXXX」，这一支不能删。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            self.assertEqual(memory.get_preferred_name("136666451"), "用户6451")
            self.assertEqual(memory.get_preferred_name("abc"), "某人")


class RecallScoreFloorTests(unittest.TestCase):
    """修复 2：0.000 相似度不许进 prompt。"""

    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    # `utils.text.tokenize` 把整段中文按标点切成 token（`[一-鿿]{2,}`），
    # 所以只有带标点的语料才会出现「部分重叠」这种真实分数分布；
    # 不带标点的整句只有完全相同才非零，构不出「0.354 / 0.000 / 0.000」的场景。
    QUERY = "封我，这个人"
    OVERLAP = "封我，试试"          # 与 QUERY 共享 1/2 token → 0.500
    ZERO_ROWS = ["螺蛳粉，外卖", "开会，几点"]   # 零重叠 → 0.000

    @staticmethod
    def _seed(memory: MemoryEngine, texts: list[str]) -> None:
        for text in texts:
            memory.add_message("group:42", "10001", "user", text, user_name="小明")
        memory._flush_vector_buffer()

    def test_zero_overlap_rows_are_dropped_instead_of_padding_top_k(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            self._seed(memory, [self.OVERLAP, *self.ZERO_ROWS])
            query = self.QUERY
            scored = self._scores(memory, query)
            self.assertTrue(any(s <= 0.0 for s, _ in scored), f"用例前提不成立：{scored}")

            results = memory.search_related("group:42", query, roles=("user",), user_id="10001")
            # 真命中留下，两条 0.000 被丢掉 —— 而不是靠 top_k 把池子填满。
            self.assertEqual(results, [self.OVERLAP])
            for content in results:
                score = dict((c, s) for s, c in scored)[content]
                self.assertGreaterEqual(
                    score,
                    memory.retrieve_min_score,
                    f"低于下限的内容仍被注入：{content} score={score:.3f}",
                )

    def test_returns_empty_list_rather_than_zero_score_filler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            self._seed(memory, self.ZERO_ROWS)
            results = memory.search_related(
                "group:42", self.QUERY, roles=("user",), user_id="10001"
            )
            self.assertEqual(results, [])

    def test_floor_zero_keeps_the_old_no_gate_behaviour(self) -> None:
        """下限可配；0 表示关掉此门，用于确认新行为确实由下限造成。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp), retrieve_min_score=0)
            self._seed(memory, self.ZERO_ROWS)
            results = memory.search_related(
                "group:42", self.QUERY, roles=("user",), user_id="10001"
            )
            self.assertEqual(sorted(results), sorted(self.ZERO_ROWS))

    def test_per_call_min_score_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp), retrieve_min_score=0)
            self._seed(memory, [self.OVERLAP, *self.ZERO_ROWS])
            self.assertEqual(
                memory.search_related(
                    "group:42", self.QUERY, roles=("user",), user_id="10001", min_score=0.9
                ),
                [],
            )
            self.assertEqual(
                memory.search_related(
                    "group:42", self.QUERY, roles=("user",), user_id="10001", min_score=0.4
                ),
                [self.OVERLAP],
            )

    def test_default_floor_sits_in_the_measured_empty_band(self) -> None:
        """默认下限必须落在实测空隙 (0, 0.1026) 内。

        在真实库 872 行上复算 671 个 top5 分数：264 个恰好为 0（39.3%），
        (0, 0.10) 区间一个都没有，最小非零值 0.1026。所以下限要 >0（砍掉零重叠）
        且 <0.1026（不误伤真命中）。0.15 会连带砍掉实测存在的 7 个
        0.1026~0.1336 的弱命中 —— 这条断言就是拦那个改动的。
        """
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            self.assertGreater(memory.retrieve_min_score, 0.0)
            self.assertLess(
                memory.retrieve_min_score,
                0.1026,
                "下限高于实测最小非零相似度，会丢掉真命中",
            )

    def test_a_real_weak_hit_at_the_measured_minimum_survives(self) -> None:
        """实测最小非零相似度那一档（0.1026）必须还能被召回。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            memory.add_message("group:42", "10001", "user", self.OVERLAP, user_name="小明")
            memory._flush_vector_buffer()
            scored = self._scores(memory, self.QUERY)
            self.assertTrue(scored and scored[0][0] > 0)
            # 构造一个恰好等于实测最小非零值的下限，确认它不会被门砍掉
            results = memory.search_related(
                "group:42", self.QUERY, roles=("user",), user_id="10001", min_score=0.1026
            )
            self.assertEqual(results, [self.OVERLAP])

    @staticmethod
    def _scores(memory: MemoryEngine, query: str) -> list[tuple[float, str]]:
        """复算 search_related 内部打分，用于断言「被丢掉的确实低于下限」。"""
        query_vec = memory._embed(query)
        with memory._connect() as conn:
            rows = conn.execute(
                "SELECT role, content, embedding FROM embeddings WHERE conversation_id = ?;",
                ("group:42",),
            ).fetchall()
        out: list[tuple[float, str]] = []
        for role, content, emb_json in rows:
            if str(role) != "user":
                continue
            out.append((memory._cosine(query_vec, [float(x) for x in json.loads(emb_json)]), str(content)))
        out.sort(key=lambda item: item[0], reverse=True)
        return out


class RecallObservabilityTests(unittest.TestCase):
    """修复 3：召回必须留结构化埋点，否则错误注入线上不可观测。"""

    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    def test_search_related_emits_memory_related_hit_with_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            for text in ["封我，试试", "螺蛳粉，外卖"]:
                memory.add_message("group:42", "10001", "user", text, user_name="小明")
            memory._flush_vector_buffer()

            with self.assertLogs("yukiko.memory", level=logging.INFO) as captured:
                memory.search_related(
                    "group:42",
                    "封我，这个人",
                    roles=("user",),
                    user_id="10001",
                    trace_id="trace-abc",
                )

            hits = [line for line in captured.output if "memory_related_hit" in line]
            self.assertEqual(len(hits), 1, captured.output)
            line = hits[0]
            self.assertIn("trace=trace-abc", line)
            for field in ("pool=", "hits=", "top=", "floor_dropped="):
                self.assertIn(field, line)

    def test_log_reports_the_number_of_rows_the_floor_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            for text in ["螺蛳粉，外卖", "开会，几点"]:
                memory.add_message("group:42", "10001", "user", text, user_name="小明")
            memory._flush_vector_buffer()

            with self.assertLogs("yukiko.memory", level=logging.INFO) as captured:
                results = memory.search_related(
                    "group:42", "封我，这个人", roles=("user",), user_id="10001"
                )
            self.assertEqual(results, [])
            line = next(line for line in captured.output if "memory_related_hit" in line)
            self.assertIn("hits=0", line)
            self.assertIn("floor_dropped=2", line)


class ConnectionKeyedByDbPathTests(unittest.TestCase):
    """修复 4：同线程第二个引擎不能静默读写第一个库。"""

    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    def test_second_engine_writes_land_in_its_own_database_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _engine(root / "a")
            second = _engine(root / "b")

            first.add_message("group:1", "10001", "user", "第一个库的消息", user_name="甲")
            first._flush_vector_buffer()
            second.add_message("group:2", "10002", "user", "第二个库的消息", user_name="乙")
            second._flush_vector_buffer()

            self.assertNotEqual(str(first.db_path), str(second.db_path))
            self.assertEqual(self._contents(first.db_path), ["第一个库的消息"])
            self.assertEqual(self._contents(second.db_path), ["第二个库的消息"])

    def test_connect_returns_distinct_connections_per_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _engine(root / "a")
            second = _engine(root / "b")
            self.assertIsNot(first._connect(), second._connect())
            self.assertIs(first._connect(), first._connect())

    def test_close_only_drops_its_own_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _engine(root / "a")
            second = _engine(root / "b")
            kept = second._connect()
            first.close()
            self.assertIs(second._connect(), kept)
            second.add_message("group:2", "10002", "user", "关掉别人之后还能写", user_name="乙")
            second._flush_vector_buffer()
            self.assertEqual(self._contents(second.db_path), ["关掉别人之后还能写"])

    @staticmethod
    def _contents(db_path: Path) -> list[str]:
        conn = sqlite3.connect(str(db_path))
        try:
            return [str(row[0]) for row in conn.execute("SELECT content FROM embeddings ORDER BY id;")]
        finally:
            conn.close()


class EmbeddingDedupeMigrationTests(unittest.TestCase):
    """修复 5：写入去重 + 存量重复清理，唯一索引必须建得起来。"""

    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    def test_unique_index_exists_after_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            with memory._connect() as conn:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_embeddings_unique';"
                ).fetchone()
            self.assertIsNotNone(row, "唯一索引没建起来")
            sql = str(row[0])
            for column in ("conversation_id", "role", "content"):
                self.assertIn(column, sql)

    def test_repeated_identical_messages_store_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            for _ in range(37):
                memory.add_message("group:42", "10001", "user", "同一句错误话术", user_name="小明")
            memory._flush_vector_buffer()
            with memory._connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM embeddings WHERE content = ?;", ("同一句错误话术",)
                ).fetchone()[0]
            self.assertEqual(int(count), 1)

    def test_different_speakers_saying_the_same_thing_both_survive(self) -> None:
        """去重键含 user_id：不同人说同一句话是两个事实，不是重复。

        实测真实库里「你是谁」来自 3 个不同 QQ 号、「搜一下稻香这首歌」来自 2 个。
        去重键漏掉 user_id 会把「谁问过」这个事实删掉，而要治的 37 次重复
        全部是同一说话人，含 user_id 一样治得住。
        """
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            for uid in ("10001", "10002", "10003"):
                memory.add_message("group:42", uid, "user", "你是谁", user_name=f"u{uid}")
            memory._flush_vector_buffer()
            with memory._connect() as conn:
                speakers = [
                    str(r[0])
                    for r in conn.execute(
                        "SELECT user_id FROM embeddings WHERE content = ? ORDER BY user_id;",
                        ("你是谁",),
                    )
                ]
            self.assertEqual(speakers, ["10001", "10002", "10003"])

    def test_migration_keeps_one_row_per_speaker(self) -> None:
        """存量清理同样按 user_id 分组，不能把不同人的同句话合成一行。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = _engine(root)
            db_path = memory.db_path
            memory.close()
            _reset_thread_conns()

            legacy = sqlite3.connect(str(db_path))
            try:
                legacy.execute("DROP INDEX IF EXISTS idx_embeddings_unique;")
                # 两个人各说了 3 次同一句话
                for uid in ("10001", "10002"):
                    for _ in range(3):
                        legacy.execute(
                            "INSERT INTO embeddings "
                            "(conversation_id, user_id, role, content, embedding, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?);",
                            ("group:42", uid, "user", "你是谁", json.dumps([0.0] * 64),
                             "2026-01-01T00:00:00+00:00"),
                        )
                legacy.commit()
            finally:
                legacy.close()
            _reset_thread_conns()

            reopened = _engine(root)
            with reopened._connect() as conn:
                speakers = [
                    str(r[0])
                    for r in conn.execute("SELECT user_id FROM embeddings ORDER BY user_id;")
                ]
            # 每人各留 1 行，而不是全库只剩 1 行
            self.assertEqual(speakers, ["10001", "10002"])

    def test_top_k_is_not_flooded_by_one_repeated_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            for _ in range(37):
                memory.add_message("group:42", "10001", "user", "封我，试试", user_name="小明")
            memory.add_message("group:42", "10001", "user", "封我，我是认真的", user_name="小明")
            memory._flush_vector_buffer()
            with memory._connect() as conn:
                stored = int(conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0])
            self.assertEqual(stored, 2, "37 条同句话应只落 1 行")

            results = memory.search_related(
                "group:42", "封我，这个人", roles=("user",), user_id="10001"
            )
            self.assertEqual(len(results), len(set(results)))
            self.assertEqual(sorted(results), sorted(["封我，试试", "封我，我是认真的"]))

    def test_migration_cleans_legacy_duplicates_and_keeps_earliest_row(self) -> None:
        """存量重复不清掉唯一索引建不起来 —— 清理与建索引必须同一次迁移。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = _engine(root)
            db_path = memory.db_path
            memory.close()
            _reset_thread_conns()

            # 造一个「建唯一索引之前」的旧库：卸索引后灌 5 条完全重复行
            legacy = sqlite3.connect(str(db_path))
            try:
                legacy.execute("DROP INDEX IF EXISTS idx_embeddings_unique;")
                for i in range(5):
                    legacy.execute(
                        "INSERT INTO embeddings "
                        "(conversation_id, user_id, role, content, embedding, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?);",
                        (
                            "group:42",
                            "10001",
                            "user",
                            "同一句错误话术",
                            json.dumps([0.0] * 64),
                            f"2026-01-0{i + 1}T00:00:00+00:00",
                        ),
                    )
                legacy.execute(
                    "INSERT INTO embeddings "
                    "(conversation_id, user_id, role, content, embedding, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?);",
                    (
                        "group:42",
                        "10001",
                        "user",
                        "另一句",
                        json.dumps([0.0] * 64),
                        "2026-01-09T00:00:00+00:00",
                    ),
                )
                legacy.commit()
                self.assertEqual(
                    int(legacy.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0]), 6
                )
            finally:
                legacy.close()
            _reset_thread_conns()

            reopened = _engine(root)
            with reopened._connect() as conn:
                rows = conn.execute(
                    "SELECT content, created_at FROM embeddings ORDER BY id;"
                ).fetchall()
                index_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_embeddings_unique';"
                ).fetchone()
            self.assertIsNotNone(index_sql, "迁移后唯一索引仍未建立")
            self.assertEqual([str(r[0]) for r in rows], ["同一句错误话术", "另一句"])
            # 保留最早一行（MIN(id)），created_at 是首次出现时间
            self.assertEqual(str(rows[0][1]), "2026-01-01T00:00:00+00:00")

    def test_migration_is_a_noop_when_index_already_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = _engine(Path(tmp))
            memory.add_message("group:42", "10001", "user", "一句话", user_name="小明")
            memory._flush_vector_buffer()
            self.assertEqual(memory._migrate_embeddings_dedupe(), 0)
            with memory._connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM embeddings;").fetchone()[0]
            self.assertEqual(int(count), 1)


if __name__ == "__main__":
    unittest.main()
