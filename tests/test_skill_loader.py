"""skill_loader 模块的单元测试。"""

import platform
import tempfile
import unittest
from pathlib import Path

from core.skill_loader import (
    SkillMeta,
    SkillRegistry,
    evaluate_requires,
    load_skill,
    parse_frontmatter,
    render_catalog,
)


def _write_skill(base: Path, name: str, description: str, frontmatter_extra: str = "") -> Path:
    """在 base 下创建一个技能目录并返回其路径。"""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{frontmatter_extra}"
        "---\n"
    )
    (skill_dir / "SKILL.md").write_text(frontmatter + "# 指令体\n", encoding="utf-8")
    return skill_dir


class TestParseFrontmatter(unittest.TestCase):
    def test_parse_frontmatter_extracts_meta_and_body(self):
        text = (
            "---\n"
            "name: video-notes\n"
            "description: Extract timestamps from video transcripts\n"
            "metadata:\n"
            "  openclaw:\n"
            "    always: true\n"
            "    install:\n"
            "      - npm install -g ffmpeg-static\n"
            "---\n"
            "# Video Notes\n"
            "Use ffmpeg to extract timestamps."
        )
        frontmatter, body = parse_frontmatter(text)
        self.assertEqual(frontmatter["name"], "video-notes")
        self.assertEqual(frontmatter["description"], "Extract timestamps from video transcripts")
        self.assertTrue(frontmatter["metadata"]["openclaw"]["always"])
        self.assertIn("# Video Notes", body)
        self.assertIn("Use ffmpeg to extract timestamps.", body)

    def test_parse_frontmatter_missing_fence_returns_full_text(self):
        text = "plain body without fences"
        frontmatter, body = parse_frontmatter(text)
        self.assertEqual(frontmatter, {})
        self.assertEqual(body, text)


class TestLoadSkill(unittest.TestCase):
    def test_load_skill_requires_name_and_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            no_desc = base / "no-desc"
            no_desc.mkdir()
            (no_desc / "SKILL.md").write_text("---\nname: no-desc\n---\nbody", encoding="utf-8")
            self.assertIsNone(load_skill(no_desc))

            # 缺 name 时回退目录名，仍能加载
            no_name = base / "fallback-name"
            no_name.mkdir()
            (no_name / "SKILL.md").write_text(
                "---\ndescription: desc here\n---\nbody", encoding="utf-8"
            )
            skill = load_skill(no_name)
            self.assertIsNotNone(skill)
            self.assertEqual(skill.name, "fallback-name")
            self.assertEqual(skill.description, "desc here")

            # 缺 SKILL.md → None
            missing = base / "no-file"
            missing.mkdir()
            self.assertIsNone(load_skill(missing))

    def test_load_skill_rejects_invalid_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            bad = base / "bad-name-dir"
            bad.mkdir()
            (bad / "SKILL.md").write_text(
                "---\nname: 'Invalid Name'\ndescription: ok\n---\n", encoding="utf-8"
            )
            self.assertIsNone(load_skill(bad))

            upper = base / "upper-dir"
            upper.mkdir()
            (upper / "SKILL.md").write_text(
                "---\nname: VideoNotes\ndescription: ok\n---\n", encoding="utf-8"
            )
            self.assertIsNone(load_skill(upper))

            too_long = base / "long-name-dir"
            too_long.mkdir()
            (too_long / "SKILL.md").write_text(
                f"---\nname: {'a' * 65}\ndescription: ok\n---\n", encoding="utf-8"
            )
            self.assertIsNone(load_skill(too_long))

            long_desc = base / "long-desc"
            long_desc.mkdir()
            (long_desc / "SKILL.md").write_text(
                f"---\nname: long-desc\ndescription: {'d' * 1025}\n---\n", encoding="utf-8"
            )
            self.assertIsNone(load_skill(long_desc))

    def test_load_skill_reads_openclaw_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            skill_dir = _write_skill(
                base,
                "helper",
                "Helper skill",
                frontmatter_extra=(
                    "metadata:\n"
                    "  openclaw:\n"
                    "    always: true\n"
                    "    requires:\n"
                    "      os: darwin\n"
                    "      bins:\n"
                    "        - ls\n"
                    "    install:\n"
                    "      - pip install requests\n"
                ),
            )
            skill = load_skill(skill_dir)
            self.assertIsNotNone(skill)
            self.assertTrue(skill.always)
            self.assertEqual(skill.requires["os"], ["darwin"])
            self.assertEqual(skill.requires["bins"], ["ls"])
            self.assertEqual(skill.install, ["pip install requests"])

    def test_load_skill_flat_frontmatter_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            skill_dir = _write_skill(
                base,
                "flat-skill",
                "Flat skill",
                frontmatter_extra=(
                    "description_zh: 扁平技能\n"
                    "homepage: https://example.com/flat\n"
                    "user-invocable: false\n"
                    "disable-model-invocation: true\n"
                    "requires:\n"
                    "  os:\n"
                    "    - linux\n"
                    "  env:\n"
                    "    - API_KEY\n"
                ),
            )
            skill = load_skill(skill_dir)
            self.assertIsNotNone(skill)
            self.assertEqual(skill.description_zh, "扁平技能")
            self.assertEqual(skill.homepage, "https://example.com/flat")
            self.assertFalse(skill.user_invocable)
            self.assertTrue(skill.disable_model_invocation)
            self.assertEqual(skill.requires["os"], ["linux"])
            self.assertEqual(skill.requires["env"], ["API_KEY"])


class TestRenderCatalog(unittest.TestCase):
    def test_render_catalog_truncates_description(self):
        skill = SkillMeta(name="demo", description="d" * 300)
        catalog = render_catalog([skill], desc_max=50)
        line = catalog.splitlines()[0]
        self.assertTrue(line.startswith("- demo: "))
        self.assertIn("…", line)
        desc_part = line.split(": ", 1)[1]
        self.assertEqual(len(desc_part), 51)  # 50 字符 + 省略号

    def test_render_catalog_empty_description_shows_name_only(self):
        catalog = render_catalog([SkillMeta(name="silent", description="")])
        self.assertEqual(catalog, "- silent")

    def test_render_catalog_bisects_when_over_budget(self):
        skills = [SkillMeta(name=f"skill-{i}", description="word " * 20) for i in range(10)]
        catalog = render_catalog(skills, max_chars=120, desc_max=220)
        self.assertTrue(catalog.endswith("\n⚠️ Skills truncated"))
        body = catalog[: -len("\n⚠️ Skills truncated")]
        self.assertLessEqual(len(body), 120)
        self.assertNotIn("skill-9", catalog)


class TestEvaluateRequires(unittest.TestCase):
    def test_evaluate_requires_bins_and_env(self):
        meta = SkillMeta(
            name="req-demo",
            description="demo",
            requires={
                "bins": ["ls"],
                "anyBins": ["definitely-not-a-real-bin-xyz", "sh"],
                "env": ["FAKE_VAR"],
                "config": ["search.tool_interface.github_enable"],
            },
        )
        self.assertTrue(
            evaluate_requires(
                meta,
                env={"FAKE_VAR": "1"},
                config={"search": {"tool_interface": {"github_enable": True}}},
            )
        )
        self.assertFalse(
            evaluate_requires(
                meta,
                env={"FAKE_VAR": ""},
                config={"search": {"tool_interface": {"github_enable": True}}},
            )
        )
        self.assertFalse(
            evaluate_requires(
                meta,
                env={"FAKE_VAR": "1"},
                config={"search": {"tool_interface": {"github_enable": False}}},
            )
        )

    def test_evaluate_requires_bins_all_required(self):
        meta = SkillMeta(
            name="req-demo2",
            description="demo",
            requires={"bins": ["definitely-not-a-real-bin-xyz"]},
        )
        self.assertFalse(evaluate_requires(meta))

    def test_evaluate_requires_os_gate(self):
        current = platform.system().lower()
        on_windows = SkillMeta(
            name="req-demo3", description="demo", requires={"os": ["windows"]}
        )
        self.assertFalse(evaluate_requires(on_windows))
        on_current = SkillMeta(name="req-demo4", description="demo", requires={"os": current})
        self.assertTrue(evaluate_requires(on_current))

    def test_evaluate_requires_always_skips_gates(self):
        meta = SkillMeta(
            name="always-on",
            description="demo",
            always=True,
            requires={
                "os": ["windows"],
                "bins": ["definitely-not-a-real-bin-xyz"],
                "env": ["NOT_SET_VAR"],
            },
        )
        self.assertTrue(evaluate_requires(meta, env={}, config={}))


class TestSkillRegistry(unittest.TestCase):
    def test_skill_registry_load_match_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_skill(base, "video-notes", "Extract timestamps and summarize video transcripts")
            _write_skill(base, "web-search", "Search the web for the latest information")
            _write_skill(base, "weather-now", "Current weather in a city")

            registry = SkillRegistry(base)
            self.assertEqual(len(registry.load()), 3)

            self.assertEqual(registry.match("video transcript")[0].name, "video-notes")
            self.assertEqual(registry.match("web")[0].name, "web-search")
            self.assertLessEqual(len(registry.match("web")), 3)

            content = registry.read_skill("video-notes")
            self.assertIsNotNone(content)
            self.assertIn("name: video-notes", content)
            self.assertIn("# 指令体", content)

            catalog = registry.describe()
            self.assertIn("- video-notes:", catalog)
            self.assertIn("- web-search:", catalog)

    def test_skill_registry_rejects_missing_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_skill(base, "valid-skill", "A valid skill")
            (base / "no-skill-md").mkdir()
            (base / "stray.txt").write_text("not a dir", encoding="utf-8")

            registry = SkillRegistry(base)
            skills = registry.load()
            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0].name, "valid-skill")

            self.assertIsNone(registry.read_skill("no-skill-md"))
            self.assertIsNone(registry.read_skill("does-not-exist"))

            missing = SkillRegistry(base / "nope")
            self.assertEqual(missing.load(), [])
