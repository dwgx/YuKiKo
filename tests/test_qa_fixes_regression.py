"""全工具 QA 修复回归测试。

覆盖 4 个 High 修复（来自 4 路 QA agent 报告）：
1. music _split_artist_song：中文「歌名 歌手」顺序（"晴天 周杰伦"→ artist=周杰伦）。
2. memory_add 重复内容幂等（不再撞唯一索引崩溃）。
3. recall_about_user 知识库检索不跨用户泄漏（search_by_tag 精确匹配）。
4. set_group_ban 目标解析信任模型显式 user_id。
"""
from __future__ import annotations

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


class MusicSplitArtistSongTests(unittest.TestCase):
    def test_chinese_song_first_artist_last(self) -> None:
        from core.music import MusicEngine

        artist, title = MusicEngine._split_artist_song("晴天 周杰伦")
        self.assertEqual(artist, "周杰伦")
        self.assertEqual(title, "晴天")

    def test_de_pattern_artist_de_song(self) -> None:
        from core.music import MusicEngine

        artist, title = MusicEngine._split_artist_song("周杰伦的晴天")
        self.assertEqual(artist, "周杰伦")
        self.assertEqual(title, "晴天")

    def test_single_word_returns_empty_artist(self) -> None:
        from core.music import MusicEngine

        artist, title = MusicEngine._split_artist_song("稻香")
        self.assertEqual(artist, "")
        self.assertEqual(title, "稻香")


class MemoryAddIdempotentTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_thread_conns()

    def tearDown(self) -> None:
        _reset_thread_conns()

    def test_duplicate_add_is_idempotent_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                {"enable_daily_log": False}, Path(tmp) / "memory"
            )
            kwargs = dict(
                conversation_id="group:1",
                user_id="10001",
                role="user",
                content="我住在杭州",
                actor="agent:10001",
            )
            ok1, _, _ = memory.add_memory_record(**kwargs)
            ok2, msg2, payload2 = memory.add_memory_record(**kwargs)
            self.assertTrue(ok1)
            self.assertTrue(ok2)
            self.assertEqual(msg2, "memory_exists")
            self.assertTrue(payload2.get("duplicate"))


class RecallAboutUserNoLeakTests(unittest.TestCase):
    def test_search_by_tag_exact_no_prefix_leak(self) -> None:
        """recall_about_user 改用 search_by_tag 精确匹配（user:10001 不得命中 user:1000102）。"""
        from core.knowledge import KnowledgeBase

        with tempfile.TemporaryDirectory() as tmp:
            kb = KnowledgeBase(db_path=str(Path(tmp) / "kb.db"))
            try:
                kb.add(
                    category="learned",
                    title="乌龙茶",
                    content="10001 喜欢乌龙茶",
                    source="chat",
                    tags=["user:10001"],
                    upsert=False,
                )
                kb.add(
                    category="learned",
                    title="隐私",
                    content="1000102 有抑郁症在吃舍曲林",
                    source="chat",
                    tags=["user:1000102"],
                    upsert=False,
                )
                rows = kb.search_by_tag("user:10001", category="learned", limit=5)
                contents = [str(r.content) for r in rows]
                self.assertIn("10001 喜欢乌龙茶", contents)
                self.assertNotIn("1000102 有抑郁症在吃舍曲林", contents, "跨用户泄漏")
            finally:
                kb.close()


class SetGroupBanTargetTests(unittest.TestCase):
    def test_model_provided_user_id_is_trusted(self) -> None:
        from core.agent_tools_napcat import _resolve_group_ban_target

        uid, err = _resolve_group_ban_target(
            {"user_id": "123456"},
            {
                "user_id": "10001",
                "bot_id": "bot",
                "message_text": "把 123456 禁言",
                "at_other_user_ids": [],
                "reply_to_user_id": "",
            },
        )
        self.assertEqual(uid, 123456, f"err={err}")
        self.assertEqual(err, "")

    def test_missing_target_still_rejected(self) -> None:
        from core.agent_tools_napcat import _resolve_group_ban_target

        uid, err = _resolve_group_ban_target(
            {},
            {
                "user_id": "10001",
                "bot_id": "bot",
                "message_text": "把他禁言",
                "at_other_user_ids": [],
                "reply_to_user_id": "",
            },
        )
        self.assertIsNone(uid)
        self.assertTrue(err)


if __name__ == "__main__":
    unittest.main()
