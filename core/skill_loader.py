"""SKILL.md 声明式技能加载器。

为 YuKiKo 提供兼容 Anthropic Agent Skills / OpenClaw / WorkBuddy 的
SKILL.md 技能生态。技能目录布局：

    skills/<name>/SKILL.md            # YAML frontmatter + markdown 指令体
    skills/<name>/scripts/            # 可选脚本
    skills/<name>/references/         # 可选参考资料
    skills/<name>/assets/             # 可选资源

本模块只负责解析与目录编排，不执行任何技能内容。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
_MAX_NAME_LEN = 64
_MAX_DESC_LEN = 1024
_REQUIRE_KEYS = ("bins", "anyBins", "env", "config", "os")


@dataclass(frozen=True)
class SkillMeta:
    """单个技能的元数据。"""

    name: str
    description: str
    description_zh: str = ""
    homepage: str | None = None
    user_invocable: bool = True
    disable_model_invocation: bool = False
    always: bool = False
    requires: dict[str, Any] = field(default_factory=dict)  # {bins:[], anyBins:[], env:[], config:[], os:[]}
    install: list[str] = field(default_factory=list)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 SKILL.md 的 YAML frontmatter。

    识别首行 ``---`` 围栏，围栏内用 yaml.safe_load 解析；无 frontmatter 时返回 ({}, 原文)。
    """
    if text.startswith("\ufeff"):
        text = text[len("\ufeff"):]
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    lines = lines[1:]
    end_idx = None
    for idx, line in enumerate(lines):
        if line.strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, text
    yaml_block = "\n".join(lines[:end_idx])
    body = "\n".join(lines[end_idx + 1:])
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        data = {}
    return data, body


def _get_openclaw_meta(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """从 frontmatter 中取 metadata.openclaw 块，没有则返回空 dict。"""
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict):
        openclaw = metadata.get("openclaw")
        if isinstance(openclaw, dict):
            return openclaw
    return {}


def _as_bool(value: Any, default: bool) -> bool:
    """把 YAML 值转成 bool；非布尔可识别字符串，否则用默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_str_list(value: Any) -> list[str]:
    """把标量规整成单元素列表，列表原样转字符串列表，空值返回空列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _normalize_requires(value: Any) -> dict[str, Any]:
    """把 requires 规整成 {bins, anyBins, env, config, os} 五键列表形式。"""
    if not isinstance(value, dict):
        value = {}
    return {key: _as_str_list(value.get(key)) for key in _REQUIRE_KEYS}


def load_skill(dir_path: Path) -> SkillMeta | None:
    """从目录加载单个技能；SKILL.md 缺失或元数据不合法时返回 None。"""
    dir_path = Path(dir_path)
    skill_md = dir_path / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    frontmatter, _body = parse_frontmatter(text)

    name = frontmatter.get("name") or dir_path.name
    description = frontmatter.get("description")
    if not name or not description:
        return None
    name = str(name).strip()
    description = str(description).strip()
    if len(name) > _MAX_NAME_LEN or not _NAME_PATTERN.fullmatch(name):
        return None
    if len(description) > _MAX_DESC_LEN:
        return None

    openclaw = _get_openclaw_meta(frontmatter)

    description_zh = frontmatter.get("description_zh")
    homepage = frontmatter.get("homepage")
    user_invocable = _as_bool(
        frontmatter.get("user-invocable", frontmatter.get("user_invocable", True)), True
    )
    disable_model_invocation = _as_bool(
        frontmatter.get(
            "disable-model-invocation", frontmatter.get("disable_model_invocation", False)
        ),
        False,
    )
    always = _as_bool(openclaw.get("always", frontmatter.get("always", False)), False)
    install = _as_str_list(openclaw.get("install", frontmatter.get("install", [])))
    requires = _normalize_requires(openclaw.get("requires", frontmatter.get("requires", {})))

    return SkillMeta(
        name=name,
        description=description,
        description_zh=str(description_zh) if description_zh else "",
        homepage=str(homepage) if homepage else None,
        user_invocable=user_invocable,
        disable_model_invocation=disable_model_invocation,
        always=always,
        requires=requires,
        install=install,
    )


def render_catalog(skills: list[SkillMeta], max_chars: int = 18000, desc_max: int = 220) -> str:
    """把技能列表渲染成一行一技能的目录；超预算时二分裁剪并附截断提示。"""
    lines: list[str] = []
    for skill in skills:
        if not skill.description:
            lines.append(f"- {skill.name}")
            continue
        description = skill.description
        if len(description) > desc_max:
            description = description[:desc_max] + "…"
        lines.append(f"- {skill.name}: {description}")

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text

    low, high = 0, len(lines)
    while low < high:
        mid = (low + high + 1) // 2
        if len("\n".join(lines[:mid])) <= max_chars:
            low = mid
        else:
            high = mid - 1

    return "\n".join(lines[:low]) + "\n⚠️ Skills truncated"


def _resolve_dot_path(config: dict[str, Any], dot_path: str) -> Any:
    """按点路径逐层取值，任意一层缺失返回 None。"""
    node: Any = config
    for part in dot_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def evaluate_requires(
    meta: SkillMeta,
    env: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """按 requires 全部门控评估技能在当前环境下是否可用。

    meta.always 为 True 时直接放行；env 为 None 时回退读取真实环境变量，
    config 为 None 时视为空配置（存在 config 门控则判否）。
    """
    if meta.always:
        return True

    requires = meta.requires or {}

    os_list = _as_str_list(requires.get("os"))
    if os_list and platform.system().lower() not in os_list:
        return False

    for binary in _as_str_list(requires.get("bins")):
        if shutil.which(binary) is None:
            return False

    any_bins = _as_str_list(requires.get("anyBins"))
    if any_bins and not any(shutil.which(binary) for binary in any_bins):
        return False

    for var in _as_str_list(requires.get("env")):
        value = os.getenv(var) if env is None else env.get(var)
        if not value:
            return False

    cfg = config if config is not None else {}
    for dot_path in _as_str_list(requires.get("config")):
        if not _resolve_dot_path(cfg, dot_path):
            return False

    return True


def _tokenize(text: str) -> list[str]:
    """把查询文本切成小写词元；纯 CJK 时退化为按空白切分。"""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if tokens:
        return tokens
    return [token for token in text.lower().split() if token]


class SkillRegistry:
    """扫描技能目录并对外提供查询、匹配与全文读取。"""

    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)
        self._loaded: list[SkillMeta] | None = None

    def load(self) -> list[SkillMeta]:
        """扫描 skills_dir 下每个子目录的 SKILL.md，返回全部合法技能（带缓存）。"""
        if self._loaded is not None:
            return self._loaded
        skills: list[SkillMeta] = []
        if self.skills_dir.is_dir():
            for child in sorted(self.skills_dir.iterdir()):
                if child.is_dir():
                    skill = load_skill(child)
                    if skill is not None:
                        skills.append(skill)
        self._loaded = skills
        return self._loaded

    def match(self, text: str, top_k: int = 3) -> list[SkillMeta]:
        """按关键词在 name/description 中的出现次数排序，返回 top_k 个技能。"""
        skills = self.load()
        keywords = _tokenize(text)
        if not keywords:
            return skills[:top_k]

        def score(skill: SkillMeta) -> int:
            haystack = f"{skill.name} {skill.description} {skill.description_zh}".lower()
            return sum(haystack.count(keyword) for keyword in keywords)

        return sorted(skills, key=score, reverse=True)[:top_k]

    def describe(self) -> str:
        """渲染技能目录文本。"""
        return render_catalog(self.load())

    def read_skill(self, name: str) -> str | None:
        """返回 skills_dir/name/SKILL.md 的全文（含 frontmatter），不存在返回 None。

        resolve() 后校验真实路径在 skills_dir 内，防 symlink 逃逸读越界文件。
        """
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        base = self.skills_dir.resolve()
        skill_md = (self.skills_dir / name / "SKILL.md").resolve()
        if not skill_md.is_file() or not str(skill_md).startswith(str(base)):
            return None
        return skill_md.read_text(encoding="utf-8", errors="replace")


def register_skill_tools(registry: Any, skill_registry: SkillRegistry) -> None:
    """把 read_skill 工具注册进 AgentToolRegistry。

    渐进式披露：技能目录（name+description）已由 AgentLoop 注入 system prompt，
    模型命中后调 read_skill 读取 SKILL.md 全文再执行。全文不进上下文，避免挤占预算。
    """
    from core.agent_tools_types import ToolCallResult, ToolSchema

    async def _handle_read_skill(args: dict[str, Any], context: Any) -> ToolCallResult:
        _ = context
        name = str(args.get("name", "")).strip()
        text = skill_registry.read_skill(name)
        if not text:
            return ToolCallResult(
                ok=False,
                error="skill_not_found",
                display=f"技能 '{name}' 不存在或未加载",
            )
        return ToolCallResult(
            ok=True,
            data={"name": name, "chars": len(text)},
            display=text,
        )

    registry.register(
        ToolSchema(
            name="read_skill",
            description="读取一个技能（skill）的 SKILL.md 全文，获取该技能的详细执行步骤。技能列表见系统提示的『可用技能』。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名（SKILL.md frontmatter 里的 name，小写连字符）",
                    },
                },
                "required": ["name"],
            },
            category="general",
        ),
        _handle_read_skill,
    )
