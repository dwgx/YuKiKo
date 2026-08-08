from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config_manager import ConfigManager
from core.config_schema import validate_config
from core.config_templates import _built_in_config_defaults, deep_merge_dict, load_config_template


def _canonical_config() -> dict:
    """模板 + 内置默认值合并出的规范配置，并把已知漂移点纠正为 schema 期望值。"""
    base = deep_merge_dict(dict(_built_in_config_defaults()), dict(load_config_template()))
    # 已知漂移：模板 private_chat_mode=false(bool) vs 内置 "off"(str)。
    # 消费端按字符串模式读取，schema 期望 str —— 这里用修正值构造合法配置。
    base["bot"]["private_chat_mode"] = "off"
    return base


class ConfigSchemaValidationTests(unittest.TestCase):
    def test_valid_config_has_no_issues(self) -> None:
        issues = validate_config(_canonical_config())
        self.assertEqual(issues, [])

    def test_type_mismatch_detected_for_bool_as_str(self) -> None:
        cfg = _canonical_config()
        cfg["bot"]["private_chat_mode"] = False  # schema 期望 str
        issues = validate_config(cfg)
        hits = [i for i in issues if i["path"] == "bot.private_chat_mode"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "type_mismatch")
        self.assertIn("str", hits[0]["expected"])
        self.assertEqual(hits[0]["actual"], "bool")

    def test_type_mismatch_detected_for_int_as_str(self) -> None:
        cfg = _canonical_config()
        cfg["queue"]["group_concurrency"] = "3"
        issues = validate_config(cfg)
        hits = [i for i in issues if i["path"] == "queue.group_concurrency"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "type_mismatch")
        self.assertEqual(hits[0]["actual"], "str")

    def test_bool_rejected_where_int_expected(self) -> None:
        cfg = _canonical_config()
        cfg["queue"]["message_ttl_seconds"] = True  # bool 是 int 子类，必须拒绝
        issues = validate_config(cfg)
        hits = [i for i in issues if i["path"] == "queue.message_ttl_seconds"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "type_mismatch")

    def test_float_accepts_int(self) -> None:
        cfg = _canonical_config()
        cfg["routing"]["min_confidence"] = 1  # int 对 float 约束是允许的
        issues = validate_config(cfg)
        self.assertEqual(issues, [])

    def test_required_section_missing_detected(self) -> None:
        cfg = _canonical_config()
        del cfg["bot"]
        issues = validate_config(cfg)
        hits = [i for i in issues if i["path"] == "bot" and i["kind"] == "missing"]
        self.assertEqual(len(hits), 1)
        # 整段缺失时叶键不应重复报 missing
        leaf_missing = [
            i for i in issues if i["path"].startswith("bot.") and i["kind"] == "missing"
        ]
        self.assertEqual(leaf_missing, [])

    def test_nested_section_wrong_type_detected(self) -> None:
        cfg = _canonical_config()
        cfg["bot"]["humanization_profile"] = "oops"  # 期望 dict
        issues = validate_config(cfg)
        hits = [i for i in issues if i["path"] == "bot.humanization_profile"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "type_mismatch")
        self.assertEqual(hits[0]["actual"], "str")

    def test_list_expected_rejects_str(self) -> None:
        cfg = _canonical_config()
        cfg["bot"]["nicknames"] = "yuki"  # 期望 list
        issues = validate_config(cfg)
        hits = [i for i in issues if i["path"] == "bot.nicknames"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "type_mismatch")


class ConfigManagerValidationIntegrationTests(unittest.TestCase):
    def test_last_validation_issues_accessible_and_clean_on_valid_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            storage_dir = Path(tmp) / "storage"
            config_dir.mkdir()
            storage_dir.mkdir()
            cm = ConfigManager(config_dir, storage_dir)
            # 模板 + 内置默认值合并出的规范配置不应有告警
            self.assertEqual(cm.last_validation_issues, [])
            # 返回的是拷贝，外部修改不影响内部状态
            issues = cm.last_validation_issues
            issues.append({"kind": "x", "path": "p", "expected": "y", "actual": "z", "value": None})
            self.assertEqual(cm.last_validation_issues, [])

    def test_user_wrong_type_in_config_yml_flags_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            storage_dir = Path(tmp) / "storage"
            config_dir.mkdir()
            storage_dir.mkdir()
            (config_dir / "config.yml").write_text(
                "queue:\n  group_concurrency: three\n", encoding="utf-8"
            )
            cm = ConfigManager(config_dir, storage_dir)
            hits = [
                i
                for i in cm.last_validation_issues
                if i["path"] == "queue.group_concurrency"
            ]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["actual"], "str")

    def test_load_does_not_raise_on_validation_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            storage_dir = Path(tmp) / "storage"
            config_dir.mkdir()
            storage_dir.mkdir()
            (config_dir / "config.yml").write_text(
                "bot:\n  private_chat_mode: false\n  nicknames: notalist\n",
                encoding="utf-8",
            )
            cm = ConfigManager(config_dir, storage_dir)  # 不应抛异常
            ok, msg = cm.reload()
            self.assertTrue(ok, msg)
            self.assertGreaterEqual(len(cm.last_validation_issues), 1)


if __name__ == "__main__":
    unittest.main()
