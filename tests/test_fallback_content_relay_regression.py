"""内容搬运类工具的 display 不能被兜底原样发进群。

事故形状（实测）：用户问「看看群精华有什么」→ 模型调 get_essence_msg_list 成功
→ 随后撞 max_steps / LLM 超时（provider 是 skiapi，实测小 prompt 6.7~10.7s 且高频 503，
这个前提很现实）→ `_build_fallback_result` 取最后一个有 display 的 step，
截 280 字**直接当回复外发**，零 LLM、零人格底稿参与。
于是「@某人 你妈也死了」被完整复述进群并加了调侃。
用户确实主动要求了，但完整复述等于又发一遍。

这条路碰不到任何 prompt 层禁令 —— 它不经过模型。唯一的拦法是按**工具身份**
（结构事实，不是扫文本找词）把这类工具排除在原样外发之外，让它落到
messages.tool_failure_* 的类别兜底句。

同类还有名单/资料工具：display 是成员名册和个人资料，直通等于把一批
QQ 号和昵称倒进群里。
"""

from __future__ import annotations

import unittest

from core.agent import AgentLoop

# 这些工具的 display 里是别人说过的原话（行号指 core/agent_tools_napcat.py）
_CONTENT_RELAY_TOOLS = (
    "get_essence_msg_list",  # :2482  "共 N 条精华:\n[昵称] 内容"
    "get_group_history",  # :1896
    "get_chat_history",  # :1921
    "get_group_msg_history",  # :2431
    "get_forward_msg",  # :4008
)

# 这些的 display 是名册 / 个人资料
_ROSTER_TOOLS = (
    "get_group_member_list",  # :1153
    "get_friend_list",  # :1778
    "get_group_list",  # :1787
    "get_user_info",  # :1175
    "get_login_info",  # :1846
)

# 实测事故原文的形状（内容已替换成无害占位，保留结构）
_ESSENCE_DISPLAY = "共 3 条精华:\n[张三] 你这个人真是\n[李四] 我也觉得\n[王五] 别吵了"


class ContentRelayToolsAreNotRelayedVerbatimTests(unittest.TestCase):
    def test_content_relay_tools_are_skipped(self) -> None:
        for tool in _CONTENT_RELAY_TOOLS:
            with self.subTest(tool=tool):
                self.assertTrue(
                    AgentLoop._skip_raw_tool_display_in_fallback(tool, _ESSENCE_DISPLAY),
                    f"{tool} 的 display 是别人的原话，不能原样外发",
                )

    def test_roster_tools_are_skipped(self) -> None:
        roster_display = "群 123 共 373 人: 张三(1001), 李四(1002), 王五(1003)"
        for tool in _ROSTER_TOOLS:
            with self.subTest(tool=tool):
                self.assertTrue(
                    AgentLoop._skip_raw_tool_display_in_fallback(tool, roster_display),
                    f"{tool} 的 display 是名册/资料，直通等于把 QQ 号倒进群里",
                )

    def test_the_previously_covered_discovery_tools_still_skipped(self) -> None:
        """别把老白名单改坏了 —— 那批是上一轮为「可用模型: 1 个」被当回复发出而加的。"""

        for tool in (
            "list_image_models",
            "list_faces",
            "list_emojis",
            "browse_sticker_categories",
            "fetch_webpage",
            "scrape_extract",
        ):
            with self.subTest(tool=tool):
                self.assertTrue(
                    AgentLoop._skip_raw_tool_display_in_fallback(tool, "可用模型: 1 个")
                )

    def test_ordinary_tools_are_still_allowed_through(self) -> None:
        """反向保护：不能把所有工具都拦掉，那会丢掉真实失败原因 ——
        core/agent.py 那行注释写的「避免二次 LLM 超时后丢失真实错误」是真顾虑。"""

        for tool in ("parse_video", "analyze_image", "music_search", "web_search"):
            with self.subTest(tool=tool):
                self.assertFalse(
                    AgentLoop._skip_raw_tool_display_in_fallback(
                        tool, "这个视频链接命中了安全限制"
                    ),
                    f"{tool} 的失败原因被误拦，用户会拿不到任何解释",
                )

    def test_skip_list_membership_is_by_tool_identity_not_by_text(self) -> None:
        """判定必须只看工具名这个结构事实。

        如果它开始按文本内容判断，就变成了本仓已明确删除的那类语义否决层
        （core/engine.py 里 _self_check_decision 的墓碑注释）。
        同一个工具名配任意文本，结论都该一致。
        """

        for text in (
            _ESSENCE_DISPLAY,
            "无消息记录",
            "共 0 条精华:",
            "x" * 300,
            "全都是英文 ascii text without any cjk characters at all here",
        ):
            with self.subTest(text[:24]):
                self.assertTrue(
                    AgentLoop._skip_raw_tool_display_in_fallback(
                        "get_essence_msg_list", text
                    )
                )


class SkipListShapeTests(unittest.TestCase):
    def test_skip_list_is_a_frozenset_of_lowercase_names(self) -> None:
        names = AgentLoop._FALLBACK_RAW_DISPLAY_SKIP_TOOLS
        self.assertIsInstance(names, frozenset)
        for name in names:
            self.assertEqual(name, name.lower(), f"{name} 大小写不一致，匹配会漏")

    def test_every_declared_tool_is_in_the_list(self) -> None:
        names = AgentLoop._FALLBACK_RAW_DISPLAY_SKIP_TOOLS
        missing = sorted(
            t for t in (*_CONTENT_RELAY_TOOLS, *_ROSTER_TOOLS) if t not in names
        )
        self.assertEqual(missing, [], f"这些工具不在白名单里: {missing}")


if __name__ == "__main__":
    unittest.main()
