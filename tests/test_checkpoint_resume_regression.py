"""E6a：checkpoint 自动恢复回归测试（Phase 5：超时重试续跑）。

锁四件事：
1. AgentLoop.run(resume_checkpoint=载荷) 从快照续跑：不再重跑已完成的工具步骤，
   且请求上下文里带着恢复出来的工具结果。
2. resume_checkpoint_id（存储内 trace_id）同样可续跑，续跑回合自己也会落新快照。
3. 无 checkpoint / 无 resume 参数时从零开始，行为与旧版一致。
4. queue 超时取消的 dispatch 结果携带 resume_token（= 该次尝试的 trace_id），
   正常完成不携带。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timezone
from pathlib import Path

from core.agent import AgentContext, AgentLoop, AgentResult
from core.agent_checkpoint import AgentTurnCheckpoint
from core.agent_tools import ToolCallResult
from core.queue import GroupQueueDispatcher, QueueDispatchResult


class _StubRegistry:
    """最小工具注册表 stub，记录每次工具调用。"""

    tool_count = 3

    def __init__(self, names: set[str] | None = None):
        self._names = names or {"web_search", "final_answer", "think"}
        self.calls: list[tuple[str, dict]] = []

    def has_tool(self, name: str) -> bool:
        return name in self._names

    def get_schema(self, name: str):
        return None

    def select_tools_for_intent(self, message_text: str, perm_level: str) -> list[str]:
        _ = (message_text, perm_level)
        return list(self._names)

    def get_schemas_for_prompt_filtered(self, selected_tools: list[str]) -> str:
        return "\n".join(f"- {n}" for n in selected_tools)

    def get_prompt_hints_text(self, section: str, tool_names: list[str] | None = None) -> str:
        _ = (section, tool_names)
        return ""

    def list_tools_for_permission(self, permission_level: str = "user") -> list[str]:
        _ = permission_level
        return list(self._names)

    def get_schemas_for_native_tools(self, tool_names: list[str]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": n,
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for n in tool_names
        ]

    def get_dynamic_context(self, payload: dict, tool_names: list[str] | None = None) -> str:
        _ = (payload, tool_names)
        return ""

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        return ToolCallResult(ok=True, data={}, display=f"{name} 执行完成")


class _CapturingModelClient:
    """按顺序返回预设 tool_call JSON 的模型 stub，并记录每次请求的 messages。"""

    enabled = True

    def __init__(self, responses: list[str], native_tools: bool = True):
        self._responses = list(responses)
        self._native_tools = native_tools
        self.requests: list[list[dict]] = []

    def supports_native_tool_calling(self) -> bool:
        return self._native_tools

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0):
        raise AssertionError("chat_text_with_retry 不应在本测试中被调用（native_tools=True）")

    async def chat_completion_with_retry(self, messages, max_tokens=0, tools=None, retries=0, backoff=0.0):
        _ = (max_tokens, tools, retries, backoff)
        self.requests.append([dict(m) for m in messages])
        if not self._responses:
            raise AssertionError("模型响应已耗尽 —— 若恢复失败，续跑会在这里暴露")
        resp = self._responses.pop(0)
        parsed = json.loads(resp)
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_123",
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


def _make_ctx(trace_id: str = "resume-test") -> AgentContext:
    return AgentContext(
        conversation_id="group:1:user:2",
        user_id="2",
        user_name="tester",
        group_id=1,
        bot_id="bot",
        is_private=False,
        mentioned=True,
        message_text="你好",
        trace_id=trace_id,
    )


def _make_loop(
    client: _CapturingModelClient,
    registry: _StubRegistry,
    checkpoint_dir: Path | None = None,
) -> AgentLoop:
    loop = AgentLoop(
        model_client=client,
        tool_registry=registry,
        config={
            "agent": {
                "enable": True,
                "max_steps": 8,
                "fallback_on_parse_error": True,
            },
            "admin": {"super_users": ["10001"]},
            "queue": {"process_timeout_seconds": 120},
        },
        checkpoint_dir=checkpoint_dir,
    )
    loop.high_risk_control_enable = False
    loop._build_system_prompt = lambda ctx: "system prompt"
    loop._build_user_message = lambda ctx: ctx.message_text
    return loop


def _full_run_responses() -> list[str]:
    """一步 web_search + final_answer。同一回合第二次 web_search 会被
    duplicate_external_fact_query 守卫拦截（按 query 去重），所以每回合只放一步。"""
    return [
        json.dumps({"tool": "web_search", "args": {"q": "第一轮"}}),
        json.dumps({"tool": "final_answer", "args": {"text": "完成"}}),
    ]


class CheckpointResumeRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_from_explicit_checkpoint_skips_completed_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_dir = Path(tmp)
            registry1 = _StubRegistry()
            client1 = _CapturingModelClient(_full_run_responses())
            loop1 = _make_loop(client1, registry1, checkpoint_dir=checkpoint_dir)
            ctx1 = _make_ctx()
            result1: AgentResult = await loop1.run(ctx1)

            self.assertEqual(result1.reply_text, "完成")
            self.assertEqual([c[0] for c in registry1.calls], ["web_search"])
            self.assertEqual(len(client1.requests), 2)

            # 超时带出：从存储读本回合 checkpoint（已完成的一步已落盘）
            payload = AgentTurnCheckpoint(checkpoint_dir).load(ctx1.trace_id)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["step_idx"], 0)
            self.assertEqual([s["tool"] for s in payload["steps"]], ["web_search"])

            # 续跑：只准备一条 final_answer —— 若从头重跑，第一步就会耗尽响应而报错
            registry2 = _StubRegistry()
            client2 = _CapturingModelClient(
                [json.dumps({"tool": "final_answer", "args": {"text": "完成"}})]
            )
            loop2 = _make_loop(client2, registry2, checkpoint_dir=checkpoint_dir)
            result2: AgentResult = await loop2.run(
                _make_ctx(trace_id="resume-test-2"), resume_checkpoint=payload
            )

            self.assertEqual(result2.reply_text, "完成")
            # 不再重跑已完成步骤：零工具调用、只问了一次模型
            self.assertEqual(registry2.calls, [])
            self.assertEqual(len(client2.requests), 1)
            # 请求上下文带着恢复出的工具结果，而不是从零开始
            joined = json.dumps(client2.requests[0], ensure_ascii=False)
            self.assertIn("web_search 执行完成", joined)
            # 结果里的 steps 是恢复的历史 + 本轮 final_answer
            self.assertEqual([s["tool"] for s in result2.steps], ["web_search", "final_answer"])

    async def test_resume_via_checkpoint_id_loads_from_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_dir = Path(tmp)
            registry1 = _StubRegistry()
            loop1 = _make_loop(
                _CapturingModelClient(_full_run_responses()),
                registry1,
                checkpoint_dir=checkpoint_dir,
            )
            ctx1 = _make_ctx(trace_id="resume-token-test")
            await loop1.run(ctx1)
            self.assertEqual(len(registry1.calls), 1)

            # 用 checkpoint_id（trace_id）续跑：走存储加载路径。
            # 续跑回合从 step 1 继续，模型先再执行一步新工具再收尾。
            registry2 = _StubRegistry()
            client2 = _CapturingModelClient(
                [
                    json.dumps({"tool": "web_search", "args": {"q": "第二轮"}}),
                    json.dumps({"tool": "final_answer", "args": {"text": "完成"}}),
                ]
            )
            loop2 = _make_loop(client2, registry2, checkpoint_dir=checkpoint_dir)
            result2: AgentResult = await loop2.run(
                _make_ctx(trace_id="resume-token-test-2"),
                resume_checkpoint_id=ctx1.trace_id,
            )

            self.assertEqual(result2.reply_text, "完成")
            # 只有续跑新增的一步，没有重跑第一轮的 web_search
            self.assertEqual([c[0] for c in registry2.calls], ["web_search"])
            self.assertEqual(len(client2.requests), 2)
            # 续跑的第一个请求带着恢复出的 step 0 结果
            self.assertIn(
                "web_search 执行完成",
                json.dumps(client2.requests[0], ensure_ascii=False),
            )
            # 续跑回合自己也会落新 trace_id 的快照，供下一次超时续跑
            new_payload = AgentTurnCheckpoint(checkpoint_dir).load("resume-token-test-2")
            self.assertIsNotNone(new_payload)
            self.assertEqual(new_payload["step_idx"], 1)
            self.assertEqual(
                [s["tool"] for s in new_payload["steps"]],
                ["web_search", "web_search"],
            )

    async def test_resume_checkpoint_id_missing_falls_back_to_fresh_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = _StubRegistry()
            client = _CapturingModelClient(_full_run_responses())
            loop = _make_loop(client, registry, checkpoint_dir=Path(tmp))
            # 存储里没有这个 id，也没有同 trace_id 文件 → 从零开始，不报错
            result: AgentResult = await loop.run(
                _make_ctx(trace_id="no-such-checkpoint"), resume_checkpoint_id="ghost"
            )
            self.assertEqual(result.reply_text, "完成")
            self.assertEqual([c[0] for c in registry.calls], ["web_search"])
            self.assertEqual(len(client.requests), 2)

    async def test_without_checkpoint_starts_fresh(self) -> None:
        registry = _StubRegistry()
        client = _CapturingModelClient(_full_run_responses())
        # 未配置 checkpoint_dir，也不传 resume 参数 → 旧行为
        loop = _make_loop(client, registry)
        result: AgentResult = await loop.run(_make_ctx(trace_id="fresh"))
        self.assertEqual(result.reply_text, "完成")
        self.assertEqual([c[0] for c in registry.calls], ["web_search"])
        self.assertEqual(len(client.requests), 2)
        self.assertEqual([s["tool"] for s in result.steps], ["web_search", "final_answer"])

    async def test_fallback_result_carries_resume_checkpoint_id(self) -> None:
        # 兜底结果带出 checkpoint_id：模拟 agent 侧先于 queue 超时的场景。
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_dir = Path(tmp)
            registry = _StubRegistry()
            # 只给一步工具调用就耗尽响应 → 触发 max_steps 兜底（走了 checkpoint 保存）
            client = _CapturingModelClient(
                [json.dumps({"tool": "web_search", "args": {"q": "第一轮"}})]
            )
            loop = _make_loop(client, registry, checkpoint_dir=checkpoint_dir)
            result: AgentResult = await loop.run(_make_ctx(trace_id="fallback-token"))
            self.assertTrue(result.reason.startswith("agent_fallback_"))
            self.assertEqual(result.resume_checkpoint_id, "fallback-token")
            # 无 checkpoint 落盘的回合（比如一开始就空转）不携带凭据
            bare_registry = _StubRegistry()
            bare_client = _CapturingModelClient([])
            bare_loop = _make_loop(bare_client, bare_registry)  # 无 checkpoint_dir
            bare_result: AgentResult = await bare_loop.run(_make_ctx(trace_id="bare"))
            self.assertEqual(bare_result.resume_checkpoint_id, "")


class QueueResumeTokenTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for(self, outcomes: list[QueueDispatchResult]) -> None:
        for _ in range(100):
            if outcomes:
                return
            await asyncio.sleep(0.05)
        self.fail("dispatch 未在超时内完成")

    async def test_timeout_dispatch_carries_resume_token(self) -> None:
        dispatcher = GroupQueueDispatcher(
            {"process_timeout_seconds": 1, "cancel_previous_on_new": False}
        )
        outcomes: list[QueueDispatchResult] = []

        async def on_complete(dispatch: QueueDispatchResult) -> None:
            outcomes.append(dispatch)

        async def process() -> str:
            await asyncio.sleep(60)
            return "late"

        async def send(value: str) -> None:
            _ = value

        now = datetime.now(UTC)
        await dispatcher.submit(
            "conversation:timeout",
            dispatcher.next_seq("conversation:timeout"),
            now,
            process=process,
            send=send,
            on_complete=on_complete,
            trace_id="timeout-trace-1",
        )
        await self._wait_for(outcomes)

        dispatch = outcomes[0]
        self.assertEqual(dispatch.status, "cancelled")
        self.assertEqual(dispatch.reason, "process_timeout")
        # 恢复凭据 = 该次尝试的 trace_id（checkpoint 以它为文件名）
        self.assertEqual(dispatch.resume_token, "timeout-trace-1")

    async def test_normal_finish_has_no_resume_token(self) -> None:
        dispatcher = GroupQueueDispatcher({"process_timeout_seconds": 5})
        outcomes: list[QueueDispatchResult] = []
        sent: list[str] = []

        async def on_complete(dispatch: QueueDispatchResult) -> None:
            outcomes.append(dispatch)

        async def process() -> str:
            return "ok"

        async def send(value: str) -> None:
            sent.append(value)

        now = datetime.now(UTC)
        await dispatcher.submit(
            "conversation:ok",
            dispatcher.next_seq("conversation:ok"),
            now,
            process=process,
            send=send,
            on_complete=on_complete,
            trace_id="ok-trace",
        )
        await self._wait_for(outcomes)

        dispatch = outcomes[0]
        self.assertEqual(dispatch.status, "finished")
        self.assertEqual(dispatch.reason, "ok")
        self.assertEqual(sent, ["ok"])
        self.assertEqual(dispatch.resume_token, "")


if __name__ == "__main__":
    unittest.main()
