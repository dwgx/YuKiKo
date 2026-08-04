"""A10 + C3 回归：prompts 词表剪枝的两条绕过路径，以及 YAML 里不再留关键词表。

C3：`_strip_heuristic_prompt_lists` 原先只作用于 `_built_in_prompts_defaults()`，
`load_prompts_template()`（返回磁盘模板原文）与 `prompt_loader.reload()`（合并之后）
都没过它，于是升级装机的 `prompts.yml` 能把词表一直留着。

A10：模板 / prompts.yml 里不许有「某个词 → 某个工具」的对照表；
分区菜单 + 工具 schema 才是模型选工具的唯一依据。
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import core.config_templates as ct
import core.prompt_loader as pl
import yaml
from core.config_templates import _built_in_config_defaults

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_TEMPLATE = _REPO_ROOT / "config" / "templates" / "master.template.yml"
_REAL_PROMPTS = _REPO_ROOT / "config" / "prompts.yml"


class _IsolatedPromptFilesMixin(unittest.TestCase):
    """把模板与 prompts.yml 指向临时副本。

    `prompt_loader` 的 `_cache` / `_loaded` 是模块级全局，别的测试
    （如 test_router_prompt_scope_regression）会读它，所以必须完整还原。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_dir = Path(self._tmp.name)
        self._saved_template = ct._TEMPLATE_FILE
        self._saved_prompts_file = pl._PROMPTS_FILE
        self._saved_cache = copy.deepcopy(pl._cache)
        self._saved_loaded = pl._loaded

    def tearDown(self) -> None:
        ct._TEMPLATE_FILE = self._saved_template
        pl._PROMPTS_FILE = self._saved_prompts_file
        ct.reload_template()
        pl._cache = self._saved_cache
        pl._loaded = self._saved_loaded
        self._tmp.cleanup()

    def _write_template(self, mutate) -> Path:
        payload = yaml.safe_load(_REAL_TEMPLATE.read_text(encoding="utf-8"))
        mutate(payload)
        path = self._tmp_dir / "master.template.yml"
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        ct._TEMPLATE_FILE = path
        ct.reload_template()
        return path

    def _write_prompts(self, payload: dict) -> Path:
        path = self._tmp_dir / "prompts.yml"
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        pl._PROMPTS_FILE = path
        pl._cache = {}
        pl._loaded = False
        return path


class StripHeuristicPromptListsBypassTests(_IsolatedPromptFilesMixin):
    def test_should_prune_cue_lists_that_live_in_the_on_disk_template(self) -> None:
        """绕过路径 1：模板是磁盘文件，原先 load_prompts_template 原样返回。"""

        def mutate(payload: dict) -> None:
            payload["prompts"]["agent"]["legacy_music_cues"] = ["点歌", "播歌", "来首"]
            payload["prompts"]["routing_patterns"] = ["^/music", "^/download"]

        self._write_template(mutate)

        loaded = ct.load_prompts_template()

        self.assertNotIn("legacy_music_cues", loaded["agent"])
        self.assertNotIn("routing_patterns", loaded)

    def test_should_keep_cue_suffixed_keys_whose_value_is_not_a_list(self) -> None:
        """只剪 list 值。同后缀的说明文字不能被顺手删掉。"""

        def mutate(payload: dict) -> None:
            payload["prompts"]["agent"]["explain_tokens"] = "这是一段说明文字，不是词表"

        self._write_template(mutate)

        loaded = ct.load_prompts_template()

        self.assertEqual(
            loaded["agent"]["explain_tokens"], "这是一段说明文字，不是词表"
        )

    def test_should_prune_cue_lists_already_persisted_in_prompts_file(self) -> None:
        """绕过路径 2：_merge_with_defaults 只回填不覆盖，老词表能一直活着。"""
        path = self._write_prompts(
            {
                "agent": {"identity": "手改过的身份文案", "stale_download_cues": ["下载", "安装包"]},
                "messages": {"no_result": "手改过的兜底文案"},
                "legacy_regexes": ["^/music"],
            }
        )

        pl.reload()

        self.assertNotIn("stale_download_cues", pl._cache["agent"])
        self.assertNotIn("legacy_regexes", pl._cache)

        on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertNotIn(
            "stale_download_cues",
            on_disk["agent"],
            "只剪内存不算修好：词表必须真的从 prompts.yml 磁盘上消失",
        )
        self.assertNotIn("legacy_regexes", on_disk)

    def test_should_not_overwrite_hand_edited_prompt_values_while_pruning(self) -> None:
        """剪枝不能破坏「只回填缺失 key、不覆盖已有 key」这个语义。"""
        path = self._write_prompts(
            {
                "agent": {"identity": "手改过的身份文案", "stale_download_cues": ["下载"]},
                "messages": {"no_result": "手改过的兜底文案"},
            }
        )

        pl.reload()

        self.assertEqual(pl.get_nested("agent", "identity"), "手改过的身份文案")
        self.assertEqual(pl.get_message("no_result"), "手改过的兜底文案")

        on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["agent"]["identity"], "手改过的身份文案")
        # 回填仍然发生：模板里有、prompts.yml 里缺的 key 要补进来。
        self.assertIn("prompt_navigator", on_disk)

    def test_should_report_pruned_key_paths_for_audit(self) -> None:
        """剪掉什么必须可审计，否则线上永远不知道自己丢了哪些键。"""
        payload = {
            "agent": {"identity": "x", "a_cues": ["w"]},
            "nested": {"deep": {"b_patterns": ["p"]}},
            "keep": {"c_tokens": "字符串不剪"},
        }

        stripped, pruned = ct.strip_heuristic_prompt_lists(payload, source="unit-test")

        self.assertEqual(sorted(pruned), ["agent.a_cues", "nested.deep.b_patterns"])
        self.assertEqual(stripped["keep"]["c_tokens"], "字符串不剪")
        self.assertEqual(stripped["agent"]["identity"], "x")


class YamlKeywordTableRemovalTests(unittest.TestCase):
    """A10：模板与 prompts.yml 里不再有关键词/短语表。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.template = yaml.safe_load(_REAL_TEMPLATE.read_text(encoding="utf-8"))
        cls.prompts = yaml.safe_load(_REAL_PROMPTS.read_text(encoding="utf-8"))

    def test_prompts_section_ships_no_cue_suffixed_lists(self) -> None:
        suffixes = ("_cues", "_patterns", "_regexes", "_tokens")

        def offenders(node, path=""):
            found = []
            if isinstance(node, dict):
                for key, value in node.items():
                    child = f"{path}.{key}" if path else str(key)
                    if str(key).endswith(suffixes) and isinstance(value, list):
                        found.append(child)
                    found.extend(offenders(value, child))
            return found

        self.assertEqual(offenders(self.template["prompts"]), [])
        self.assertEqual(offenders(self.prompts), [])

    def test_agent_prompts_no_longer_carry_a_word_to_tool_table(self) -> None:
        """`tool_priority` 是一张字面词 → 工具的对照表，已删除。

        它无条件打印、不受当前分区约束，实测在 20 个分区里点名的 25 个工具
        有 17~24 个在当前分区被 scoped_tools() 物理隐藏，等于教模型去要够不到的
        工具，与分区菜单抢同一个决策权。
        """
        for label, agent_prompts in (
            ("template", self.template["prompts"]["agent"]),
            ("prompts.yml", self.prompts["agent"]),
        ):
            self.assertNotIn("tool_priority", agent_prompts, label)

        blob = yaml.safe_dump(
            {"t": self.template["prompts"], "p": self.prompts}, allow_unicode=True
        )
        for mapping in ("点歌 →", "打卡/签到 →", "画图/生图 →", "排行榜 →", "图片文字 →"):
            self.assertNotIn(mapping, blob, f"残留词→工具映射: {mapping}")

    def test_tool_hints_describe_capability_instead_of_trigger_words(self) -> None:
        """tool_hints 按工具名查表（机制正确），但文案不许教触发词。"""
        for hints in (self.template["prompts"]["tool_hints"], self.prompts["tool_hints"]):
            self.assertNotIn("用户说打卡/签到时调用", hints["checkin"])
            self.assertIn("打卡", hints["checkin"])  # 能力描述本身要留住
            self.assertNotIn("用户直接发视频或回复视频时用它", hints["analyze_local_video"])

    def test_builtin_config_defaults_ship_no_gated_keyword_tables(self) -> None:
        """这些键出厂即空表，且门 control.heuristic_rules_enable 默认 False
        且不在出厂配置里 —— 实测填入真实词也毫无效果。留着只是邀请别人加词表。
        """
        defaults = _built_in_config_defaults()
        memory_cfg = defaults["memory"]
        for key in (
            "preferred_name_patterns",
            "preferred_name_invalid_parts",
            "preferred_name_blocklist",
            "preferred_name_block_patterns",
            "high_risk_confirm_enable_patterns",
            "high_risk_confirm_disable_patterns",
        ):
            self.assertNotIn(key, memory_cfg)

        knowledge_cfg = defaults.get("knowledge_update", {})
        for key in (
            "fragment_only_texts",
            "fragment_short_max_len",
            "invalid_fact_titles",
            "invalid_fact_title_patterns",
            "name_preference_patterns",
            "name_preference_blocklist",
            "name_preference_block_patterns",
        ):
            self.assertNotIn(key, knowledge_cfg)

        high_risk = defaults["agent"]["high_risk_control"]
        # 用自然语言开关「高危确认」= 拿意图猜测去动安全开关，删掉。
        self.assertNotIn("user_enable_patterns", high_risk)
        self.assertNotIn("user_disable_patterns", high_risk)
        # 但匹配工具名 / 工具描述的结构事实是护栏，必须留着。
        self.assertTrue(high_risk["tool_name_patterns"])
        self.assertTrue(high_risk["description_patterns"])

    def test_safety_and_identity_term_lists_are_deliberately_kept(self) -> None:
        """保留清单：安全过滤、身份别名、输出协议泄漏标记不属于意图启发式。"""
        defaults = _built_in_config_defaults()
        self.assertIn("custom_block_terms", defaults["safety"])
        self.assertIn("custom_allow_terms", defaults["safety"])
        self.assertTrue(defaults["bot"]["nicknames"])
        self.assertIn("<invoke", defaults["bot"]["sanitize_banned_phrases"])

    def test_template_and_prompts_file_agree_field_by_field(self) -> None:
        """改 prompt 必须三处一致。_merge_with_defaults 只回填不覆盖，
        所以只改模板不改 prompts.yml 是到不了运行期的。
        """

        def flatten(node, prefix=""):
            out = {}
            if isinstance(node, dict):
                for key, value in node.items():
                    path = f"{prefix}.{key}" if prefix else str(key)
                    if isinstance(value, dict):
                        out.update(flatten(value, path))
                    else:
                        out[path] = value
            return out

        template_flat = flatten(self.template["prompts"])
        prompts_flat = flatten(self.prompts)

        self.assertEqual(
            sorted(template_flat.keys()),
            sorted(prompts_flat.keys()),
            "模板与 prompts.yml 的键集合必须一致",
        )
        mismatched = [k for k in template_flat if template_flat[k] != prompts_flat[k]]
        self.assertEqual(mismatched, [])


if __name__ == "__main__":
    unittest.main()
