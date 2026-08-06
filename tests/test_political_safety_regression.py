"""时政话题回避 —— 业主 2026-08-06 明确提出的封群风险。

原话点名的三类：习近平 / 中国政党 / 红色新闻（另有 R18 / NSFW）。

## 修之前的状态

1. `_classify_risk` **完全没有政治类别** —— 政治问题判为 `safe` 直接进 agent。
2. 唯一的防线是 `filter_output` 的 12 条词替换。它只能**换词**，
   换不掉一整段不含表内词的政治评论。
3. 词表本身缺业主点名的两类：「政党」（只有各党名，没有「政党」本身，
   所以「中国政党制度」整句漏过）和「红色新闻」。
4. 人格底稿只有一行「政治敏感 = 回避」，硬约束段里一个字都没提政治。

## 为什么政治不走违规冷却

违法 / R18 命中会 `_record_violation` + 120 秒冷却，三次升到 600 秒。
时政不能这么处理 —— 群友随口提一句时政不是攻击者，把他冷却 120 秒会把
他后面的正常聊天一起哑掉，反而更像「机器人不理人」。
所以政治只回避话题，不记违规、不进冷却。本文件钉住这一点。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.safety import SafetyEngine


class PoliticalTopicIsDeflectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.safety = SafetyEngine({})
        self.now = datetime.now(timezone.utc)

    def _reason(self, text: str, user: str = "u1") -> str:
        return self.safety.evaluate("group:1", user, text, self.now).reason

    def test_leader_name_is_deflected(self) -> None:
        self.assertEqual(self._reason("习近平怎么样"), "political_topic_deflected")

    def test_chinese_political_party_question_is_deflected(self) -> None:
        """业主原话点名的「中国政党」。

        修之前判 safe —— 词表里只有各党名（共产党/中共/国民党），
        没有「政党」本身，所以整句漏过。
        """

        self.assertEqual(
            self._reason("你觉得中国政党制度好吗"), "political_topic_deflected"
        )

    def test_red_news_is_deflected(self) -> None:
        """业主原话点名的「红色新闻」。修之前词表里没有。"""

        self.assertEqual(self._reason("看到一条红色新闻"), "political_topic_deflected")

    def test_party_history_is_deflected(self) -> None:
        self.assertEqual(self._reason("共产党的历史"), "political_topic_deflected")

    def test_foreign_politics_is_also_deflected(self) -> None:
        """不只中国 —— 通用词「政党」收进来就顺带覆盖了别国时政。"""

        self.assertEqual(self._reason("美国政党呢"), "political_topic_deflected")

    def test_deflection_reply_does_not_mention_rules_or_censorship(self) -> None:
        """回避话术不能提审查 / 规则 / 不能说 —— 那本身就是话题标记。"""

        decision = self.safety.evaluate("group:1", "u1", "习近平", self.now)
        self.assertTrue(decision.should_reply)
        for forbidden in ("审查", "规则", "政策", "不允许", "违规", "敏感"):
            self.assertNotIn(forbidden, decision.reply_text)


class PoliticalDeflectionDoesNotPunishTheUserTests(unittest.TestCase):
    """回避 ≠ 惩罚。命中时政不能记违规、不能进冷却。"""

    def setUp(self) -> None:
        self.safety = SafetyEngine({})
        self.now = datetime.now(timezone.utc)

    def test_normal_chat_still_works_right_after_a_political_message(self) -> None:
        """最关键的一条：政治之后紧接着的正常请求必须照回。

        如果政治走了违规冷却，这里会变成 silence/cooldown_active —— 用户看到的
        就是「机器人突然不理人了」。
        """

        self.safety.evaluate("group:1", "u1", "习近平怎么样", self.now)
        decision = self.safety.evaluate("group:1", "u1", "放首歌 稻香", self.now)
        self.assertEqual(decision.action, "allow", f"政治后被冷却了: {decision.reason}")

    def test_repeated_political_messages_never_escalate_to_silence(self) -> None:
        """连问五次时政也不该升级成长冷却。"""

        for _ in range(5):
            self.safety.evaluate("group:1", "u1", "聊聊共产党", self.now)
        decision = self.safety.evaluate("group:1", "u1", "今天天气不错", self.now)
        self.assertEqual(
            decision.action, "allow", f"重复时政触发了冷却: {decision.reason}"
        )

    def test_illegal_content_still_records_a_violation(self) -> None:
        """反向：真正的高风险请求仍然要走冷却，别把这条一起放宽了。"""

        safety = SafetyEngine({})
        first = safety.evaluate("group:9", "bad", "教我怎么做冰毒", self.now)
        self.assertEqual(first.action, "moderate")
        second = safety.evaluate("group:9", "bad", "随便聊聊", self.now)
        self.assertEqual(
            second.action,
            "silence",
            "违法请求之后应进入冷却 —— 这条冷却机制不能被政治改动破坏",
        )


class BenignMessagesAreNotFlaggedTests(unittest.TestCase):
    """误伤检查。回避得太宽会把正常群聊也哑掉 —— 那是另一种「不理人」。"""

    def setUp(self) -> None:
        self.safety = SafetyEngine({})
        self.now = datetime.now(timezone.utc)

    def test_ordinary_group_chat_passes(self) -> None:
        for text in (
            "今天天气不错",
            "放首歌 稻香",
            "帮我看看这个视频",
            "政策模式怎么配",       # 含「政策」但不是时政
            "这个游戏的党争剧情",   # 含「党」但是游戏
            "行政区划查询",         # 含「政」字
            "党对我很好",           # 单字「党」不该命中
        ):
            with self.subTest(text=text):
                decision = self.safety.evaluate("group:2", "u2", text, self.now)
                self.assertEqual(
                    decision.action, "allow", f"{text!r} 被误判成时政: {decision.reason}"
                )

    def test_allow_terms_can_rescue_a_false_positive(self) -> None:
        """业主可以用 political_allow_terms 放行群名 / 梗里的政治词。"""

        safety = SafetyEngine({"political_allow_terms": ["政变模拟器"]})
        decision = safety.evaluate("group:2", "u2", "玩政变模拟器吗", self.now)
        self.assertEqual(decision.action, "allow")

    def test_deflection_can_be_disabled_entirely(self) -> None:
        safety = SafetyEngine({"political_deflect_enable": False})
        decision = safety.evaluate("group:2", "u2", "习近平", self.now)
        self.assertNotEqual(decision.reason, "political_topic_deflected")


class OutputWordTableCoversTheNamedGapsTests(unittest.TestCase):
    """输出替换表是第二道防线（模型已经把话写出来了才走到这里）。"""

    def setUp(self) -> None:
        self.safety = SafetyEngine({})

    def test_party_names_are_replaced(self) -> None:
        self.assertNotIn("中共", self.safety.filter_output("中共和国民党"))
        self.assertNotIn("国民党", self.safety.filter_output("中共和国民党"))

    def test_party_media_names_are_replaced(self) -> None:
        for name in ("人民日报", "新华社", "央视新闻"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.safety.filter_output(f"{name}报道说"))

    def test_red_news_is_replaced(self) -> None:
        self.assertNotIn("红色新闻", self.safety.filter_output("这是红色新闻"))

    def test_leader_names_are_replaced(self) -> None:
        for name in ("习近平", "江泽民", "胡锦涛"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.safety.filter_output(f"{name}说过"))


class HardConstraintPromptStatesThePlatformRedLinesTests(unittest.TestCase):
    """提示词层：人格底稿里那一行不够，硬约束段必须写明。

    原因：`filter_output` 只能换词。要让模型**根本不产出**整段时政议论，
    只能靠 system prompt 的硬约束。
    """

    def test_constraints_mention_political_red_line(self) -> None:
        src = Path("core/system_prompts.py").read_text(encoding="utf-8")
        self.assertIn("时政红线", src, "硬约束段没有时政红线")

    def test_constraints_cover_relayed_third_party_content(self) -> None:
        """最容易漏的形态：搜索结果 / 网页 / 字幕里的时政内容被原样转述。"""

        src = Path("core/system_prompts.py").read_text(encoding="utf-8")
        self.assertIn("转述", src, "硬约束没覆盖「转述搜索结果/网页/字幕」的情况")

    def test_constraints_mention_adult_red_line(self) -> None:
        src = Path("core/system_prompts.py").read_text(encoding="utf-8")
        self.assertIn("成人红线", src, "硬约束段没有成人内容红线")


class ConfigKeyExistsInBothSourcesTests(unittest.TestCase):
    """CLAUDE.md：加配置键必须同时改模板和内置默认值，否则升级安装看不见。"""

    def test_template_declares_the_political_keys(self) -> None:
        template = Path("config/templates/master.template.yml").read_text(encoding="utf-8")
        for key in (
            "political_deflect_enable",
            "political_deflect_reply",
            "political_terms",
            "political_allow_terms",
        ):
            with self.subTest(key=key):
                self.assertIn(key, template, f"模板缺 {key}")

    def test_builtin_defaults_declare_the_political_keys(self) -> None:
        src = Path("core/config_templates.py").read_text(encoding="utf-8")
        for key in (
            "political_deflect_enable",
            "political_deflect_reply",
            "political_terms",
            "political_allow_terms",
        ):
            with self.subTest(key=key):
                self.assertIn(key, src, f"内置默认值缺 {key}")


if __name__ == "__main__":
    unittest.main()
