"""B4：config schema 全量覆盖 + 类型漂移 + strict 模式。

覆盖三个目标：
1. CONFIG_SCHEMA 键集覆盖 master.template.yml config 段的每一个键（含中间 dict 节点）。
2. validate_config 能检出类型漂移（值类型与 schema 不符）。
3. ConfigManager.load 在 strict 模式（config.validation.strict / YUKIKO_CONFIG_STRICT=1）
   下校验失败抛 ConfigValidationError 阻断启动；默认只记 warning。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from core.config_manager import ConfigManager
from core.config_schema import CONFIG_SCHEMA, ConfigValidationError, validate_config
from core.config_templates import _built_in_config_defaults, deep_merge_dict, load_config_template

_REPO = Path(__file__).resolve().parents[1]
_TEMPLATE_FILE = _REPO / "config" / "templates" / "master.template.yml"


def _flatten_full(node: dict, prefix: str = "") -> dict:
    """记录模板 config 树的每一个键路径，dict 中间节点也算，便于比键集。"""
    out: dict = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        out[path] = value
        if isinstance(value, dict):
            out.update(_flatten_full(value, path))
    return out


def _canonical_config() -> dict:
    """模板 + 内置默认值合并出的规范配置（消费端合法形态）。"""
    base = deep_merge_dict(dict(_built_in_config_defaults()), dict(load_config_template()))
    base["bot"]["private_chat_mode"] = "off"
    return base


class SchemaTemplateCoverageTests(unittest.TestCase):
    """schema 键集必须覆盖模板 config 段的全部键。"""

    @classmethod
    def _template_keys(cls) -> set[str]:
        parsed = yaml.safe_load(_TEMPLATE_FILE.read_text(encoding="utf-8")) or {}
        return set(_flatten_full(parsed["config"]))

    def test_schema_covers_full_template_key_set(self) -> None:
        missing = sorted(self._template_keys() - set(CONFIG_SCHEMA))
        self.assertEqual(missing, [], f"模板 config 里有、schema 未覆盖的键：{missing}")

    def test_schema_has_no_keys_outside_template(self) -> None:
        extra = sorted(set(CONFIG_SCHEMA) - self._template_keys())
        self.assertEqual(extra, [], f"schema 里有、模板 config 不存在的键：{extra}")

    def test_canonical_config_passes_validation(self) -> None:
        self.assertEqual(validate_config(_canonical_config()), [])


class TypeDriftDetectionTests(unittest.TestCase):
    """值类型与 schema 不符时必须被检出。"""

    def test_drift_bool_where_str_expected_detected(self) -> None:
        cfg = _canonical_config()
        cfg["bot"]["private_chat_mode"] = False  # schema 期望 str
        hits = [i for i in validate_config(cfg) if i["path"] == "bot.private_chat_mode"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "type_mismatch")
        self.assertEqual(hits[0]["actual"], "bool")
        self.assertIn("str", hits[0]["expected"])

    def test_drift_str_where_int_expected_detected(self) -> None:
        cfg = _canonical_config()
        cfg["queue"]["group_concurrency"] = "3"
        hits = [i for i in validate_config(cfg) if i["path"] == "queue.group_concurrency"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["actual"], "str")

    def test_drift_str_where_number_expected_detected(self) -> None:
        cfg = _canonical_config()
        cfg["api"]["temperature"] = "0.7"
        hits = [i for i in validate_config(cfg) if i["path"] == "api.temperature"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["actual"], "str")

    def test_drift_list_where_dict_expected_detected(self) -> None:
        cfg = _canonical_config()
        cfg["bot"]["humanization_profile"] = ["warmth"]
        hits = [i for i in validate_config(cfg) if i["path"] == "bot.humanization_profile"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["actual"], "list")

    def test_float_accepts_int_but_rejects_bool(self) -> None:
        cfg = _canonical_config()
        cfg["routing"]["min_confidence"] = 1  # int 对 float 约束合法
        self.assertEqual(validate_config(cfg), [])
        cfg["routing"]["min_confidence"] = True  # bool 必须拒绝
        hits = [i for i in validate_config(cfg) if i["path"] == "routing.min_confidence"]
        self.assertEqual(len(hits), 1)


class ConfigManagerStrictModeTests(unittest.TestCase):
    """strict 模式阻断启动；默认 warning 不阻断。"""

    @staticmethod
    def _make_dirs(tmp: str) -> tuple[Path, Path]:
        config_dir = Path(tmp) / "config"
        storage_dir = Path(tmp) / "storage"
        config_dir.mkdir()
        storage_dir.mkdir()
        return config_dir, storage_dir

    def test_strict_config_key_raises_on_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir, storage_dir = self._make_dirs(tmp)
            (config_dir / "config.yml").write_text(
                "validation:\n  strict: true\nqueue:\n  group_concurrency: three\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigValidationError) as ctx:
                ConfigManager(config_dir, storage_dir)
            self.assertIn("queue.group_concurrency", str(ctx.exception))

    def test_strict_env_var_raises_on_drift(self) -> None:
        with mock.patch.dict(os.environ, {"YUKIKO_CONFIG_STRICT": "1"}):
            with tempfile.TemporaryDirectory() as tmp:
                config_dir, storage_dir = self._make_dirs(tmp)
                (config_dir / "config.yml").write_text(
                    "queue:\n  group_concurrency: three\n", encoding="utf-8"
                )
                with self.assertRaises(ConfigValidationError) as ctx:
                    ConfigManager(config_dir, storage_dir)
                self.assertIn("queue.group_concurrency", str(ctx.exception))

    def test_strict_clean_config_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir, storage_dir = self._make_dirs(tmp)
            (config_dir / "config.yml").write_text(
                "validation:\n  strict: true\n", encoding="utf-8"
            )
            cm = ConfigManager(config_dir, storage_dir)
            self.assertEqual(cm.last_validation_issues, [])

    def test_default_warns_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir, storage_dir = self._make_dirs(tmp)
            (config_dir / "config.yml").write_text(
                "queue:\n  group_concurrency: three\n", encoding="utf-8"
            )
            cm = ConfigManager(config_dir, storage_dir)  # 不抛异常
            hits = [
                i
                for i in cm.last_validation_issues
                if i["path"] == "queue.group_concurrency"
            ]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["actual"], "str")

    def test_reload_fails_in_strict_mode_on_drift(self) -> None:
        with mock.patch.dict(os.environ, {"YUKIKO_CONFIG_STRICT": "1"}):
            with tempfile.TemporaryDirectory() as tmp:
                config_dir, storage_dir = self._make_dirs(tmp)
                (config_dir / "config.yml").write_text("", encoding="utf-8")
                cm = ConfigManager(config_dir, storage_dir)  # 空配置合法
                self.assertEqual(cm.last_validation_issues, [])
                (config_dir / "config.yml").write_text(
                    "queue:\n  group_concurrency: three\n", encoding="utf-8"
                )
                ok, msg = cm.reload()
                self.assertFalse(ok)
                self.assertIn("queue.group_concurrency", msg)


if __name__ == "__main__":
    unittest.main()
