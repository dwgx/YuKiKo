"""Skill Workshop（create_skill / list_skills / remove_skill）回归测试。

对应 OpenClaw 技能提案→审批→生效闭环的保守形态：create_skill / remove_skill
由 super_admin 直接执行，写盘后 skill_registry.reload() 让技能立即可用。
"""

import tempfile
import unittest
from pathlib import Path

from core.agent_tools_registry import AgentToolRegistry
from core.skill_loader import (
    SkillRegistry,
    install_skill,
    register_skill_tools,
    remove_skill_dir,
)

_SUPER_ADMIN_CTX = {"permission_level": "super_admin"}
_USER_CTX = {"permission_level": "user"}


class TestInstallSkill(unittest.TestCase):
    def test_install_skill_writes_loadable_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ok, err = install_skill(base, "meeting-notes", "整理会议纪要", "1. 读取会议记录\n2. 输出要点")
            self.assertTrue(ok, err)

            skill_md = base / "meeting-notes" / "SKILL.md"
            self.assertTrue(skill_md.is_file())
            text = skill_md.read_text(encoding="utf-8")
            self.assertIn("name: meeting-notes", text)
            self.assertIn("description: 整理会议纪要", text)
            self.assertIn("1. 读取会议记录", text)

            # 写出的 SKILL.md 能被 loader 扫到
            registry = SkillRegistry(base)
            skills = registry.load()
            self.assertEqual([s.name for s in skills], ["meeting-notes"])

    def test_install_skill_rejects_invalid_name_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for bad in ("", "BadName", "VideoNotes", "a/b", "..", ".", "has space", "a;rm -rf /"):
                ok, _err = install_skill(base, bad, "desc", "content")
                self.assertFalse(ok, f"name should be rejected: {bad!r}")
            self.assertEqual(list(base.iterdir()), [])

    def test_install_skill_rejects_empty_description_or_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ok, _err = install_skill(base, "valid-name", "  ", "content")
            self.assertFalse(ok)
            ok, _err = install_skill(base, "valid-name", "desc", "   ")
            self.assertFalse(ok)
            self.assertEqual(list(base.iterdir()), [])


class TestRemoveSkillDir(unittest.TestCase):
    def test_remove_skill_dir_deletes_only_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            install_skill(base, "keep-me", "desc", "content")
            install_skill(base, "drop-me", "desc", "content")

            ok, err = remove_skill_dir(base, "drop-me")
            self.assertTrue(ok, err)
            self.assertFalse((base / "drop-me").exists())
            self.assertTrue((base / "keep-me" / "SKILL.md").is_file())

            ok, _err = remove_skill_dir(base, "drop-me")
            self.assertFalse(ok)  # 已删除 → 报不存在

    def test_remove_skill_dir_rejects_invalid_and_escaping_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            install_skill(base, "victim", "desc", "content")
            for bad in ("", "..", "../victim", "a/b"):
                ok, _err = remove_skill_dir(base, bad)
                self.assertFalse(ok, f"name should be rejected: {bad!r}")
            self.assertTrue((base / "victim" / "SKILL.md").is_file())


class TestSkillWorkshopTools(unittest.IsolatedAsyncioTestCase):
    def _make_workshop(self):
        """构造 Skill Workshop 工具环境：注册表 + 空技能目录（addCleanup 保证清理）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        registry = AgentToolRegistry()
        skill_registry = SkillRegistry(base)
        register_skill_tools(registry, skill_registry)
        return base, registry, skill_registry

    async def test_create_skill_writes_and_is_immediately_readable(self):
        base, registry, skill_registry = self._make_workshop()
        result = await registry._handlers["create_skill"](
            {
                "name": "meeting-notes",
                "description": "整理会议纪要",
                "content": "1. 读取会议记录\n2. 输出要点",
            },
            _SUPER_ADMIN_CTX,
        )
        self.assertTrue(result.ok, result.error)

        # 磁盘落盘
        skill_md = base / "meeting-notes" / "SKILL.md"
        self.assertTrue(skill_md.is_file())

        # 重载后立即可用：load / read_skill / describe 都能看到
        names = [s.name for s in skill_registry.load()]
        self.assertIn("meeting-notes", names)
        self.assertIn("name: meeting-notes", skill_registry.read_skill("meeting-notes"))
        self.assertIn("- meeting-notes:", skill_registry.describe())
        self.assertEqual(result.data["skill_count"], 1)

    async def test_create_skill_rejects_invalid_name_and_missing_args(self):
        _base, registry, skill_registry = self._make_workshop()
        bad = await registry._handlers["create_skill"](
            {"name": "BadName", "description": "d", "content": "c"}, _SUPER_ADMIN_CTX
        )
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error, "invalid_skill")

        missing = await registry._handlers["create_skill"]({"name": "ok-name", "description": "d"}, _SUPER_ADMIN_CTX)
        self.assertFalse(missing.ok)
        self.assertEqual(missing.error, "missing_required_args")

        self.assertEqual(skill_registry.load(), [])

    async def test_remove_skill_deletes_and_reloads(self):
        _base, registry, skill_registry = self._make_workshop()
        await registry._handlers["create_skill"](
            {"name": "temp-skill", "description": "d", "content": "c"}, _SUPER_ADMIN_CTX
        )
        self.assertEqual(len(skill_registry.load()), 1)

        result = await registry._handlers["remove_skill"]({"name": "temp-skill"}, _SUPER_ADMIN_CTX)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(skill_registry.load(), [])
        self.assertIsNone(skill_registry.read_skill("temp-skill"))

        gone = await registry._handlers["remove_skill"]({"name": "temp-skill"}, _SUPER_ADMIN_CTX)
        self.assertFalse(gone.ok)

    async def test_create_and_remove_skill_reject_non_super_admin(self):
        _base, registry, skill_registry = self._make_workshop()
        for level in ("user", "group_admin", ""):
            ctx = {"permission_level": level}
            created = await registry._handlers["create_skill"]({"name": "x", "description": "d", "content": "c"}, ctx)
            self.assertFalse(created.ok, f"create_skill should be blocked for {level!r}")
            self.assertEqual(created.error, "need_super_admin")

            removed = await registry._handlers["remove_skill"]({"name": "x"}, ctx)
            self.assertFalse(removed.ok, f"remove_skill should be blocked for {level!r}")
            self.assertEqual(removed.error, "need_super_admin")

        self.assertEqual(skill_registry.load(), [])

    async def test_list_skills_available_to_all_users(self):
        _base, registry, skill_registry = self._make_workshop()
        await registry._handlers["create_skill"](
            {"name": "visible-skill", "description": "d", "content": "c"}, _SUPER_ADMIN_CTX
        )

        result = await registry._handlers["list_skills"]({}, _USER_CTX)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 1)
        self.assertIn("visible-skill", result.display)
        self.assertEqual(result.data["names"], ["visible-skill"])

    def test_create_skill_is_in_super_admin_tool_set(self):
        """create_skill 必须留在 _SUPER_ADMIN_TOOLS：AgentLoop 的 _check_permission_gate
        依赖这个集合在 agent 层拦截普通用户调用。"""
        self.assertIn("create_skill", AgentToolRegistry._SUPER_ADMIN_TOOLS)


if __name__ == "__main__":
    unittest.main()
