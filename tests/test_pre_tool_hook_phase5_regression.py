"""Phase 5a：Claude Code 式 PreToolUse 审批钩子回归测试。

锁四件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.5 第 4 条）：
1. register_pre_tool_hook 注册审批钩子（可插拔，非 callable 忽略）。
2. 钩子返回非空字符串 = 阻止该工具并回喂。
3. 多个钩子按注册顺序执行，第一个非空阻止。
4. 异常钩子被跳过并继续，不影响后续钩子。
"""
from __future__ import annotations

import unittest

from core.agent import AgentLoop


class PreToolHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = AgentLoop.__new__(AgentLoop)
        self.loop._pre_tool_hooks = []

    def test_register_non_callable_ignored(self) -> None:
        self.loop.register_pre_tool_hook("not callable")
        self.assertEqual(self.loop._pre_tool_hooks, [])

    def test_allow_returns_empty(self) -> None:
        self.loop.register_pre_tool_hook(lambda ctx, name, args: "")
        self.assertEqual(self.loop._run_pre_tool_hooks(None, "search", {}), "")

    def test_deny_returns_message(self) -> None:
        self.loop.register_pre_tool_hook(lambda ctx, name, args: "禁止执行")
        self.assertEqual(self.loop._run_pre_tool_hooks(None, "search", {}), "禁止执行")

    def test_first_nonempty_hook_wins(self) -> None:
        calls: list[str] = []

        def h1(ctx, name, args):  # type: ignore[no-untyped-def]
            calls.append("h1")
            return ""

        def h2(ctx, name, args):  # type: ignore[no-untyped-def]
            calls.append("h2")
            return "blocked by h2"

        def h3(ctx, name, args):  # type: ignore[no-untyped-def]
            calls.append("h3")
            return ""

        self.loop.register_pre_tool_hook(h1)
        self.loop.register_pre_tool_hook(h2)
        self.loop.register_pre_tool_hook(h3)
        self.assertEqual(self.loop._run_pre_tool_hooks(None, "x", {}), "blocked by h2")
        self.assertEqual(calls, ["h1", "h2"])

    def test_exception_hook_is_skipped(self) -> None:
        def bad(ctx, name, args):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        def good(ctx, name, args):  # type: ignore[no-untyped-def]
            return "good block"

        self.loop.register_pre_tool_hook(bad)
        self.loop.register_pre_tool_hook(good)
        self.assertEqual(self.loop._run_pre_tool_hooks(None, "x", {}), "good block")


if __name__ == "__main__":
    unittest.main()
