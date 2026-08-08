from __future__ import annotations

from pathlib import Path
import unittest
from typing import Any

import yaml

from core.config_templates import _built_in_config_defaults

_TEMPLATE_FILE = (
    Path(__file__).resolve().parents[1] / "config" / "templates" / "master.template.yml"
)


def _template_config() -> dict[str, Any]:
    parsed = yaml.safe_load(_TEMPLATE_FILE.read_text(encoding="utf-8")) or {}
    config = parsed.get("config", {})
    assert isinstance(config, dict)
    return config


def _flatten(node: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """把嵌套 config 树拍平成 'section.key' -> 叶子值，便于比对键集。"""
    out: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


class ConfigDriftRegressionTests(unittest.TestCase):
    """A2：built-in 默认值与 master.template.yml 的 config 段必须键值一致。

    历史漂移（已修）：
    - bot.private_chat_mode：模板是布尔 False，built-in 是字符串 "off"，
      消费端 app.py 做 str(...).lower()，False 变成 "false"，私聊永远被拒。
    - routing.ai_gate_min_confidence / non_directed_min_confidence /
      followup_fast_path_enable 三处 built-in 与模板不同。
    - 61 个键只在模板里（affinity / image_gen / video_analysis /
      routing.mode / search.video_resolver.* 等），built-in 缺失。
    """

    def test_builtin_and_template_have_identical_leaf_key_sets(self) -> None:
        builtin = _flatten(_built_in_config_defaults())
        template = _flatten(_template_config())

        only_builtin = sorted(set(builtin) - set(template))
        only_template = sorted(set(template) - set(builtin))

        self.assertEqual(
            only_builtin,
            [],
            "built-in 默认值里的键在模板里不存在：%s" % only_builtin,
        )
        self.assertEqual(
            only_template,
            [],
            "模板 config 里的键在 built-in 默认值里缺失：%s" % only_template,
        )

    def test_builtin_and_template_agree_on_all_leaf_values(self) -> None:
        builtin = _flatten(_built_in_config_defaults())
        template = _flatten(_template_config())

        mismatches = sorted(
            key for key in set(builtin) & set(template) if builtin[key] != template[key]
        )
        self.assertEqual(mismatches, [], "键值不一致：%s" % mismatches)

    def test_private_chat_mode_is_string_off_in_both_sources(self) -> None:
        builtin_value = _built_in_config_defaults()["bot"]["private_chat_mode"]
        template_value = _template_config()["bot"]["private_chat_mode"]

        self.assertIsInstance(builtin_value, str)
        self.assertIsInstance(template_value, str)
        self.assertEqual(builtin_value, "off")
        self.assertEqual(template_value, "off")

        # 消费端 app.py:1066 的归一化路径：str(...).lower() 必须得到 "off"，
        # 而不是布尔 False 落成 "false"。
        self.assertEqual(str(template_value).lower(), "off")

    def test_routing_thresholds_match_template(self) -> None:
        builtin = _built_in_config_defaults()["routing"]
        template = _template_config()["routing"]

        self.assertEqual(builtin["ai_gate_min_confidence"], template["ai_gate_min_confidence"])
        self.assertEqual(
            builtin["non_directed_min_confidence"],
            template["non_directed_min_confidence"],
        )
        self.assertEqual(
            builtin["followup_fast_path_enable"],
            template["followup_fast_path_enable"],
        )
        self.assertEqual(builtin["mode"], template["mode"])

    def test_backfilled_sections_present_in_builtin_defaults(self) -> None:
        builtin = _built_in_config_defaults()

        # 曾经只存在于模板的顶层段，补齐后 built-in 也必须能独立给出默认值。
        for section in ("affinity", "image_gen", "video_analysis", "knowledge_update"):
            self.assertIn(section, builtin, "built-in 默认值缺顶层段 %s" % section)
        self.assertIn("mode", builtin["routing"])
        self.assertIn("cache_dir", builtin["music"])
        self.assertIn("github_token", builtin["search"]["tool_interface"])
        self.assertIn("cookies_from_browser", builtin["search"]["video_resolver"])
