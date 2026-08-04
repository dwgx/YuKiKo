"""画像里的关键词表清除（MIGRATION_TODO A11 的 memory 半边）。

被删的是 `_detect_topic_category`：5 张硬编码词表（tech/game/anime/life/music，共 62 词）
从自由文本猜「这个人是什么人」，经 profiles.json 的 topic_counts →
get_user_profile_summary 的「常聊X」→ prompt → ThinkingEngine._adaptive_style_hint
变成「可以用专业术语」这类行为指令。词表决定行为，删。

被留的是 `_detect_language_style`：它一个语义词都不认，看的全是排版
（emoji / 颜文字 / 叠字标点 / 叹号数 / 拉丁占比 / 字符数 / 中文句读）。
和文件扩展名、MIME 嗅探同类，是结构事实。下面用「同排版异语义」和
「抹掉全部实词」两组对照把这件事锁住 —— 以后谁想把词表塞回这个函数，这两个用例会红。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import core.memory as memory_module
from core.memory import MemoryEngine

_TECH_FLAVOURED = [
    "帮我看下这段 python 代码为什么报 bug",
    "docker compose 起不来，端口冲突",
    "python 的 asyncio 我一直没搞明白",
]


def _reset_thread_conn() -> None:
    conn = getattr(memory_module._db_local, "conn", None)
    if conn is not None:
        conn.close()
        memory_module._db_local.conn = None


class TopicKeywordTableRemovedTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_thread_conn()

    def tearDown(self) -> None:
        _reset_thread_conn()

    def test_topic_category_detector_is_gone(self) -> None:
        self.assertFalse(hasattr(MemoryEngine, "_detect_topic_category"))

    def test_profile_no_longer_stores_topic_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                config={"enable_daily_log": False}, memory_dir=Path(tmp) / "memory"
            )
            for text in _TECH_FLAVOURED:
                memory.add_message("group:42", "10001", "user", text, user_name="小明")
            profile = memory._user_profiles["10001"]
            self.assertNotIn("topic_counts", profile)

    def test_profile_summary_gives_raw_keywords_not_topic_verdicts(self) -> None:
        """替代物必须在场：模型拿到原始词频，自己判断对方懂不懂技术。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                config={"enable_daily_log": False}, memory_dir=Path(tmp) / "memory"
            )
            for text in _TECH_FLAVOURED:
                memory.add_message("group:42", "10001", "user", text, user_name="小明")
            summary = memory.get_user_profile_summary("10001")

            self.assertIn("常聊关键词", summary)
            self.assertIn("python", summary)
            # 词表下的结论标签一个都不许出现。
            for verdict in ("常聊技术", "常聊游戏", "常聊动漫", "常聊日常生活", "常聊音乐"):
                self.assertNotIn(verdict, summary)

    def test_legacy_profile_topic_counts_are_pruned_on_load(self) -> None:
        """旧画像里的词表结论不清掉，会继续从磁盘漏进 prompt。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / "memory"
            (memory_dir / "users").mkdir(parents=True, exist_ok=True)
            legacy = {
                "10001": {
                    "user_id": "10001",
                    "display_name": "老用户",
                    "message_count": 50,
                    "total_chars": 900,
                    "question_count": 2,
                    "keywords": {"python": 9, "docker": 4},
                    "style_counts": {"casual": 40, "formal": 10},
                    "topic_counts": {"tech": 30, "game": 12},
                    "active_hours": {"14": 30},
                }
            }
            profiles_path = memory_dir / "users" / "profiles.json"
            profiles_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

            memory = MemoryEngine(config={"enable_daily_log": False}, memory_dir=memory_dir)
            self.assertNotIn("topic_counts", memory._user_profiles["10001"])
            self.assertNotIn("常聊技术", memory.get_user_profile_summary("10001"))

            # style_counts 是结构事实，不能被牵连删掉。
            self.assertEqual(
                memory._user_profiles["10001"]["style_counts"], {"casual": 40, "formal": 10}
            )

            # 启动清理必须真的落盘：__init__ 原先在 sanitize 之后把 dirty 位擦掉了，
            # 导致清理结果只活在内存里。
            memory._flush_user_profiles()
            on_disk = json.loads(profiles_path.read_text(encoding="utf-8"))
            self.assertNotIn("topic_counts", on_disk["10001"])

    def test_user_portrait_drops_never_written_keys(self) -> None:
        """portrait 原先读 language_style / topic_preferences，全仓从未有代码写过它们。"""
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryEngine(
                config={"enable_daily_log": False}, memory_dir=Path(tmp) / "memory"
            )
            for text in _TECH_FLAVOURED:
                memory.add_message("group:42", "10001", "user", text, user_name="小明")
            portrait = memory.get_user_portrait("10001")
            self.assertIn("小明", portrait)
            self.assertNotIn("风格:", portrait)
            self.assertNotIn("兴趣:", portrait)


class LanguageStyleIsStructuralTests(unittest.TestCase):
    """`_detect_language_style` 保留的依据：它认排版，不认词。"""

    def test_same_wording_different_layout_changes_label(self) -> None:
        detect = MemoryEngine._detect_language_style
        self.assertEqual(detect("这个方案我觉得可以。"), "casual")
        self.assertEqual(detect("这个方案我觉得可以!!!"), "slang")
        self.assertEqual(detect("这个方案我觉得可以~~~ 😂"), "slang")

    def test_different_wording_same_layout_keeps_label(self) -> None:
        detect = MemoryEngine._detect_language_style
        for text in (
            "关于这个功能的实现方案我需要先确认一下整体架构的边界。",
            "关于今天晚饭吃什么这件事我需要先确认一下大家的口味偏好。",
            "关于原神这次抽卡的保底机制我需要先确认一下具体的概率数值。",
        ):
            self.assertEqual(detect(text), "formal", text)

    def test_label_survives_scrubbing_every_content_word(self) -> None:
        """把实词全换成无意义字、排版位不动，标签必须不变。"""
        detect = MemoryEngine._detect_language_style
        for original, scrubbed in (
            (
                "帮我看下这段代码为什么报错，接口一直返回失败",
                "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌",
            ),
            ("哈哈哈哈笑死我了www 太强了！！！", "甲甲甲甲乙丙丁戊www 己庚辛！！！"),
        ):
            self.assertEqual(detect(original), detect(scrubbed), original)

    def test_detector_holds_no_semantic_word_table(self) -> None:
        """直接看字节码常量：除了标签、正则和标点，不该有任何实词。"""
        consts = {
            c
            for c in MemoryEngine._detect_language_style.__func__.__code__.co_consts
            if isinstance(c, str)
        }
        allowed = {"casual", "slang", "formal", r"\s+", "", "!", "！"}
        leftover = {c for c in consts if c not in allowed and not c.startswith("检测")}
        self.assertEqual(leftover, set(), f"出现了非排版字面量：{leftover}")


if __name__ == "__main__":
    unittest.main()
