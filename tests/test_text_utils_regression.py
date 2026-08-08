"""回归测试：text_utils 纯函数搬移后行为与原来一致 + 薄转发一致性。

D9 收敛把 agent.py / engine.py 里无 self 依赖的纯文本判断/解析函数
搬进 core/text_utils.py，原类上保留薄转发。本测试覆盖：
- 文本判断（_looks_like_* / _is_* 系列）
- 时间/数字解析（parse_time_token_to_seconds / to_safe_int / parse_zh_number 等）
- JSON 工具调用归一化（normalize_tool_call / parse_json_object_from_text / find_json_end）
- agent.py / engine.py 薄转发与 text_utils 行为一致
"""

from __future__ import annotations

import unittest

from core import text_utils
from core.agent import AgentLoop
from core.engine import YukikoEngine


class CueNegationTests(unittest.TestCase):
    def test_should_detect_negated_confirmation(self) -> None:
        self.assertTrue(text_utils.cue_is_negated("我不确认", "确认"))
        self.assertTrue(text_utils.cue_is_negated("别确认", "确认"))
        self.assertTrue(text_utils.cue_is_negated("无法确认", "确认"))
        self.assertTrue(text_utils.cue_is_negated("不要确认", "确认"))

    def test_should_accept_plain_confirmation(self) -> None:
        self.assertFalse(text_utils.cue_is_negated("确认", "确认"))
        self.assertFalse(text_utils.cue_is_negated("我要确认", "确认"))

    def test_should_ignore_unrelated_negation(self) -> None:
        self.assertFalse(text_utils.cue_is_negated("确认取消订单", "确认"))


class DeclaredFlagAndSafeIntTests(unittest.TestCase):
    def test_to_declared_flag(self) -> None:
        self.assertTrue(text_utils.to_declared_flag(True))
        self.assertFalse(text_utils.to_declared_flag(False))
        self.assertTrue(text_utils.to_declared_flag("true"))
        self.assertFalse(text_utils.to_declared_flag("false"))
        self.assertTrue(text_utils.to_declared_flag(1))
        self.assertFalse(text_utils.to_declared_flag(0))
        self.assertTrue(text_utils.to_declared_flag("yes"))

    def test_to_safe_int(self) -> None:
        self.assertEqual(text_utils.to_safe_int("123"), 123)
        self.assertEqual(text_utils.to_safe_int("-7"), -7)
        self.assertEqual(text_utils.to_safe_int("abc"), 0)
        self.assertEqual(text_utils.to_safe_int(""), 0)
        self.assertEqual(text_utils.to_safe_int(True), 0)
        self.assertEqual(text_utils.to_safe_int(None), 0)


class LookupAndSplitHintTests(unittest.TestCase):
    def test_infer_lookup_keyword(self) -> None:
        self.assertEqual(text_utils.infer_lookup_keyword("/lookup 量子力学"), "量子力学")
        self.assertEqual(text_utils.infer_lookup_keyword("keyword=引力波"), "引力波")
        self.assertEqual(text_utils.infer_lookup_keyword(""), "")
        self.assertLessEqual(len(text_utils.infer_lookup_keyword("x" * 200)), 80)

    def test_infer_split_video_mode(self) -> None:
        self.assertEqual(text_utils.infer_split_video_mode("mode=audio"), "audio")
        self.assertEqual(text_utils.infer_split_video_mode("mode=cover"), "cover")
        self.assertEqual(text_utils.infer_split_video_mode("mode=frames"), "frames")
        self.assertEqual(text_utils.infer_split_video_mode("mode=clip"), "clip")
        self.assertEqual(text_utils.infer_split_video_mode("5s - 20s 这段"), "clip")
        self.assertEqual(text_utils.infer_split_video_mode("随便"), "")

    def test_infer_frame_count_hint(self) -> None:
        self.assertEqual(text_utils.infer_frame_count_hint("frame_count=5"), 5)
        self.assertEqual(text_utils.infer_frame_count_hint("3 张截图"), 3)
        self.assertEqual(text_utils.infer_frame_count_hint("999 帧"), 12)
        self.assertEqual(text_utils.infer_frame_count_hint("没有"), 0)


class TimeTokenTests(unittest.TestCase):
    def test_parse_time_token_to_seconds(self) -> None:
        self.assertEqual(text_utils.parse_time_token_to_seconds("1:30"), 90.0)
        self.assertEqual(text_utils.parse_time_token_to_seconds("1:02:03"), 3723.0)
        self.assertEqual(text_utils.parse_time_token_to_seconds("45秒"), 45.0)
        self.assertEqual(text_utils.parse_time_token_to_seconds("2.5s"), 2.5)
        self.assertIsNone(text_utils.parse_time_token_to_seconds("abc"))


class ContinuationTests(unittest.TestCase):
    def test_is_context_continuation_phrase(self) -> None:
        self.assertTrue(text_utils.is_context_continuation_phrase("/next"))
        self.assertTrue(text_utils.is_context_continuation_phrase("继续找"))
        self.assertFalse(text_utils.is_context_continuation_phrase("今天天气怎么样"))

    def test_strip_continuation_prefix(self) -> None:
        self.assertEqual(text_utils.strip_continuation_prefix("/next 那是什么"), "那是什么")
        self.assertEqual(text_utils.strip_continuation_prefix("/continue 然后呢"), "然后呢")
        self.assertEqual(text_utils.strip_continuation_prefix("普通文本"), "普通文本")


class UrlAndTextJudgementTests(unittest.TestCase):
    def test_looks_like_reference_to_previous_link(self) -> None:
        self.assertTrue(text_utils.looks_like_reference_to_previous_link("/source"))
        self.assertTrue(text_utils.looks_like_reference_to_previous_link("source=previous"))
        self.assertFalse(text_utils.looks_like_reference_to_previous_link("随便说说"))

    def test_looks_like_image_question(self) -> None:
        self.assertTrue(text_utils.looks_like_image_question("/analyze"))
        self.assertTrue(text_utils.looks_like_image_question("mode=analyze"))
        self.assertFalse(text_utils.looks_like_image_question("这张图好看吗"))

    def test_looks_like_english_refusal_text(self) -> None:
        self.assertTrue(text_utils.looks_like_english_refusal_text("I can't help with that request."))
        self.assertFalse(text_utils.looks_like_english_refusal_text("我做不到这件事"))

    def test_sanitize_profile_summary(self) -> None:
        self.assertEqual(text_utils.sanitize_profile_summary("消息数 123。你好"), "你好")
        self.assertEqual(text_utils.sanitize_profile_summary("QQ号 12345 活跃"), "")

    def test_strip_urls_and_hosts(self) -> None:
        out = text_utils.strip_urls_and_hosts("看 https://a.com/x 和 example.com 啊 @bot [CQ:image]")
        self.assertNotIn("http", out)
        self.assertNotIn("example", out)
        self.assertNotIn("[CQ", out)
        self.assertNotIn("@bot", out)


class JsonAndToolCallTests(unittest.TestCase):
    def test_parse_json_object_from_text(self) -> None:
        self.assertEqual(
            text_utils.parse_json_object_from_text('{"tool": "web_search", "args": {}}'),
            {"tool": "web_search", "args": {}},
        )
        self.assertEqual(
            text_utils.parse_json_object_from_text('```json\n{"tool": "think"}\n```'),
            {"tool": "think"},
        )
        self.assertIsNone(text_utils.parse_json_object_from_text("不是 json"))

    def test_find_json_end(self) -> None:
        self.assertEqual(text_utils.find_json_end('{"a": {"b": 1}} 尾巴'), 14)
        self.assertIsNone(text_utils.find_json_end('{"a": 1'))

    def test_normalize_tool_call(self) -> None:
        self.assertEqual(
            text_utils.normalize_tool_call({"tool": "a", "args": {}}),
            {"tool": "a", "args": {}},
        )
        self.assertEqual(
            text_utils.normalize_tool_call({"name": "a", "arguments": {"x": 1}}),
            {"tool": "a", "args": {"x": 1}},
        )
        self.assertEqual(
            text_utils.normalize_tool_call({"function": "a", "parameters": {}}),
            {"tool": "a", "args": {}},
        )
        self.assertEqual(
            text_utils.normalize_tool_call({"action": "a", "input": {}}),
            {"tool": "a", "args": {}},
        )
        self.assertIsNone(text_utils.normalize_tool_call({"foo": 1}))


class EngineTextJudgementTests(unittest.TestCase):
    def test_has_control_token_respects_boundary(self) -> None:
        self.assertTrue(text_utils.has_control_token("发我 /music 晴天", "/music"))
        self.assertFalse(text_utils.has_control_token("music 好听", "/music"))
        self.assertFalse(text_utils.has_control_token("", "/music"))

    def test_normalize_short_ping_phrase(self) -> None:
        self.assertEqual(text_utils.normalize_short_ping_phrase("YuKiKo！"), "yukiko")

    def test_looks_like_explicit_request(self) -> None:
        self.assertTrue(text_utils.looks_like_explicit_request("这个是什么？"))
        self.assertTrue(text_utils.looks_like_explicit_request("/status"))
        self.assertFalse(text_utils.looks_like_explicit_request("你好"))

    def test_looks_like_media_request(self) -> None:
        self.assertTrue(text_utils.looks_like_media_request("看看 https://a.com/x"))
        self.assertTrue(text_utils.looks_like_media_request("BV1234567"))
        self.assertFalse(text_utils.looks_like_media_request("你好"))

    def test_looks_like_low_info_group_chitchat(self) -> None:
        self.assertTrue(text_utils.looks_like_low_info_group_chitchat("哦"))
        self.assertTrue(text_utils.looks_like_low_info_group_chitchat("。。"))
        self.assertFalse(text_utils.looks_like_low_info_group_chitchat("今天天气不错呀"))

    def test_looks_like_download_task_intent(self) -> None:
        self.assertTrue(text_utils.looks_like_download_task_intent("给我一个 xxx.zip"))
        self.assertFalse(text_utils.looks_like_download_task_intent("你好"))

    def test_looks_like_music_request(self) -> None:
        self.assertTrue(text_utils.looks_like_music_request("点播 a.mp3"))
        self.assertTrue(text_utils.looks_like_music_request("/music 晴天"))
        self.assertFalse(text_utils.looks_like_music_request("你好"))

    def test_extract_music_keyword(self) -> None:
        self.assertIn("晴天", text_utils.extract_music_keyword("/music 晴天"))
        self.assertIn("起风了", text_utils.extract_music_keyword("@bot /song 起风了"))
        self.assertEqual(text_utils.extract_music_keyword(""), "")

    def test_extract_github_repo_from_text(self) -> None:
        self.assertEqual(
            text_utils.extract_github_repo_from_text("看 https://github.com/owner/repo.git"),
            "owner/repo",
        )
        self.assertEqual(text_utils.extract_github_repo_from_text("没有"), "")

    def test_normalize_reply_echo_text(self) -> None:
        self.assertEqual(text_utils.normalize_reply_echo_text("你好，世界！"), "你好世界")

    def test_extract_local_path_candidates(self) -> None:
        rows = text_utils.extract_local_path_candidates("去 /tmp/a.jpg 看看")
        self.assertIn("/tmp/a.jpg", rows)
        self.assertNotIn("https://a.com/x", text_utils.extract_local_path_candidates("https://a.com/x"))


class NumericAndFragmentTests(unittest.TestCase):
    def test_clamp_unit_float(self) -> None:
        self.assertEqual(text_utils.clamp_unit_float(1.5), 1.0)
        self.assertEqual(text_utils.clamp_unit_float(-1), 0.0)
        self.assertEqual(text_utils.clamp_unit_float("x", default=0.3), 0.3)

    def test_mask_numeric_id(self) -> None:
        self.assertEqual(text_utils.mask_numeric_id("1234567"), "****567")
        self.assertEqual(text_utils.mask_numeric_id("12"), "**")
        self.assertEqual(text_utils.mask_numeric_id(""), "")

    def test_strip_known_kaomoji_tokens(self) -> None:
        self.assertEqual(text_utils.strip_known_kaomoji_tokens("QAQ 你好"), "你好")
        self.assertEqual(text_utils.strip_known_kaomoji_tokens(""), "")

    def test_enforce_identity_claim(self) -> None:
        self.assertEqual(text_utils.enforce_identity_claim("我是 OpenAI 的 AI 助手"), "我是 YuKiKo。")
        self.assertEqual(text_utils.enforce_identity_claim("今天天气好"), "今天天气好")

    def test_parse_zh_number(self) -> None:
        self.assertEqual(text_utils.parse_zh_number("十一"), 11)
        self.assertEqual(text_utils.parse_zh_number("二十三"), 23)
        self.assertEqual(text_utils.parse_zh_number("十"), 10)
        self.assertEqual(text_utils.parse_zh_number("五"), 5)
        self.assertIsNone(text_utils.parse_zh_number("zz"))

    def test_extract_choice_index(self) -> None:
        self.assertEqual(text_utils.extract_choice_index("2"), 2)
        self.assertEqual(text_utils.extract_choice_index("第3个"), 3)
        self.assertEqual(text_utils.extract_choice_index("第二个"), 2)
        self.assertIsNone(text_utils.extract_choice_index("abc"))

    def test_contains_choice_numbered_list(self) -> None:
        self.assertFalse(text_utils.contains_choice_numbered_list("甲和乙"))

    def test_fragment_judgements(self) -> None:
        self.assertTrue(text_utils.is_fragment_continuation("？"))
        self.assertFalse(text_utils.is_fragment_continuation("这个句子很长很长很长很长很长很长"))
        self.assertTrue(text_utils.is_fragment_timeout_nudge("??"))

    def test_user_typed_text_for_trigger(self) -> None:
        self.assertEqual(text_utils.user_typed_text_for_trigger("MULTIMODAL_EVENT\n看看这个"), "看看这个")
        self.assertEqual(text_utils.user_typed_text_for_trigger("普通消息"), "普通消息")


class AgentForwarderConsistencyTests(unittest.TestCase):
    """搬移后，agent.py 上的薄转发 staticmethod 与 text_utils 行为一致。"""

    def setUp(self) -> None:
        self.loop = AgentLoop.__new__(AgentLoop)

    def test_agent_forwarders_match_text_utils(self) -> None:
        self.assertTrue(self.loop._cue_is_negated("我不确认", "确认"))
        self.assertEqual(self.loop._to_safe_int("42"), 42)
        self.assertTrue(self.loop._to_declared_flag("true"))
        self.assertEqual(self.loop._infer_lookup_keyword("/lookup 量子力学"), "量子力学")
        self.assertEqual(self.loop._infer_split_video_mode("mode=audio"), "audio")
        self.assertEqual(self.loop._parse_time_token_to_seconds("1:30"), 90.0)
        self.assertEqual(self.loop._infer_frame_count_hint("frame_count=3"), 3)
        self.assertTrue(self.loop._is_context_continuation_phrase("/next"))
        self.assertEqual(self.loop._strip_continuation_prefix("/next 然后呢"), "然后呢")
        self.assertTrue(self.loop._looks_like_reference_to_previous_link("/source"))
        self.assertTrue(self.loop._looks_like_image_question("/analyze"))
        self.assertTrue(self.loop._looks_like_english_refusal_text("I can't help."))
        self.assertEqual(self.loop._parse_json_object_from_text('{"tool": "a"}'), {"tool": "a"})
        self.assertEqual(self.loop._find_json_end('{"a": 1}'), 7)
        self.assertEqual(
            self.loop._normalize_tool_call({"name": "a", "arguments": {}}),
            {"tool": "a", "args": {}},
        )
        self.assertNotIn("https://", self.loop._strip_urls_and_hosts("看 https://a.com"))


class EngineForwarderConsistencyTests(unittest.TestCase):
    """搬移后，engine.py 上的薄转发 staticmethod 与 text_utils 行为一致。"""

    def setUp(self) -> None:
        self.engine = YukikoEngine.__new__(YukikoEngine)

    def test_engine_forwarders_match_text_utils(self) -> None:
        self.assertTrue(self.engine._has_control_token("发我 /music 晴天", "/music"))
        self.assertEqual(self.engine._normalize_short_ping_phrase("YuKiKo！"), "yukiko")
        self.assertTrue(self.engine._looks_like_explicit_request("这个是什么？"))
        self.assertTrue(self.engine._looks_like_media_request("BV1234567"))
        self.assertTrue(self.engine._looks_like_low_info_group_chitchat("哦"))
        self.assertTrue(self.engine._looks_like_download_task_intent("xxx.zip"))
        self.assertTrue(self.engine._looks_like_music_request("a.mp3"))
        self.assertIn("晴天", self.engine._extract_music_keyword("/music 晴天"))
        self.assertEqual(
            self.engine._extract_github_repo_from_text("https://github.com/o/r.git"), "o/r"
        )
        self.assertEqual(self.engine._normalize_reply_echo_text("你好，世界！"), "你好世界")
        self.assertIn("/tmp/a.jpg", self.engine._extract_local_path_candidates("去 /tmp/a.jpg"))
        self.assertEqual(self.engine._clamp_unit_float(2.0), 1.0)
        self.assertEqual(self.engine._mask_numeric_id("1234567"), "****567")
        self.assertEqual(self.engine._strip_known_kaomoji_tokens("QAQ 你好"), "你好")
        self.assertEqual(self.engine._enforce_identity_claim("我是 OpenAI 的 AI 助手"), "我是 YuKiKo。")
        self.assertEqual(self.engine._extract_choice_index("第3个"), 3)
        self.assertEqual(self.engine._parse_zh_number("二十三"), 23)
        self.assertFalse(self.engine._contains_choice_numbered_list("甲和乙"))
        self.assertTrue(self.engine._is_fragment_continuation("？"))
        self.assertTrue(self.engine._is_fragment_timeout_nudge("??"))
        self.assertEqual(self.engine._user_typed_text_for_trigger("MULTIMODAL_EVENT\n看看"), "看看")


if __name__ == "__main__":
    unittest.main()
