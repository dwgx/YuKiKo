"""配置中心 — 加载 / 环境变量替换 / 解密 / 热重载。

用法:
    cm = ConfigManager(config_dir, storage_dir)
    cm.get("bot.name")          # 点路径访问
    ok, msg = cm.reload()       # 热重载
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from core.config_schema import ConfigValidationError, validate_config
from core.config_templates import deep_merge_dict, load_config_template
from core.crypto import DecryptionError, SecretManager

_log = logging.getLogger("yukiko.config")
_ENV_PATTERN = re.compile(r"^\$\{([A-Z0-9_]+)\}$")
# 环境变量置 1 时强制 strict 模式：配置校验失败抛异常阻断启动。
_STRICT_ENV_VAR = "YUKIKO_CONFIG_STRICT"
_STRICT_CONFIG_KEY = "validation.strict"


class ConfigManager:
    """单例式配置管理器，支持热重载。"""

    def __init__(self, config_dir: Path, storage_dir: Path):
        self._config_dir = config_dir
        self._storage_dir = storage_dir
        self._config_file = config_dir / "config.yml"
        self._secret = SecretManager(storage_dir / ".secret_key")
        self._data: dict[str, Any] = {}
        self._last_validation_issues: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.load()

    # ── public ────────────────────────────────────────────────
    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def get(self, dotpath: str, default: Any = None) -> Any:
        """点路径访问: get('bot.name') → config['bot']['name']"""
        keys = dotpath.split(".")
        node: Any = self._data
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
            else:
                return default
        return node if node is not None else default

    def load(self) -> None:
        """加载 config.yml 并处理环境变量 + 解密。"""
        with self._lock:
            raw = self._load_yaml(self._config_file)
            template = load_config_template()
            if isinstance(template, dict) and template:
                merged = deep_merge_dict(dict(template), raw if isinstance(raw, dict) else {})
                raw = merged
            resolved = self._resolve_env_vars(raw)
            try:
                decrypted = self._secret.decrypt_dict(resolved)  # type: ignore[assignment]
            except DecryptionError as exc:
                raise RuntimeError(f"配置中的加密字段无法解密: {exc}") from exc
            # 先校验（strict 下失败抛异常，_data 保持旧值）再落盘，避免热重载失败形成双态：
            # config_manager 服务新非法配置而 engine.config 还是旧对象。
            self._validate_runtime_config(decrypted)
            self._data = decrypted
            _log.info("配置已加载: %s", self._config_file)

    def reload(self) -> tuple[bool, str]:
        """热重载配置。返回 (成功, 消息)。"""
        try:
            self.load()
            return True, "配置已重载"
        except Exception as exc:
            msg = f"配置重载失败: {exc}"
            _log.error(msg)
            return False, msg

    @property
    def secret(self) -> SecretManager:
        return self._secret

    @property
    def last_validation_issues(self) -> list[dict[str, Any]]:
        """最近一次 load() 的 schema 校验结果（类型不匹配 / 必填缺失）。"""
        return list(self._last_validation_issues)

    # ── private ───────────────────────────────────────────────
    def _validate_runtime_config(self, data: dict[str, Any] | None = None) -> None:
        """按 schema 校验运行时配置。

        漂移（模板与内置默认值、或用户 config.yml 的类型矛盾）静默错下去会
        拖到消费端才炸，这里在启动/热重载时尽早暴露。默认只记 warning；
        当 `validation.strict: true` 或环境变量 `YUKIKO_CONFIG_STRICT=1` 时
        校验失败抛 ConfigValidationError 阻断启动。
        """
        target = data if data is not None else self._data
        self._last_validation_issues = validate_config(target)
        for issue in self._last_validation_issues:
            _log.warning(
                "config_validation | kind=%s | path=%s | expected=%s | actual=%s",
                issue["kind"],
                issue["path"],
                issue["expected"],
                issue["actual"],
            )
        if self._last_validation_issues and self._strict_mode_enabled(target):
            detail = "; ".join(
                f"{i['path']}: expected {i['expected']}, got {i['actual']}"
                for i in self._last_validation_issues
            )
            raise ConfigValidationError(f"配置校验失败（strict 模式）: {detail}")

    def _strict_mode_enabled(self, data: dict[str, Any] | None = None) -> bool:
        """strict 模式开关：环境变量 YUKIKO_CONFIG_STRICT=1 或配置 validation.strict=true。

        从**待校验的配置**（而非已落盘的 self._data）读取 validation.strict，
        否则 strict 配置本身在热重载/首次加载时读不到。
        """
        env_strict = os.environ.get(_STRICT_ENV_VAR, "") == "1"
        target = data if data is not None else self._data
        raw_strict: Any = False
        if isinstance(target, dict):
            validation_cfg = target.get("validation")
            if isinstance(validation_cfg, dict):
                raw_strict = validation_cfg.get("strict", False)
        cfg_strict = str(raw_strict).strip().lower() in {"1", "true", "yes"}
        return env_strict or cfg_strict

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            _log.warning("配置文件不存在: %s", path)
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}

    @classmethod
    def _resolve_env_vars(cls, data: Any) -> Any:
        """递归替换 ${VAR_NAME} 为环境变量值。"""
        if isinstance(data, dict):
            return {k: cls._resolve_env_vars(v) for k, v in data.items()}
        if isinstance(data, list):
            return [cls._resolve_env_vars(v) for v in data]
        if isinstance(data, str):
            m = _ENV_PATTERN.fullmatch(data.strip())
            if m:
                return os.environ.get(m.group(1), "")
        return data
