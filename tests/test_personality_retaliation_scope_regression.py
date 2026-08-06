"""回归：「被骂可以反击」的作用域必须收窄到「对方直接冲机器人本人来」。

背景：上一轮实测到的精华消息事故形状 —— 用户要求查精华消息，机器人把
「@某人 你妈也死了」完整复述进群还加调侃。用户确实主动要求了，但完整复述
等于把辱骂又发了一遍；而人格底稿里那句无限定的「被骂/攻击：可以反击」
不区分「骂的是你本人」和「你在转述别人骂别人」，等于给复述+调侃背书。

这里断言的是**人格底稿文本**里的两条边界，走真实读取路径读，不读死字符串：
    ensure_default_files() 落盘 → from_file() 读回 → persona_text
persona_text 就是 core/engine.py:183 传给 AgentLoop 的那个值，
也是 system_instruction() 注入【人格底稿】的那个值，两条路都覆盖到。

同时反向保护人格底色：业主要的是有个性的女生，不是一味道歉的客服，
所以「不一味道歉」「傲娇」这些词被删掉也要报红。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.personality import PersonalityEngine


def _load_persona_text_through_real_read_path() -> str:
    """按真实落盘+读回路径取人格底稿，避免直接断言 DEFAULT_PERSONA_TEXT 字符串。"""
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp) / "config"
        PersonalityEngine.ensure_default_files(config_dir)
        engine = PersonalityEngine.from_file(config_dir / "personality.yml")
        return engine.persona_text


class PersonaRetaliationScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.persona = _load_persona_text_through_real_read_path()
        self.assertTrue(self.persona.strip(), "人格底稿读取路径返回空文本，测试前提不成立")

    def _permission_lines(self) -> list[str]:
        """给出「可以反击」许可的行（排除写着「不适用」的排除条款行）。"""
        return [
            line
            for line in self.persona.splitlines()
            if "反击" in line and "不适用" not in line
        ]

    def test_should_allow_retaliation_only_when_the_insult_targets_the_bot_directly(self) -> None:
        permission_lines = self._permission_lines()
        self.assertTrue(
            permission_lines,
            "人格底稿里应当仍有一条允许反击的行，人格底色不能被删干净",
        )
        for line in permission_lines:
            self.assertTrue(
                ("直接" in line) or ("当面" in line),
                f"允许反击的行必须限定为对方直接冲机器人本人来，当前为无限定许可：{line!r}",
            )

    def test_should_not_grant_unscoped_retaliation_permission(self) -> None:
        self.assertNotIn(
            "被骂/攻击：可以反击",
            self.persona,
            "无限定的「被骂/攻击：可以反击」等于给转述辱骂时的复述+调侃背书，必须收窄",
        )

    def test_should_exclude_relayed_or_quoted_insults_from_retaliation(self) -> None:
        relay_lines = [
            line
            for line in self.persona.splitlines()
            if ("转述" in line or "引用" in line) and ("辱骂" in line or "骂" in line)
        ]
        self.assertTrue(
            relay_lines,
            "人格底稿缺少「转述/引用到的辱骂内容不适用反击」这条边界",
        )
        self.assertTrue(
            any("不适用" in line for line in relay_lines),
            f"转述场景必须明确不适用反击，当前相关行：{relay_lines!r}",
        )

    def test_should_forbid_reproducing_relayed_insults_verbatim(self) -> None:
        relay_lines = [
            line
            for line in self.persona.splitlines()
            if ("转述" in line or "引用" in line) and ("辱骂" in line or "骂" in line)
        ]
        self.assertTrue(relay_lines, "人格底稿缺少转述辱骂的处理边界")
        joined = "\n".join(relay_lines)
        self.assertIn(
            "完整",
            joined,
            f"必须明确禁止把辱骂原话完整复述一遍，当前相关行：{relay_lines!r}",
        )
        self.assertIn(
            "概括",
            joined,
            f"禁止复述之外要给出可用的替代做法（概括大意），当前相关行：{relay_lines!r}",
        )

    def test_should_keep_the_persona_edge_instead_of_defaulting_to_apology(self) -> None:
        for keyword in ("不一味道歉", "傲娇"):
            self.assertIn(
                keyword,
                self.persona,
                f"收窄作用域不等于改成永远道歉的客服，人格底色词「{keyword}」不能丢",
            )

    def test_should_inject_the_scoped_boundary_into_the_composed_system_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            PersonalityEngine.ensure_default_files(config_dir)
            engine = PersonalityEngine.from_file(config_dir / "personality.yml")
            prompt = engine.system_instruction(bot_name="YuKiKo")

        self.assertIn("【人格底稿】", prompt, "系统提示词应当仍然注入人格底稿段")
        self.assertNotIn(
            "被骂/攻击：可以反击",
            prompt,
            "无限定的反击许可不能出现在最终系统提示词里",
        )
        self.assertTrue(
            any(
                ("转述" in line or "引用" in line) and "不适用" in line
                for line in prompt.splitlines()
            ),
            "转述辱骂不适用反击这条边界必须真的进到最终系统提示词里",
        )


if __name__ == "__main__":
    unittest.main()
