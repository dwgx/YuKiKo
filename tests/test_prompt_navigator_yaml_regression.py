"""A3 回归：prompt_navigator 默认数据已从 Python 内联迁到 core/prompt_navigator_data.yml。

钉住三件事：
1. default_prompt_navigator_payload() 从 YAML 数据文件读出，结构与迁移前一致
   （sections / enable / strict_tool_routing 等关键键存在且非空）。
2. 四份真相源逐字节一致：core/prompt_navigator_data.yml（新）、
   config/templates/master.template.yml、config/prompts.yml、函数返回值。
3. 数据文件缺失时回退最小内联默认，初始化不崩。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import core.prompt_navigator as pn
import yaml
from core.prompt_navigator import (
    PromptNavigator,
    default_prompt_navigator_payload,
    load_prompt_navigator_config,
)

_REPO = Path(__file__).resolve().parent.parent
_DATA_FILE = _REPO / "core" / "prompt_navigator_data.yml"
_TEMPLATE_FILE = _REPO / "config" / "templates" / "master.template.yml"
_PROMPTS_FILE = _REPO / "config" / "prompts.yml"

_REQUIRED_SECTION_FIELDS = ("name", "when_to_use", "tools", "instructions", "fallback_sections", "failure_policy")


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class PromptNavigatorYamlRegressionTests(unittest.TestCase):
    def test_payload_reads_from_yaml_data_file(self) -> None:
        """函数返回值必须等于 core/prompt_navigator_data.yml 的内容。"""
        payload = default_prompt_navigator_payload()
        data_payload = _read_yaml(_DATA_FILE)
        self.assertEqual(payload, data_payload)

    def test_key_structure_keys_present_and_nonempty(self) -> None:
        """迁移前的关键键必须存在且非空。"""
        payload = default_prompt_navigator_payload()

        sections = payload.get("sections")
        self.assertIsInstance(sections, dict)
        self.assertTrue(sections)
        self.assertGreaterEqual(len(sections), 20)

        self.assertIs(payload.get("enable"), True)
        self.assertIs(payload.get("strict_tool_routing"), True)
        self.assertIn(payload.get("default_section"), sections)
        self.assertIsInstance(payload.get("max_switches"), int)
        self.assertIsInstance(payload.get("root_prompt"), str)
        self.assertTrue(payload["root_prompt"].strip())

    def test_each_section_has_required_fields(self) -> None:
        """每个分区的字段集与 load_prompt_navigator_config 消费的一致。"""
        payload = default_prompt_navigator_payload()
        for section_id, section in payload["sections"].items():
            with self.subTest(section=section_id):
                self.assertIsInstance(section, dict)
                for field in _REQUIRED_SECTION_FIELDS:
                    self.assertIn(field, section, f"分区 {section_id} 缺字段 {field}")

    def test_all_four_sources_stay_identical(self) -> None:
        """函数返回值 / YAML 数据文件 / 模板 / prompts.yml 四份真相源必须一致。"""
        python_payload = default_prompt_navigator_payload()
        data_payload = _read_yaml(_DATA_FILE)
        template_payload = _read_yaml(_TEMPLATE_FILE)["prompts"]["prompt_navigator"]
        prompts_payload = _read_yaml(_PROMPTS_FILE)["prompt_navigator"]
        self.assertEqual(python_payload, data_payload)
        self.assertEqual(python_payload, template_payload)
        self.assertEqual(python_payload, prompts_payload)

    def test_navigator_still_builds_from_yaml_payload(self) -> None:
        """从 YAML 读出的载荷仍能构建 PromptNavigator 并完成分区切换。"""
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        self.assertTrue(nav.enabled)
        self.assertEqual(nav.config.default_section, "general_chat")
        self.assertTrue(nav.config.strict_tool_routing)

    def test_load_config_uses_yaml_defaults_when_raw_empty(self) -> None:
        cfg = load_prompt_navigator_config(None)
        self.assertTrue(cfg.enable)
        self.assertTrue(cfg.strict_tool_routing)
        self.assertEqual(cfg.default_section, "general_chat")
        self.assertGreaterEqual(len(cfg.sections), 20)

    def test_missing_data_file_falls_back_to_minimal(self) -> None:
        """数据文件缺失时回退最小内联默认，不抛异常。"""
        original_path = pn._NAV_DATA_FILE
        try:
            pn._NAV_DATA_FILE = _REPO / "core" / "no_such_navigator_data.yml"
            pn._nav_payload_cache = None
            payload = default_prompt_navigator_payload()
            self.assertEqual(list(payload["sections"].keys()), ["general_chat"])
            self.assertIs(payload["enable"], True)
            self.assertEqual(payload["default_section"], "general_chat")
            # 最小默认也要能被 navigator 消费
            nav = PromptNavigator.from_payload(payload)
            self.assertTrue(nav.enabled)
        finally:
            pn._NAV_DATA_FILE = original_path
            pn._nav_payload_cache = None

    def test_invalid_data_file_falls_back_to_minimal(self) -> None:
        """数据文件内容非法（非 dict / 无 sections）时回退最小内联默认。"""
        import tempfile

        original_path = pn._NAV_DATA_FILE
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
                fh.write("enable: true\nsections: not-a-dict\n")
                bad_path = Path(fh.name)
            pn._NAV_DATA_FILE = bad_path
            pn._nav_payload_cache = None
            payload = default_prompt_navigator_payload()
            self.assertEqual(list(payload["sections"].keys()), ["general_chat"])
        finally:
            pn._NAV_DATA_FILE = original_path
            pn._nav_payload_cache = None
            bad_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
