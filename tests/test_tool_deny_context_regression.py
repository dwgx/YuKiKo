"""蓝图 §4.5 deny 即移除工具：会话级 deny 工具集回归测试。

锁五件事（全部落在真实调用上）：
1. 工具连续失败 ≥3 次（第 3 次尝试被 consecutive_crashes_guard 拦）→ 自动 deny，
   从该会话可见工具集消失。
2. 模型每步收到的原生 tools schema 不再含被 deny 工具（本回合中途立刻生效）。
3. loop_guard critical 熔断同样触发 deny。
4. deny 记录带时间戳，超过 TTL（30 分钟）惰性过期恢复；deny 只影响指定会话，
   final_answer/think 恒可见（权限门单次行为不变）。
5. agent.tool_deny_enable=False 时整机制不生效。
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest

from core.agent import AgentContext, AgentLoop
from core.agent_tools import ToolCallResult
from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_types import ToolSchema

CONVERSATION_ID = "group:1:user:2"


# ---------------------------------------------------------------------------
# 测试桩
# ---------------------------------------------------------------------------


class _DenyRegistry(AgentToolRegistry):
    """真实 AgentToolRegistry + search 工具（可配置成功/失败）。"""

    def __init__(self, search_ok: bool = False) -> None:
        super().__init__()
        self.search_calls = 0

        async def _fail(args: dict, context: dict) -> ToolCallResult:
            _ = (args, context)
            return ToolCallResult(ok=False, display="search 失败: 模拟错误", error="mock_boom")

        async def _ok(args: dict, context: dict) -> ToolCallResult:
            _ = (args, context)
            return ToolCallResult(ok=True, display="search 完成")

        async def _search(args: dict, context: dict) -> ToolCallResult:
            _ = context
            self.search_calls += 1
            if search_ok:
                return await _ok(args, context)
            return await _fail(args, context)

        self.register(
            ToolSchema(
                name="search",
                description="搜索互联网",
                parameters={
                    "properties": {"query": {"type": "string", "description": "关键词"}},
                    "required": ["query"],
                },
                category="search",
            ),
            _search,
        )
        for name, desc in (("final_answer", "最终回复"), ("think", "思考")):
            self.register(
                ToolSchema(
                    name=name,
                    description=desc,
                    parameters={"properties": {}, "required": []},
                    category="general",
                ),
                _ok,
            )


class _RecordingModelClient:
    """按顺序吐预设响应，并记录每次 LLM 调用收到的 tools schema 与 messages。"""

    enabled = True

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.tools_seen: list[list[str]] = []
        self.messages_seen: list[list[dict]] = []

    def supports_native_tool_calling(self) -> bool:
        return True

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        raise AssertionError("原生 tools 路径不应走 chat_text_with_retry")

    async def chat_completion_with_retry(self, messages, max_tokens=0, tools=None, retries=0, backoff=0.0):
        _ = (max_tokens, retries, backoff)
        self.tools_seen.append([t["function"]["name"] for t in (tools or [])])
        self.messages_seen.append(list(messages))
        resp = self._responses.pop(0)
        try:
            parsed = json.loads(resp)
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_x",
                                    "type": "function",
                                    "function": {
                                        "name": parsed.get("tool", "unknown"),
                                        "arguments": json.dumps(parsed.get("args", {})),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        except ValueError:
            return {"choices": [{"message": {"role": "assistant", "content": resp}}]}


def _make_ctx(**overrides) -> AgentContext:
    base = AgentContext(
        conversation_id=CONVERSATION_ID,
        user_id="2",
        user_name="tester",
        group_id=1,
        bot_id="bot",
        is_private=False,
        mentioned=True,
        message_text="搜索一下",
        trace_id="deny-test",
    )
    # 真实 _build_system_prompt 被桩掉后，native_tools 由测试预置，
    # 让每步 schema 重建路径（蓝图 §4.5 的过滤点）真实生效。
    base.native_tools = ["search", "final_answer", "think", "navigate_section"]
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _make_loop(
    responses: list[str],
    registry: _DenyRegistry,
    *,
    deny_enable: bool = True,
    repeat_tool_guard_enable: bool = True,
) -> AgentLoop:
    loop = AgentLoop(
        model_client=_RecordingModelClient(responses),
        tool_registry=registry,
        config={
            "agent": {
                "enable": True,
                "max_steps": 8,
                "fallback_on_parse_error": True,
                "tool_deny_enable": deny_enable,
            },
            "admin": {"super_users": ["10001"]},
            "queue": {"process_timeout_seconds": 120},
        },
    )
    loop.high_risk_control_enable = False
    loop.repeat_tool_guard_enable = repeat_tool_guard_enable
    loop._build_system_prompt = lambda ctx: "system prompt"
    loop._build_user_message = lambda ctx: ctx.message_text
    return loop


# ===========================================================================
# registry：deny 存储 / 过滤 / 过期 / 隔离
# ===========================================================================


class RegistryDenyStorageTests(unittest.TestCase):
    """会话级 deny 集：登记、查询、清理、TTL 惰性过期。"""

    def test_deny_and_query_roundtrip(self) -> None:
        reg = AgentToolRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        self.assertEqual(reg._denied_tools_for(CONVERSATION_ID), {"search"})

    def test_deny_is_scoped_to_one_conversation(self) -> None:
        reg = AgentToolRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        self.assertEqual(reg._denied_tools_for("group:9"), set())
        self.assertEqual(reg._denied_tools_for(""), set())

    def test_clear_denied_tools_removes_all(self) -> None:
        reg = AgentToolRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        reg.deny_tools_for(CONVERSATION_ID, "web_search")
        reg.clear_denied_tools(CONVERSATION_ID)
        self.assertEqual(reg._denied_tools_for(CONVERSATION_ID), set())
        self.assertNotIn(CONVERSATION_ID, reg._denied_tools)

    def test_expired_deny_is_lazily_cleaned(self) -> None:
        reg = AgentToolRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        # 把时间戳拨到 TTL 之前 → 查询时惰性过期，会话记录一并清理
        reg._denied_tools[CONVERSATION_ID]["search"] = time.time() - reg.TOOL_DENY_TTL_SECONDS - 1
        self.assertEqual(reg._denied_tools_for(CONVERSATION_ID), set())
        self.assertNotIn(CONVERSATION_ID, reg._denied_tools)

    def test_fresh_deny_survives_and_mixed_expiry_only_drops_stale(self) -> None:
        reg = AgentToolRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        reg.deny_tools_for(CONVERSATION_ID, "web_search")
        reg._denied_tools[CONVERSATION_ID]["search"] = time.time() - reg.TOOL_DENY_TTL_SECONDS - 1
        self.assertEqual(reg._denied_tools_for(CONVERSATION_ID), {"web_search"})


class RegistryDenyFilteringTests(unittest.TestCase):
    """deny 工具从权限可见集 / schema 列表剔除。"""

    def test_denied_tool_hidden_from_permission_listing(self) -> None:
        reg = _DenyRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        tools = reg.list_tools_for_permission("user", conversation_id=CONVERSATION_ID)
        self.assertNotIn("search", tools)

    def test_without_conversation_id_no_filtering(self) -> None:
        reg = _DenyRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        tools = reg.list_tools_for_permission("user")
        self.assertIn("search", tools)

    def test_other_conversation_unaffected(self) -> None:
        reg = _DenyRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        tools = reg.list_tools_for_permission("user", conversation_id="group:9")
        self.assertIn("search", tools)

    def test_legacy_select_tools_for_intent_honors_deny(self) -> None:
        reg = _DenyRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        tools = reg.select_tools_for_intent(message_text="", permission_level="user", conversation_id=CONVERSATION_ID)
        self.assertNotIn("search", tools)

    def test_filter_tools_denied_strips_list(self) -> None:
        reg = _DenyRegistry()
        reg.deny_tools_for(CONVERSATION_ID, "search")
        filtered = reg.filter_tools_denied(["search", "final_answer", "think"], CONVERSATION_ID)
        self.assertEqual(filtered, ["final_answer", "think"])

    def test_always_include_survives_deny(self) -> None:
        # 即使被 deny，final_answer/think 这类终结/思考工具也必须可见
        reg = _DenyRegistry()
        for name in ("search", "final_answer", "think"):
            reg.deny_tools_for(CONVERSATION_ID, name)
        tools = reg.list_tools_for_permission("user", conversation_id=CONVERSATION_ID)
        self.assertIn("final_answer", tools)
        self.assertIn("think", tools)
        self.assertNotIn("search", tools)


# ===========================================================================
# agent：deny 触发与可见集过滤
# ===========================================================================


class AgentDenyTriggerTests(unittest.TestCase):
    """_deny_tool_for_conversation / _filter_denied_tools 的开关与兼容性。"""

    def _loop(self, deny_enable: bool = True) -> AgentLoop:
        loop = AgentLoop.__new__(AgentLoop)
        loop.tool_deny_enable = deny_enable
        loop.tool_registry = AgentToolRegistry()
        return loop

    def test_deny_tool_registers_for_conversation(self) -> None:
        loop = self._loop()
        self.assertTrue(loop._deny_tool_for_conversation(_make_ctx(), "search"))
        self.assertEqual(loop.tool_registry._denied_tools_for(CONVERSATION_ID), {"search"})

    def test_deny_disabled_is_noop(self) -> None:
        loop = self._loop(deny_enable=False)
        self.assertFalse(loop._deny_tool_for_conversation(_make_ctx(), "search"))
        self.assertEqual(loop.tool_registry._denied_tools_for(CONVERSATION_ID), set())

    def test_deny_tolerates_registry_without_deny_support(self) -> None:
        # 旧 registry stub（无 deny_tools_for）不应让 agent 循环崩掉
        loop = AgentLoop.__new__(AgentLoop)
        loop.tool_deny_enable = True
        loop.tool_registry = object()
        self.assertFalse(loop._deny_tool_for_conversation(_make_ctx(), "search"))

    def test_filter_denied_tools_strips_when_enabled(self) -> None:
        loop = self._loop()
        loop.tool_registry.deny_tools_for(CONVERSATION_ID, "search")
        filtered = loop._filter_denied_tools(_make_ctx(), ["search", "final_answer", "think"])
        self.assertEqual(filtered, ["final_answer", "think"])

    def test_filter_denied_tools_disabled_is_passthrough(self) -> None:
        loop = self._loop(deny_enable=False)
        loop.tool_registry.deny_tools_for(CONVERSATION_ID, "search")
        filtered = loop._filter_denied_tools(_make_ctx(), ["search", "final_answer", "think"])
        self.assertEqual(filtered, ["search", "final_answer", "think"])

    def test_filter_denied_tools_tolerates_registry_without_filter(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop.tool_deny_enable = True
        loop.tool_registry = object()
        filtered = loop._filter_denied_tools(_make_ctx(), ["search"])
        self.assertEqual(filtered, ["search"])

    def test_deny_notice_appended_to_guard_display(self) -> None:
        payload = {"tool": "search", "ok": False, "display": "该工具已连续崩溃或报错。"}
        AgentLoop._append_tool_deny_notice(payload)
        self.assertIn(AgentLoop._TOOL_DENY_NOTICE, payload["display"])
        self.assertIn("该工具已连续崩溃或报错。", payload["display"])


# ===========================================================================
# agent：端到端（真实 run() 循环）
# ===========================================================================


class AgentDenyEndToEndTests(unittest.TestCase):
    """连续失败 / loop critical 后 deny，模型每步收到的 schema 不再含该工具。"""

    def test_consecutive_failures_deny_tool_and_hide_schema(self) -> None:
        """search 连续失败，第 3 次尝试被拦并 deny → 后续 LLM 调用不再收到其 schema。"""
        registry = _DenyRegistry()
        loop = _make_loop(
            [
                '{"tool":"search","args":{"query":"a"}}',
                '{"tool":"search","args":{"query":"b"}}',
                '{"tool":"search","args":{"query":"c"}}',
                '{"tool":"final_answer","args":{"text":"没搜到"}}',
            ],
            registry,
        )
        result = asyncio.run(loop.run(_make_ctx()))

        self.assertEqual(result.action, "reply")
        self.assertEqual(registry.search_calls, 2, "第 3 次尝试应在执行前被守卫拦下")
        # deny 生效：工具从会话可见集消失
        self.assertEqual(registry._denied_tools_for(CONVERSATION_ID), {"search"})
        self.assertNotIn("search", registry.list_tools_for_permission("user", conversation_id=CONVERSATION_ID))
        # 模型消息可见集：第 3 次尝试（守卫触发）当轮的 schema 先于 deny 计算，
        # 从下一轮 LLM 调用起 search 必须消失，且之后持续消失。
        self.assertIn("search", loop.model_client.tools_seen[0])
        first_hidden = next(
            (i for i, seen in enumerate(loop.model_client.tools_seen) if "search" not in seen),
            None,
        )
        self.assertIsNotNone(first_hidden, "deny 后模型可见集应移除 search")
        self.assertEqual(first_hidden, 3, "第 3 次失败后的下一次调用即应消失")
        for seen in loop.model_client.tools_seen[first_hidden:]:
            self.assertNotIn("search", seen, f"deny 后模型仍收到 search schema: {seen}")
        # 回喂提示：deny 后模型收到「本会话已禁用」说明
        last_messages = loop.model_client.messages_seen[-1]
        self.assertTrue(
            any(AgentLoop._TOOL_DENY_NOTICE in json.dumps(m, ensure_ascii=False) for m in last_messages),
            "deny 后应回喂「本会话已禁用」提示",
        )

    def test_loop_guard_critical_denies_tool(self) -> None:
        """同参数同结果连续空转达到 loop critical → deny，schema 消失。"""
        registry = _DenyRegistry(search_ok=True)
        loop = _make_loop(
            ['{"tool":"search","args":{"query":"x"}}'] * 5 + ['{"tool":"final_answer","args":{"text":"完成"}}'],
            registry,
            repeat_tool_guard_enable=False,  # 让 loop_guard 先于重复调用守卫触发
        )
        result = asyncio.run(loop.run(_make_ctx()))

        self.assertEqual(result.action, "reply")
        self.assertEqual(registry._denied_tools_for(CONVERSATION_ID), {"search"})
        self.assertIn("search", loop.model_client.tools_seen[0])
        # 第 5 次调用（streak=4 → critical）触发 deny，其后的调用不再收到 search。
        first_hidden = next(
            (i for i, seen in enumerate(loop.model_client.tools_seen) if "search" not in seen),
            None,
        )
        self.assertEqual(first_hidden, 5, "loop critical 后下一次调用即应消失")
        for seen in loop.model_client.tools_seen[first_hidden:]:
            self.assertNotIn("search", seen, f"loop deny 后模型仍收到 search schema: {seen}")

    def test_deny_disabled_keeps_tool_visible(self) -> None:
        """agent.tool_deny_enable=False：连续失败不 deny，schema 始终可见。"""
        registry = _DenyRegistry()
        loop = _make_loop(
            [
                '{"tool":"search","args":{"query":"a"}}',
                '{"tool":"search","args":{"query":"b"}}',
                '{"tool":"search","args":{"query":"c"}}',
                '{"tool":"final_answer","args":{"text":"没搜到"}}',
            ],
            registry,
            deny_enable=False,
        )
        result = asyncio.run(loop.run(_make_ctx()))

        self.assertEqual(result.action, "reply")
        self.assertEqual(registry._denied_tools_for(CONVERSATION_ID), set())
        self.assertIn("search", loop.model_client.tools_seen[-1])
        # 单次调用仍被守卫拦下（deny 只是额外的上下文过滤，不改权限门行为）
        self.assertEqual(registry.search_calls, 2)

    def test_expired_deny_recovers_visibility(self) -> None:
        """TTL 过期后工具恢复可见（下一回合可再被模型看到）。"""
        registry = _DenyRegistry()
        registry.deny_tools_for(CONVERSATION_ID, "search")
        registry._denied_tools[CONVERSATION_ID]["search"] = time.time() - registry.TOOL_DENY_TTL_SECONDS - 1
        loop = _make_loop([], registry)
        filtered = loop._filter_denied_tools(_make_ctx(), ["search", "final_answer"])
        self.assertEqual(filtered, ["search", "final_answer"])
        self.assertIn("search", registry.list_tools_for_permission("user", conversation_id=CONVERSATION_ID))


if __name__ == "__main__":
    unittest.main()
