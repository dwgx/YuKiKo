"""Phase 3a：skill_loader 接入工具系统回归测试。

锁三件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.3（1）（2））：
1. register_skill_tools 注册 read_skill 工具，模型可读 SKILL.md 全文。
2. read_skill 按名读取、缺失返回错误、路径穿越被拦。
3. 渐进式披露：目录注入 system prompt，全文不进上下文。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core.agent_tools_registry import AgentToolRegistry
from core.skill_loader import SkillRegistry, register_skill_tools


def _make_skill_dir(tmp: Path, name: str = "my-skill", description: str = "测试技能") -> Path:
    skills_dir = tmp / "skills"
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n具体步骤正文",
        encoding="utf-8",
    )
    return skills_dir


class SkillToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _registry(self, skills_dir: Path) -> AgentToolRegistry:
        registry = AgentToolRegistry()
        register_skill_tools(registry, SkillRegistry(skills_dir))
        return registry

    async def test_read_skill_tool_returns_full_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = _make_skill_dir(Path(tmp))
            result = await self._registry(skills_dir).call(
                "read_skill", {"name": "my-skill"}, {}
            )
            self.assertTrue(result.ok, result.error)
            self.assertIn("具体步骤正文", result.display)
            self.assertIn("name: my-skill", result.display)

    async def test_read_skill_missing_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = _make_skill_dir(Path(tmp))
            result = await self._registry(skills_dir).call(
                "read_skill", {"name": "nonexistent"}, {}
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "skill_not_found")

    async def test_read_skill_path_traversal_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = _make_skill_dir(Path(tmp))
            result = await self._registry(skills_dir).call(
                "read_skill", {"name": "../../etc/passwd"}, {}
            )
            self.assertFalse(result.ok)
            self.assertNotIn("root:", result.display or "")

    async def test_skill_registry_loads_and_describes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sr = SkillRegistry(_make_skill_dir(Path(tmp)))
            self.assertEqual([s.name for s in sr.load()], ["my-skill"])
            catalog = sr.describe()
            self.assertIn("my-skill", catalog)
            self.assertIn("测试技能", catalog)


if __name__ == "__main__":
    unittest.main()
