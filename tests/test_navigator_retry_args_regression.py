from __future__ import annotations

import unittest
from typing import Any

from core.agent import AgentLoop
from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_types import ToolSchema


class _Ctx:
    """AgentContext 的最小替身：只带被校验路径读到的字段。"""

    def __init__(self, pending: tuple[str, dict[str, Any]] | None, visible: list[str]) -> None:
        self.navigator_pending_tool_retry = pending
        self.native_tools = visible
        self.navigator_state = None
        self.trace_id = "t"


class NavigatorRetryArgsRegressionTests(unittest.TestCase):
    """LLM 超时后代码会用一个小 prompt 合成一次工具调用。

    实测 `知乎上搜一下 rust 值不值得学`：合成出 `{"query": "..."}` 漏掉必填的
    `mode`，工具直接报 `invalid_args:missing_required_args:mode`，白烧一步预算。
    这次调用的参数不是模型在完整上下文里给的，缺必填字段就该丢掉，让模型自己在
    目标分区重调 —— 它看得到完整 schema，比小 prompt 猜得准。
    """

    def _loop(self) -> AgentLoop:
        registry = AgentToolRegistry()
        registry.register(
            ToolSchema(
                name="search_zhihu",
                description="搜索知乎",
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string"},
                        "query": {"type": "string"},
                    },
                    "required": ["mode"],
                },
                category="search",
            ),
            lambda args, context: None,
        )
        registry.register(
            ToolSchema(
                name="no_required",
                description="无必填参数",
                parameters={"type": "object", "properties": {}},
                category="search",
            ),
            lambda args, context: None,
        )
        loop = AgentLoop.__new__(AgentLoop)
        loop.tool_registry = registry
        return loop

    def test_missing_required_arg_is_detected_from_schema(self) -> None:
        loop = self._loop()
        self.assertEqual(
            loop._missing_required_args_from_schema("search_zhihu", {"query": "x"}),
            ["mode"],
        )

    def test_present_required_arg_passes(self) -> None:
        loop = self._loop()
        self.assertEqual(
            loop._missing_required_args_from_schema(
                "search_zhihu", {"mode": "search", "query": "x"}
            ),
            [],
        )

    def test_blank_required_arg_counts_as_missing(self) -> None:
        loop = self._loop()
        for args in ({"mode": ""}, {"mode": "   "}, {"mode": None}, {}):
            with self.subTest(repr(args)):
                self.assertEqual(
                    loop._missing_required_args_from_schema("search_zhihu", args), ["mode"]
                )

    def test_tool_without_required_list_is_always_ok(self) -> None:
        loop = self._loop()
        self.assertEqual(loop._missing_required_args_from_schema("no_required", {}), [])

    def test_unknown_tool_does_not_raise(self) -> None:
        loop = self._loop()
        self.assertEqual(loop._missing_required_args_from_schema("nope", {}), [])

    def test_pending_retry_dropped_when_required_arg_missing(self) -> None:
        loop = self._loop()
        ctx = _Ctx(("search_zhihu", {"query": "rust 值不值得学"}), ["search_zhihu"])
        self.assertIsNone(loop._consume_navigator_pending_tool_retry(ctx))

    def test_pending_retry_kept_when_args_complete(self) -> None:
        loop = self._loop()
        ctx = _Ctx(("search_zhihu", {"mode": "search", "query": "x"}), ["search_zhihu"])
        got = loop._consume_navigator_pending_tool_retry(ctx)
        self.assertEqual(got, ("search_zhihu", {"mode": "search", "query": "x"}))

    def test_pending_retry_is_consumed_exactly_once(self) -> None:
        loop = self._loop()
        ctx = _Ctx(("search_zhihu", {"mode": "search"}), ["search_zhihu"])
        self.assertIsNotNone(loop._consume_navigator_pending_tool_retry(ctx))
        self.assertIsNone(loop._consume_navigator_pending_tool_retry(ctx))

    def test_invisible_tool_still_rejected(self) -> None:
        loop = self._loop()
        ctx = _Ctx(("search_zhihu", {"mode": "search"}), [])
        self.assertIsNone(loop._consume_navigator_pending_tool_retry(ctx))


if __name__ == "__main__":
    unittest.main()
