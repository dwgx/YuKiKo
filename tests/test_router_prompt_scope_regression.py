"""router_system_prompt 在 Navigator 严格模式下不得再选工具族（MIGRATION_TODO A4）。

`core/system_prompts.py` 的 router prompt 原先枚举了整个 action 家族，并写明
「点歌 播歌 action=music_play」「画图请求 action=generate_image」——这是第二套
意图分类器，与 PromptNavigator 的分区目录抢同一个决策权，而且它看不到分区里的
工具说明，判断依据比 Agent 少。

严格模式下 router 只回答「这条要不要接」和置信度；用哪个工具由 Agent 读能力菜单决定。
断言的是**指令语义**而非字数：规则里不能出现具体工具族的选择指引。
"""
from __future__ import annotations

import unittest

import core.prompt_loader as _pl
from core.system_prompts import SystemPromptRelay

# 只在「规则区」里查禁词。末尾免责声明会提到「点歌画图」来告诉模型这些不归它管，
# 那是正确措辞，不能算泄漏。
_RULES_END_MARKER = "重要"

_TOOL_FAMILY_TOKENS = (
    "music_play",
    "music_search",
    "generate_image",
    "send_segment",
    "plugin_call",
    "get_group_member_count",
    "get_group_member_names",
    "moderate",
)


def _rules_region(prompt: str) -> str:
    idx = prompt.find(_RULES_END_MARKER)
    return prompt if idx < 0 else prompt[:idx]


class RouterPromptScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        _pl.reload()
        self.assertTrue(
            SystemPromptRelay._strict_navigator_enabled(),
            "本仓库默认 prompt_navigator.strict_tool_routing=true；若被改则本测试的前提不成立",
        )

    def _prompt(self) -> str:
        return SystemPromptRelay.router_system_prompt(
            allow_actions=["ignore", "reply", "search", "music_play", "generate_image"],
            plugin_schema=[{"name": "demo", "description": "示例插件"}],
            method_schema=[{"name": "m1", "description": "示例方法", "scope": "web"}],
        )

    def test_action_enum_is_reduced_to_attention_only(self) -> None:
        prompt = self._prompt()
        self.assertIn('"action":"ignore|reply"', prompt)

    def test_rules_do_not_select_a_tool_family(self) -> None:
        rules = _rules_region(self._prompt())
        for token in _TOOL_FAMILY_TOKENS:
            self.assertNotIn(token, rules, f"规则区不应再指定工具族: {token}")

    def test_rules_do_not_teach_keyword_to_action_mapping(self) -> None:
        """「点歌→music_play」这类映射本身就是关键词触发，只是写在了 prompt 里。"""
        rules = _rules_region(self._prompt())
        for phrase in ("点歌", "画图", "播歌", "tool_args", "tool_name"):
            self.assertNotIn(phrase, rules, f"规则区不应出现关键词映射: {phrase}")

    def test_prompt_still_asks_for_should_handle_and_confidence(self) -> None:
        """收缩范围不等于丢掉本职：是否回应与置信度仍然必须要求。"""
        prompt = self._prompt()
        self.assertIn("should_handle", prompt)
        self.assertIn("confidence", prompt)

    def test_prompt_tells_model_tool_choice_is_not_its_job(self) -> None:
        prompt = self._prompt()
        self.assertIn("Agent", prompt)
        self.assertIn("不是你的职责", prompt)

    def test_safety_is_deferred_not_decided_here(self) -> None:
        """安全判断按 owner 决策交给后续环节，router 不再自己出 moderate。"""
        prompt = self._prompt()
        self.assertNotIn("moderate", _rules_region(prompt))
        self.assertIn("安全策略", prompt)

    def test_legacy_prompt_survives_when_strict_mode_is_off(self) -> None:
        """关掉严格模式时必须回到旧行为，否则没有 Navigator 的部署会失去工具路由。"""
        original = SystemPromptRelay._strict_navigator_enabled
        try:
            SystemPromptRelay._strict_navigator_enabled = staticmethod(lambda: False)
            prompt = self._prompt()
            self.assertIn("music_play", prompt)
            self.assertIn("tool_args", prompt)
        finally:
            SystemPromptRelay._strict_navigator_enabled = original


if __name__ == "__main__":
    unittest.main()
