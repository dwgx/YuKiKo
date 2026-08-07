"""Phase 1b：GOAP 决策前推理 + Hermes prompt 原则回归测试。

锁三件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.2（2）（3））：
1. GOAP「决策前推理」原则已进入三源（Python payload / master.template.yml / prompts.yml）。
2. Hermes「结果未返回不得臆造」原则已在 tools/rules（防幻觉）。
3. 三源合并后模型实际看到的 rules 含这些原则。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import core.config_templates as ct
import core.prompt_loader as pl

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_YML = ROOT / "config" / "prompts.yml"


class GoapPromptPrincipleTests(unittest.TestCase):
    def test_builtin_payload_contains_goap_principle(self) -> None:
        payload = ct._built_in_prompts_defaults()
        rules = payload["agent"]["rules"]
        self.assertIn("决策前推理", rules)

    def test_master_template_contains_goap_principle(self) -> None:
        template = ct.load_prompts_template()
        rules = template["agent"]["rules"]
        self.assertIn("决策前推理", rules)

    def test_prompts_yml_contains_goap_principle(self) -> None:
        text = PROMPTS_YML.read_text(encoding="utf-8")
        self.assertIn("决策前推理", text)

    def test_merged_prompt_loader_contains_goap_and_anti_confabulation(self) -> None:
        # 合并后的运行时 prompt：GOAP 决策前推理 + Hermes 防臆造都必须在。
        agent_dict = pl.get_dict("agent")
        merged_rules = agent_dict.get("rules", "")
        tools = agent_dict.get("tools", "") + agent_dict.get("tool_usage", "")
        merged_blob = f"{merged_rules} {tools}"
        self.assertIn("决策前推理", merged_rules)
        self.assertTrue(
            ("不要伪造" in merged_blob) or ("没调工具就别装作看过" in merged_blob),
            "Hermes「结果未返回不得臆造」原则丢失",
        )


if __name__ == "__main__":
    unittest.main()
