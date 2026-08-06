"""trigger 段「读取但两处真相源都缺」的键必须补齐。

这类 bug 的症状是：本机手改过 config.yml 就正常，全新安装/升级装拿的是代码兜底值，
两边行为不一致，而且 WebUI 配置页里根本看不到这些键，业主没法调。
本项目已经踩过三次（routing.fragment_join_enable /
video_resolver.metadata_timeout_seconds / agent.navigator_preflight_plain_text）。

实测（2026-08-05）trigger 段有九个键属于这一类：
  active_session_timeout_minutes / ai_listen_interval_seconds / ai_listen_keywords /
  busy_window_seconds / overload_enable / overload_min_messages /
  overload_min_unique_users / overload_notice_cooldown_seconds / overload_pause_seconds

补进去时用的是各自在 core/trigger.py 里的代码兜底原值，所以这次补齐**不改变任何行为** ——
本文件同时把「补进去的值 == 代码兜底值」钉住，避免将来有人顺手改了模板值
却以为自己只是在补文档。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml
from core.config_templates import _built_in_config_defaults

_TRIGGER_PY = Path("core/trigger.py")
_TEMPLATE = Path("config/templates/master.template.yml")

# 这些键的兜底值不是字面量（是 timedelta/表达式），单独列出期望值
_EXPECTED = {
    "active_session_timeout_minutes": 8,
    "ai_listen_interval_seconds": 45,
    "ai_listen_keywords": [],
    "busy_window_seconds": 60,
    "overload_enable": True,
    "overload_min_messages": 20,
    "overload_min_unique_users": 3,
    "overload_notice_cooldown_seconds": 90,
    "overload_pause_seconds": 45,
}


def _template_trigger() -> dict:
    data = yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))
    return (data.get("config") or {}).get("trigger") or {}


def _builtin_trigger() -> dict:
    return _built_in_config_defaults().get("trigger") or {}


class TriggerConfigTruthSourceTests(unittest.TestCase):
    def test_both_truth_sources_have_identical_key_sets(self) -> None:
        """模板与内置默认值的键集合必须一模一样。

        core/config_manager.py 是 deep_merge_dict(template, raw) —— 模板在底。
        任一侧缺键就会出现「这台机器对、换台机器错」。
        """

        template = set(_template_trigger())
        builtin = set(_builtin_trigger())
        self.assertEqual(
            template,
            builtin,
            f"只在模板里={sorted(template - builtin)} / 只在内置默认里={sorted(builtin - template)}",
        )

    def test_both_truth_sources_have_identical_values(self) -> None:
        template = _template_trigger()
        builtin = _builtin_trigger()
        mismatched = {
            key: (template.get(key), builtin.get(key))
            for key in set(template) | set(builtin)
            if template.get(key) != builtin.get(key)
        }
        self.assertEqual(mismatched, {}, f"值不一致: {mismatched}")

    def test_previously_orphaned_keys_are_present(self) -> None:
        """九个曾经只有代码兜底的键，两处都要有。"""

        template = _template_trigger()
        builtin = _builtin_trigger()
        for key in _EXPECTED:
            with self.subTest(key=key):
                self.assertIn(key, template, f"{key} 不在 master.template.yml")
                self.assertIn(key, builtin, f"{key} 不在 _built_in_config_defaults()")

    def test_added_values_match_the_code_fallback(self) -> None:
        """补齐必须是零行为变更 —— 值要等于 core/trigger.py 的兜底值。"""

        template = _template_trigger()
        for key, expected in _EXPECTED.items():
            with self.subTest(key=key):
                self.assertEqual(
                    template[key],
                    expected,
                    f"{key} 模板值 {template[key]!r} 与代码兜底 {expected!r} 不一致，这已经不是补齐真相源而是改行为了",
                )

    def test_every_key_trigger_reads_exists_in_both_sources(self) -> None:
        """兜底防线：扫 core/trigger.py 里所有 trigger_config.get(...) 的键名，
        任何一个缺真相源都算失败。将来新增读取点会被这条挡住。"""

        source = _TRIGGER_PY.read_text(encoding="utf-8")
        read_keys = set(re.findall(r"trigger_config\.get\(\s*[\"']([a-z0-9_]+)[\"']", source))
        self.assertTrue(read_keys, "没扫到任何读取点，正则该更新了")

        template = set(_template_trigger())
        builtin = set(_builtin_trigger())
        missing = sorted(key for key in read_keys if key not in template or key not in builtin)
        self.assertEqual(
            missing,
            [],
            f"core/trigger.py 读了这些键但真相源缺失: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
