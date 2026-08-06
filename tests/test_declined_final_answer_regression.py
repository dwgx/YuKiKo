"""拒绝不该被 tool_review 门推翻 —— 那会让机器人去做它刚拒绝的事。

## 实测（2026-08-06，trace 118886-5-f46539ff）

用户带链接要盗版软件。日志逐步：

```
step=0 final_answer  text="破解版这种东西涉及侵权，我这边不去搜也不会推。…"
       navigator_tool_policy_block | reason=final_answer_without_tool | evidence=url
step=1 web_search     query="VD 破解版 哔哩哔哩"        <- 去搜了刚说不搜的东西
step=2 final_answer   又答一遍
agent_done | steps=3 | time=36735ms
```

`_requires_tool_review_before_final` 只看**结构证据**（有 URL / 有媒体段 / 有
artifact），没有为「模型在拒绝」留豁免。这道门本身要保留 —— 它防的是
「有链接却说我看不到」，是真问题。但**拒绝不是「用嘴代替动手」**：
拒绝的时候本来就不该动手。

两重代价：

1. **完整性**：拒绝被自己下一步推翻。用户要盗版，机器人先说不帮，然后真去搜了。
   带上业主关心的封群风险看更糟 —— 换成 R18 或时政链接，被驳回的拒绝会把模型
   推去 fetch/搜索那个内容。
2. **延迟**：多烧两次 LLM 往返。实测该回合 36.7s，而回合 p50 是 31s。

## 修法为什么不用关键词判断

「这段文本像不像拒绝」用词表判不可靠，且 CLAUDE.md 明确反对启发式词表
（`strip_heuristic_prompt_lists` 会删掉 prompts.yml 里的词表）。
改成让模型在 `final_answer` 上显式声明 `declined=true` —— 结构化、可审计、
日志里能数出来有没有被滥用（`agent_final_answer_declined`）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_utility import register_sticker_tools  # noqa: F401  (确保模块已加载)

_AGENT = Path("core/agent.py")

def _registry_with_builtins() -> AgentToolRegistry:
    """最小注册：final_answer 属于内置工具，不需要真的 search/image/model。"""

    from core.agent_tools_registry import register_builtin_tools

    registry = AgentToolRegistry()
    register_builtin_tools(registry, None, None, None, {})
    return registry



def _gate_condition_sources() -> list[str]:
    """取出所有以 strict_tool_routing 开头的门判定条件源码。"""

    tree = ast.parse(_AGENT.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            src = ast.unparse(node.test)
            if "_requires_tool_review_before_final" in src:
                out.append(src)
    return out


class DeclinedFlagExemptsTheToolReviewGateTests(unittest.TestCase):
    def test_gate_conditions_are_findable(self) -> None:
        """自检：门的形状变了本守卫要红，而不是静默通过。"""

        self.assertTrue(
            _gate_condition_sources(),
            "在 core/agent.py 里找不到 _requires_tool_review_before_final 的门 —— "
            "代码形状变了，本守卫需要跟着改",
        )

    def test_final_answer_gate_honours_the_declined_flag(self) -> None:
        """final_answer 那道门必须放行已声明的拒绝。"""

        matching = [
            src
            for src in _gate_condition_sources()
            if "declined_request" in src
        ]
        self.assertTrue(
            matching,
            "final_answer 的 tool_review 门没有检查 declined —— 模型的拒绝仍会被驳回，"
            "然后它会去做刚拒绝的事。当前条件: "
            f"{_gate_condition_sources()}",
        )

    def test_gate_still_fires_when_not_declined(self) -> None:
        """反向：没声明拒绝时门必须照旧生效。

        这道门防的是「有链接却说我看不到」，不能因为加了豁免就整个失效。
        """

        for src in _gate_condition_sources():
            if "declined_request" not in src:
                continue
            self.assertIn(
                "strict_tool_routing",
                src,
                "门丢掉了 strict_tool_routing 前提",
            )
            self.assertIn(
                "tool_calls_made == 0",
                src,
                "门丢掉了「一个工具都没调」前提 —— 会把正常多步流程也放行",
            )


class DeclinedIsExposedToTheModelTests(unittest.TestCase):
    """模型不知道有这个字段的话，豁免永远不会被用到。"""

    def test_final_answer_schema_declares_declined(self) -> None:
        registry = _registry_with_builtins()
        schemas = registry.get_schemas_for_native_tools(["final_answer"])
        self.assertTrue(schemas, "final_answer 没注册？")
        props = schemas[0]["function"]["parameters"]["properties"]
        self.assertIn(
            "declined",
            props,
            "final_answer 的 schema 里没有 declined —— 模型无法声明拒绝，豁免形同虚设",
        )
        self.assertEqual(props["declined"]["type"], "boolean")

    def test_declined_is_not_required(self) -> None:
        """绝大多数回复不是拒绝，不能把它设成必填。"""

        registry = _registry_with_builtins()
        schema = registry.get_schemas_for_native_tools(["final_answer"])[0]
        required = schema["function"]["parameters"].get("required", [])
        self.assertNotIn("declined", required)

    def test_schema_description_warns_against_misuse(self) -> None:
        """必须写明「拿它跳过工具是错误使用」，否则模型会用它偷懒。"""

        registry = _registry_with_builtins()
        schema = registry.get_schemas_for_native_tools(["final_answer"])[0]
        desc = schema["function"]["parameters"]["properties"]["declined"]["description"]
        self.assertIn("错误使用", desc, "没有防滥用说明")


class PromptTellsTheModelNotToUndoItsOwnRefusalTests(unittest.TestCase):
    """三处真相源都要有这段，否则模板会覆盖 Python payload。"""

    ANCHOR = "declined=true"
    UNDO_ANCHOR = "不要再去搜"

    def _sources(self) -> dict[str, str]:
        return {
            path: Path(path).read_text(encoding="utf-8")
            for path in (
                "core/prompt_navigator.py",
                "config/templates/master.template.yml",
                "config/prompts.yml",
            )
        }

    def test_all_three_sources_mention_the_flag(self) -> None:
        for path, src in self._sources().items():
            with self.subTest(path=path):
                self.assertIn(self.ANCHOR, src, f"{path} 没告诉模型 declined 字段")

    def test_all_three_sources_forbid_undoing_the_refusal(self) -> None:
        """最关键的一句：拒绝之后不要再去做那件事。"""

        for path, src in self._sources().items():
            with self.subTest(path=path):
                self.assertIn(
                    self.UNDO_ANCHOR,
                    src,
                    f"{path} 没写明「拒绝后不要再去搜/解析/下载」",
                )

    def test_prompt_still_forbids_talking_instead_of_acting(self) -> None:
        """反向：原来那条「不要用嘴代替动手」不能被这个豁免冲掉。"""

        src = Path("core/prompt_navigator.py").read_text(encoding="utf-8")
        self.assertIn("不要用嘴代替动手", src)
        self.assertIn("也不允许说\"我看不到\"", src)


if __name__ == "__main__":
    unittest.main()
