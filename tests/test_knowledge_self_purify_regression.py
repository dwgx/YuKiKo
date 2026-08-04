"""知识库自净化 + 关键词清理回归。

覆盖两件事：
1. A11 —— `_looks_like_tool_echo` 只保留结构判据（OneBot 段 / JSON 形状 / 纯链接投递），
   不再因为句子里含 URL 就整条跳过；`learn_knowledge` 的安全拒绝机制从硬编码脏词表
   换成模型声明的 `safety_review`，拒绝这条契约本身不变。
2. E1 —— 已写好但从未被调用的衰减公式接进读路径，写入侧补上质量门
   （内容全等去重、置信度参与的矛盾裁决、陈旧只标记不淘汰），
   并且每一次真删都留版本快照 + 审计记录。
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from core.agent_tools_knowledge import _handle_learn_knowledge
from core.audit import STREAM_KNOWLEDGE, AuditTrail
from core.knowledge import KnowledgeBase
from core.knowledge_updater import KnowledgeUpdater


def _make_updater() -> KnowledgeUpdater:
    return KnowledgeUpdater(None, {"control": {}, "knowledge_update": {}}, logger=None)


class ToolEchoStructuralOnlyTests(unittest.TestCase):
    """工具回显判定必须是结构事实，不能是「含链接就别学」这种语义判断。"""

    def test_structural_machine_output_is_still_blocked(self) -> None:
        updater = _make_updater()
        for text in (
            '{"tool":"search_music","args":{"q":"夜曲"}}',
            '{"tool_result":{"ok":true}}',
            "[CQ:image,file=abc.jpg]",
            "[cq:at,qq=10001] 你好呀",
        ):
            with self.subTest(text=text):
                self.assertTrue(updater._looks_like_tool_echo(text))

    def test_normal_sentence_containing_a_url_is_no_longer_blocked(self) -> None:
        """契约反转：原实现把 http:// / https:// 当 cue，这里断言修复后的行为。

        原先任何含链接的正常陈述句都会让整条消息的知识抽取被整体跳过
        （实测「我的博客是 https://dwgx.dev 平时写点技术笔记」被拦）。
        「链接堆砌不该抽取」这条语义规则归 _extract_candidates_llm 的 system prompt。
        """
        updater = _make_updater()
        for text in (
            "我的博客是 https://dwgx.dev 平时写点技术笔记",
            "推荐这个视频 http://b23.tv/abc123",
            "维基百科链接 https://zh.wikipedia.org/wiki/Python 讲得很清楚",
        ):
            with self.subTest(text=text):
                self.assertFalse(updater._looks_like_tool_echo(text))

    def test_url_only_payload_is_blocked_as_a_structural_fact(self) -> None:
        """剥掉 URL 后没有正文 = 纯链接投递，这是结构事实而不是语义猜测。"""
        updater = _make_updater()
        self.assertTrue(updater._looks_like_tool_echo("https://a.com"))
        self.assertTrue(updater._looks_like_tool_echo("http://a.com https://b.com"))
        self.assertFalse(updater._looks_like_tool_echo("https://a.com 这个页面讲得很清楚"))

    def test_plain_text_with_braces_or_quotes_is_not_mistaken_for_json(self) -> None:
        updater = _make_updater()
        self.assertFalse(updater._looks_like_tool_echo("python 里 dict 的写法是 {'a': 1}"))
        self.assertFalse(updater._looks_like_tool_echo("记住：我的生日是 3 月 5 日"))


class _KnowledgeBaseCase(unittest.TestCase):
    """真 KnowledgeBase + tmpdir + 真 AuditTrail，不用 stub。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.trail = AuditTrail(self.root / "audit", enable=True)
        self.kb = KnowledgeBase(db_path=str(self.root / "knowledge.db"), audit=self.trail)
        self.addCleanup(self.kb.close)

    def _raw_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.root / "knowledge.db"))
        self.addCleanup(conn.close)
        return conn

    def _audit_events(self) -> list[str]:
        return [str(r.get("event", "")) for r in self.trail.read(STREAM_KNOWLEDGE, limit=200)]


class SchemaMigrationTests(_KnowledgeBaseCase):
    def test_access_tracking_columns_exist_and_version_is_stamped(self) -> None:
        conn = self._raw_conn()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(knowledge)").fetchall()}
        self.assertIn("access_count", columns)
        self.assertIn("last_used_at", columns)
        self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 1)

    def test_migration_upgrades_a_pre_existing_v0_database(self) -> None:
        """老库靠 CREATE TABLE IF NOT EXISTS 是不会被加列的，必须走 PRAGMA user_version 阶梯。"""
        legacy = self.root / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            """
            CREATE TABLE knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL, title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
                expires_at REAL NOT NULL DEFAULT 0, extra TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        conn.execute(
            "INSERT INTO knowledge (category,title,content,created_at) VALUES ('fact','老条目','旧内容',1.0)"
        )
        conn.commit()
        conn.close()

        kb = KnowledgeBase(db_path=str(legacy))
        try:
            rows = kb.get_by_category("fact", limit=5)
            self.assertEqual([e.title for e in rows], ["老条目"])
            self.assertEqual(rows[0].access_count, 0)
        finally:
            kb.close()
        conn = sqlite3.connect(str(legacy))
        self.addCleanup(conn.close)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(knowledge)").fetchall()}
        self.assertIn("access_count", columns)
        self.assertIn("last_used_at", columns)


class DecayWiredIntoRecallTests(_KnowledgeBaseCase):
    def test_recall_increments_access_count_and_stamps_last_used(self) -> None:
        """没有 touch 就永远没有陈旧判据，强化项也永远等于 1。"""
        self.kb.add("learned", "喜欢的茶", "乌龙茶", source="chat")
        self.kb.search("喜欢的茶", category="learned", limit=5)
        self.kb.search("喜欢的茶", category="learned", limit=5)
        rows = self.kb.search("喜欢的茶", category="learned", limit=5)
        self.assertEqual(rows[0].access_count, 2)
        self.assertGreater(rows[0].last_used_at, 0.0)

    def test_stale_high_confidence_entry_ranks_below_fresh_lower_confidence_entry(self) -> None:
        """原 _entry_rank 第二项是裸 confidence，衰减公式对检索毫无影响。

        这里断言接线后的行为：置信度 0.9 但 400 天没更新的条目，
        排在置信度 0.75 的新条目之后。
        """
        now = time.time()
        self.kb.add("fact", "旧条目", "很久没人问的旧知识",
                    extra={"confidence": 0.9, "updated_at": now - 400 * 86400})
        self.kb.add("fact", "新条目", "刚刚写入的知识",
                    extra={"confidence": 0.75, "updated_at": now})
        rows = self.kb.get_by_category("fact", limit=5)
        self.assertEqual([e.title for e in rows], ["新条目", "旧条目"])

    def test_reinforcement_lifts_a_frequently_recalled_entry(self) -> None:
        now = time.time()
        hot = self.kb.add("fact", "常被问", "热门知识", extra={"confidence": 0.5, "updated_at": now})
        cold = self.kb.add("fact", "没人问", "冷门知识", extra={"confidence": 0.5, "updated_at": now})
        self.kb.touch([hot], now=now)
        self.kb.touch([hot], now=now)
        entries = {e.title: e for e in self.kb.get_by_category("fact", limit=5)}
        hot_score = KnowledgeBase._effective_score(entries["常被问"], now)
        cold_score = KnowledgeBase._effective_score(entries["没人问"], now)
        self.assertGreater(hot_score, cold_score)
        self.assertNotEqual(hot, cold)

    def test_bare_add_stamps_a_baseline_confidence_so_decay_has_an_input(self) -> None:
        """裸 add() 不写 confidence 时，衰减项 confidence*decay*reinforcement 恒等于 0。"""
        self.kb.add("learned", "裸写入", "没带置信度的条目")
        rows = self.kb.get_by_category("learned", limit=5)
        self.assertGreater(float(rows[0].extra.get("confidence", 0.0)), 0.0)
        self.assertGreater(KnowledgeBase._effective_score(rows[0], time.time()), 0.0)

    def test_upsert_does_not_downgrade_an_existing_higher_confidence(self) -> None:
        self.kb.add("fact", "高把握", "内容", extra={"confidence": 0.95})
        self.kb.add("fact", "高把握", "内容")  # 不带 extra 的第二次写入
        rows = self.kb.get_by_category("fact", limit=5)
        self.assertAlmostEqual(float(rows[0].extra.get("confidence", 0.0)), 0.95)


class WriteQualityGateTests(_KnowledgeBaseCase):
    def test_identical_content_under_a_new_title_becomes_an_alias_not_a_second_row(self) -> None:
        first = self.kb.upsert_conflict_checked("learned", "喜欢的饮料", "用户偏好乌龙茶", confidence=0.8)
        second = self.kb.upsert_conflict_checked("learned", "最喜欢的饮料", "用户偏好乌龙茶", confidence=0.8)
        self.assertEqual(first["action"], "inserted")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.kb.count("learned"), 1)
        rows = self.kb.get_by_category("learned", limit=5)
        self.assertIn("最喜欢的饮料", rows[0].extra.get("aliases", []))
        self.assertIn("knowledge_duplicate_merged", self._audit_events())

    def test_lower_confidence_contradiction_is_held_instead_of_overwriting(self) -> None:
        """原实现是盲目后写胜出：判据只有 old_content != content，confidence 从不参与决策。"""
        self.kb.upsert_conflict_checked("learned", "用户生日", "3月5日", confidence=0.95)
        held = self.kb.upsert_conflict_checked("learned", "用户生日", "8月9日", confidence=0.63)
        self.assertEqual(held["action"], "disputed")
        rows = self.kb.get_by_category("learned", limit=5)
        self.assertEqual(rows[0].content, "3月5日")
        disputed = rows[0].extra.get("disputed", [])
        self.assertEqual([d["content"] for d in disputed], ["8月9日"])
        self.assertIn("knowledge_contradiction_held", self._audit_events())

    def test_higher_confidence_still_supersedes_and_keeps_a_version_snapshot(self) -> None:
        self.kb.upsert_conflict_checked("learned", "用户生日", "3月5日", confidence=0.95)
        res = self.kb.upsert_conflict_checked("learned", "用户生日", "8月9日", confidence=0.99)
        self.assertEqual(res["action"], "updated")
        rows = self.kb.get_by_category("learned", limit=5)
        self.assertEqual(rows[0].content, "8月9日")
        conn = self._raw_conn()
        snaps = conn.execute(
            "SELECT content FROM knowledge_versions WHERE knowledge_id=? ORDER BY version_no", (res["id"],)
        ).fetchall()
        self.assertEqual([s[0] for s in snaps], ["3月5日"])
        self.assertIn("knowledge_superseded", self._audit_events())

    def test_explicit_correction_overrides_the_confidence_gate(self) -> None:
        """显式更正是用户主动纠错，不该被置信度门挡住。"""
        self.kb.upsert_conflict_checked("learned", "用户生日", "3月5日", confidence=0.95)
        res = self.kb.upsert_conflict_checked(
            "learned", "用户生日", "1月1日", confidence=0.3, mark_correction=True
        )
        self.assertEqual(res["action"], "updated")
        self.assertEqual(self.kb.get_by_category("learned", limit=5)[0].content, "1月1日")

    def test_dedup_never_aliases_onto_an_expired_row(self) -> None:
        """过期行读不出来，把新条目 alias 到它身上等于让这条知识凭空消失。"""
        self.kb.add("meme", "旧梗", "同一段内容", ttl=1)
        time.sleep(1.1)
        res = self.kb.upsert_conflict_checked("meme", "新梗", "同一段内容", confidence=0.8)
        self.assertEqual(res["action"], "inserted")
        titles = [e.title for e in self.kb.get_by_category("meme", limit=5)]
        self.assertEqual(titles, ["新梗"])


class RetentionAndAuditTests(_KnowledgeBaseCase):
    def test_stale_entries_are_reported_not_deleted(self) -> None:
        """陈旧判定只产出候选。永久知识库的前提是删除必须是显式动作。"""
        self.kb.add("fact", "从没被问过", "写了很久没人召回")
        conn = self._raw_conn()
        conn.execute("UPDATE knowledge SET created_at=? WHERE title=?", (time.time() - 200 * 86400, "从没被问过"))
        conn.commit()
        before = self.kb.count()
        stale = self.kb.list_stale(threshold_days=90, limit=10)
        self.assertEqual([e.title for e in stale], ["从没被问过"])
        self.assertEqual(self.kb.count(), before)

    def test_recalled_entry_is_not_reported_as_stale(self) -> None:
        self.kb.add("fact", "被问过", "有人召回过")
        conn = self._raw_conn()
        conn.execute("UPDATE knowledge SET created_at=? WHERE title=?", (time.time() - 200 * 86400, "被问过"))
        conn.commit()
        self.kb.search("被问过", category="fact", limit=5)
        self.assertEqual(self.kb.list_stale(threshold_days=90, limit=10), [])

    def test_expired_purge_keeps_a_snapshot_cleans_fts_and_writes_audit(self) -> None:
        """原 cleanup_expired 是裸 DELETE：无快照、不删 FTS 行、无审计 —— 静默丢数据的源头。"""
        self.kb.add("trend", "过期热搜", "旧热搜快照", ttl=1)
        time.sleep(1.1)
        deleted = self.kb.cleanup_expired()
        self.assertEqual(deleted, 1)
        conn = self._raw_conn()
        snaps = conn.execute(
            "SELECT title, content FROM knowledge_versions WHERE title=?", ("过期热搜",)
        ).fetchall()
        self.assertEqual(snaps, [("过期热搜", "旧热搜快照")])
        orphans = conn.execute(
            "SELECT COUNT(*) FROM knowledge_fts f LEFT JOIN knowledge k ON k.id=f.rowid WHERE k.id IS NULL"
        ).fetchone()[0]
        self.assertEqual(orphans, 0)
        self.assertIn("knowledge_expired_purged", self._audit_events())

    def test_cleanup_is_a_noop_when_nothing_expired(self) -> None:
        self.kb.add("fact", "永久条目", "永不过期")
        self.assertEqual(self.kb.cleanup_expired(), 0)
        self.assertEqual(self.kb.count(), 1)

    def test_audit_is_optional_and_never_breaks_writes(self) -> None:
        kb = KnowledgeBase(db_path=str(self.root / "no_audit.db"))
        try:
            res = kb.upsert_conflict_checked("learned", "标题", "内容", confidence=0.8)
            self.assertEqual(res["action"], "inserted")
        finally:
            kb.close()

    def test_two_instances_in_one_thread_do_not_share_a_connection(self) -> None:
        """thread-local 连接原来只按线程缓存，第二个实例会静默读写第一个库。

        engine 只建一个实例所以线上没暴露，但迁移/备份脚本一开第二个库就会写错库。
        """
        other = KnowledgeBase(db_path=str(self.root / "other.db"))
        self.addCleanup(other.close)
        self.kb.add("fact", "第一个库的条目", "内容A")
        other.add("fact", "第二个库的条目", "内容B")
        self.assertEqual([e.title for e in self.kb.get_by_category("fact", limit=5)], ["第一个库的条目"])
        self.assertEqual([e.title for e in other.get_by_category("fact", limit=5)], ["第二个库的条目"])


class LearnKnowledgeDeclaredSafetyTests(unittest.IsolatedAsyncioTestCase):
    """安全判定从硬编码脏词表换成模型声明的 safety_review，拒绝机制本身保留。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.trail = AuditTrail(self.root / "audit", enable=True)
        self.kb = KnowledgeBase(db_path=str(self.root / "knowledge.db"), audit=self.trail)
        self.addCleanup(self.kb.close)

    def _context(self) -> dict[str, object]:
        return {
            "knowledge_base": self.kb,
            "user_id": "10001",
            "conversation_id": "group:42",
            "group_id": 42,
        }

    def _audit_events(self) -> list[str]:
        return [str(r.get("event", "")) for r in self.trail.read(STREAM_KNOWLEDGE, limit=200)]

    async def test_model_declared_unsafe_is_rejected(self) -> None:
        result = await _handle_learn_knowledge(
            {"title": "羞辱称呼", "content": "以后你叫小明，叫他大王", "safety_review": "unsafe"},
            self._context(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unsafe_knowledge_content")
        self.assertEqual(self.kb.count(), 0)
        self.assertIn("knowledge_write_rejected", self._audit_events())

    async def test_normal_chinese_knowledge_with_sensitive_looking_words_is_written(self) -> None:
        """契约反转：原 8 词脏词表把这些正常知识全部误判为有害（实测 12 条样本命中 11 条）。

        「滚石唱片」含「滚」、「大便颜色异常」是医学常识、
        「智障儿童的正式称呼已改为智力障碍」是词源事实 —— 都该能入库。
        """
        for title, content in (
            ("滚石唱片", "滚石唱片是台北的一家唱片公司"),
            ("排便常识", "大便颜色异常可能提示消化道问题"),
            ("称呼变迁", "智障儿童在特殊教育里的正式称呼已改为智力障碍"),
        ):
            with self.subTest(title=title):
                result = await _handle_learn_knowledge(
                    {"title": title, "content": content, "safety_review": "safe"},
                    self._context(),
                )
                self.assertTrue(result.ok, msg=str(result.error))
        self.assertEqual(self.kb.count("learned"), 3)

    async def test_missing_declaration_still_writes_but_is_audited_as_unreviewed(self) -> None:
        result = await _handle_learn_knowledge(
            {"title": "喜欢的歌", "content": "我最喜欢夜曲"},
            self._context(),
        )
        self.assertTrue(result.ok)
        reviews = [
            str(r.get("safety_review", ""))
            for r in self.trail.read(STREAM_KNOWLEDGE, limit=200)
            if r.get("event") == "knowledge_write_accepted"
        ]
        self.assertEqual(reviews, ["unreviewed"])

    async def test_learn_knowledge_now_writes_confidence_and_goes_through_the_gate(self) -> None:
        """原来走裸 kb.add：不写 confidence、排序永远垫底、完全绕过质量门。"""
        result = await _handle_learn_knowledge(
            {"title": "喜欢的歌", "content": "我最喜欢夜曲", "confidence": 0.9, "safety_review": "safe"},
            self._context(),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data.get("action"), "inserted")
        rows = self.kb.get_by_category("learned", limit=5)
        self.assertAlmostEqual(float(rows[0].extra.get("confidence", 0.0)), 0.9)
        self.assertEqual(rows[0].extra.get("update_mode"), "agent")

    async def test_learn_knowledge_holds_a_lower_confidence_contradiction(self) -> None:
        await _handle_learn_knowledge(
            {"title": "用户生日", "content": "3月5日", "confidence": 0.95, "safety_review": "safe"},
            self._context(),
        )
        result = await _handle_learn_knowledge(
            {"title": "用户生日", "content": "8月9日", "confidence": 0.6, "safety_review": "safe"},
            self._context(),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data.get("action"), "disputed")
        rows = [e for e in self.kb.get_by_category("learned", limit=5) if e.title == "用户生日"]
        self.assertEqual(rows[0].content, "3月5日")

    async def test_learn_knowledge_correction_flag_lets_the_model_override(self) -> None:
        await _handle_learn_knowledge(
            {"title": "用户生日", "content": "3月5日", "confidence": 0.95, "safety_review": "safe"},
            self._context(),
        )
        result = await _handle_learn_knowledge(
            {
                "title": "用户生日",
                "content": "8月9日",
                "confidence": 0.6,
                "is_correction": True,
                "safety_review": "safe",
            },
            self._context(),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data.get("action"), "updated")
        rows = [e for e in self.kb.get_by_category("learned", limit=5) if e.title == "用户生日"]
        self.assertEqual(rows[0].content, "8月9日")

    async def test_scope_tags_survive_the_new_write_path(self) -> None:
        result = await _handle_learn_knowledge(
            {"title": "喜欢的歌", "content": "我最喜欢夜曲", "safety_review": "safe"},
            self._context(),
        )
        self.assertTrue(result.ok)
        tags = result.data.get("tags", [])
        self.assertIn("user:10001", tags)
        self.assertIn("conversation:group:42", tags)
        self.assertIn("group:42", tags)
        rows = self.kb.search("喜欢的歌", category="learned", limit=5)
        self.assertIn("user:10001", list(rows[0].tags))


if __name__ == "__main__":
    unittest.main()
