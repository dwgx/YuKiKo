from __future__ import annotations

import unittest

from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_web import _register_ai_method_tools

_ALWAYS_ON = {"fetch_webpage", "douyin_search", "get_qq_avatar"}
_GITHUB_TOOLS = {"github_search", "github_readme"}


class DisabledToolVisibilityRegressionTests(unittest.TestCase):
    """被配置关掉的工具不能注册。

    `search.tool_interface.github_enable` 两处真相源都是 false，但工具照样注册，
    于是模型看得见、会去调，然后拿到「GitHub 方法已关闭。」——
    冒烟实测 `github 上搜一下 nonebot` 白烧一整轮推理才失败。
    """

    def _register(self, config: dict | None) -> set[str]:
        registry = AgentToolRegistry()
        _register_ai_method_tools(registry, config)
        return set(registry._schemas)

    def test_github_tools_absent_when_disabled(self) -> None:
        for label, config in (
            ("no config at all", None),
            ("empty config", {}),
            ("explicitly false", {"search": {"tool_interface": {"github_enable": False}}}),
        ):
            with self.subTest(label):
                names = self._register(config)
                self.assertEqual(names & _GITHUB_TOOLS, set(), label)

    def test_github_tools_present_when_enabled(self) -> None:
        names = self._register({"search": {"tool_interface": {"github_enable": True}}})
        self.assertEqual(names & _GITHUB_TOOLS, _GITHUB_TOOLS)

    def test_other_tools_register_regardless_of_github_flag(self) -> None:
        """回归防线：把 github 注册收进 if 时曾把后续工具一起缩进去，
        导致 github 关闭时 douyin_search 等全部消失。"""

        off = self._register({"search": {"tool_interface": {"github_enable": False}}})
        on = self._register({"search": {"tool_interface": {"github_enable": True}}})

        self.assertTrue(_ALWAYS_ON <= off, sorted(_ALWAYS_ON - off))
        self.assertTrue(_ALWAYS_ON <= on, sorted(_ALWAYS_ON - on))
        # 两者只应差 github 那两个。
        self.assertEqual(on - off, _GITHUB_TOOLS)

    def test_navigator_section_may_list_unregistered_tool_safely(self) -> None:
        """navigator 的 web_research 分区仍列着 github_search / github_readme。

        这是安全的，因为 scoped_tools 与注册表求交集 —— 未注册的名字被剔除，
        不会漏给模型。本测试固化这个不变量：分区列表不是可见性的真相源。
        """

        from core.prompt_navigator import (
            PromptNavigator,
            default_prompt_navigator_payload,
            load_prompt_navigator_config,
        )

        navigator = PromptNavigator(
            load_prompt_navigator_config(default_prompt_navigator_payload())
        )
        registered = sorted(
            self._register({"search": {"tool_interface": {"github_enable": False}}})
        )
        state = navigator.initial_state(_StubCtx(), registered)
        ok, _ = navigator.switch_section(state, "web_research")
        self.assertTrue(ok)

        scoped = set(navigator.scoped_tools(state))
        self.assertEqual(scoped & _GITHUB_TOOLS, set())
        self.assertIn("fetch_webpage", scoped)


class _StubCtx:
    message_text = ""
    mentioned = False
    is_private = False
    raw_segments: list = []
    reply_media_segments: list = []
    at_other_user_ids: list = []
    media_summary = ""
    reply_media_summary = ""
    permission_level = "user"


if __name__ == "__main__":
    unittest.main()
