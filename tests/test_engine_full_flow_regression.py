"""E5：handle_message 全流程 + loop_guard 接线 端到端回归测试。

覆盖缺口：
- engine.handle_message 全流程（触发→agent→响应组装）此前无集成测试，
  现有测试只到早期 return 分支（去重/白名单/trigger）。
- agent.run 的 loop_guard veto 接线此前只有 core/loop_guard.py 单元测试，
  没有「同工具同 args 连发 → critical 阻断 → guard_payload 回喂」的端到端验证。

三条全链路径：
1. 普通消息 → 模型直接文本回复（agent 路径返回 EngineResponse）。
2. 点歌消息 → 工具产物 audio_file 一路带到 EngineResponse.audio_file。
3. 工具失败 → Hermes 式错误回喂 → 模型改参自纠重试成功。
"""
from __future__ import annotations

import unittest

from core.agent import AgentResult
from core.agent_tools import ToolCallResult
from tests.conftest import make_engine, make_message


def _wrap_run_to_capture(engine, results: list[AgentResult]) -> None:
    """用真实 run() 包一层，捕获 AgentResult 供 steps 断言。"""
    real_run = engine.agent.run

    async def wrapped(ctx):
        result = await real_run(ctx)
        results.append(result)
        return result

    engine.agent.run = wrapped  # type: ignore[method-assign]


class EngineFullFlowRegressionTests(unittest.IsolatedAsyncioTestCase):
    """handle_message 全流程：触发 → agent → 响应组装。"""

    async def test_plain_message_reaches_agent_and_returns_reply(self) -> None:
        """普通消息走完整个 agent 路径：触发评估放行 → AgentLoop 产出回复 → EngineResponse。"""
        engine = make_engine(
            responses=["你好呀，我是 YuKiKo，有什么可以帮你？"],
        )
        message = make_message(text="你好", message_id="m-flow-1")

        response = await engine.handle_message(message)

        self.assertEqual(response.action, "reply")
        self.assertIn(response.reason, {"agent_direct_reply", "agent_final_answer"})
        self.assertIn("你好呀", response.reply_text)
        # 端到端证据：消息被去重缓存记录、模型被调用、无工具执行
        self.assertIn("m-flow-1", engine._seen_message_ids)
        self.assertEqual(engine._stub_registry.calls, [])

    async def test_tool_then_final_answer_flows_through_response(self) -> None:
        """工具调用 → final_answer 的完整链：steps 记录 + 回复文本 + 元信息。"""
        engine = make_engine(
            responses=[
                '{"tool":"web_search","args":{"query":"python 是什么"}}',
                '{"tool":"final_answer","args":{"text":"搜索完成，Python 是一种编程语言。"}}',
            ],
            tool_results={
                "web_search": ToolCallResult(
                    ok=True,
                    data={"summary": "Python 编程语言"},
                    display="搜索完成，共 3 条结果",
                )
            },
        )
        message = make_message(text="帮我搜下 python", message_id="m-flow-2")

        response = await engine.handle_message(message)

        self.assertEqual(response.action, "reply")
        self.assertEqual(response.reason, "agent_final_answer")
        self.assertIn("Python", response.reply_text)
        self.assertEqual(
            engine._stub_registry.calls, [("web_search", {"query": "python 是什么"})]
        )
        self.assertEqual(response.meta["agent_tool_calls"], 1)
        self.assertEqual(response.meta["agent_steps"], 2)

    async def test_music_request_carries_audio_file_product(self) -> None:
        """点歌消息：工具产物 audio_file 一路带到 EngineResponse.audio_file。"""
        engine = make_engine(
            responses=[
                '{"tool":"music_play","args":{"query":"周杰伦 晴天"}}',
                '{"tool":"final_answer","args":{"text":"晴天已经为你播放，请查收这首经典歌曲。"}}',
            ],
            tool_results={
                "music_play": ToolCallResult(
                    ok=True,
                    data={"audio_file": "file:///tmp/晴天.mp3", "title": "晴天"},
                    display="点歌成功",
                )
            },
        )
        message = make_message(text="帮我点一首周杰伦的晴天", message_id="m-flow-3")

        response = await engine.handle_message(message)

        self.assertEqual(response.action, "reply")
        self.assertEqual(response.reason, "agent_final_answer")
        # final_answer 没带 audio_file 时，从工具步骤的 compact_data 找回
        self.assertEqual(response.audio_file, "file:///tmp/晴天.mp3")
        self.assertIn("晴天", response.reply_text)

    async def test_tool_failure_relays_error_and_model_retries_with_fixed_args(self) -> None:
        """工具失败 → error 回喂（retry_instruction）→ 模型改参自纠重试成功。"""
        engine = make_engine(
            responses=[
                '{"tool":"web_search","args":{"query":"python"}}',
                '{"tool":"web_search","args":{"query":"python 教程"}}',
                '{"tool":"final_answer","args":{"text":"搜索完成，Python 教程在这里。"}}',
            ],
        )
        # 第一次调用失败，第二次（不同参数）成功
        async def flaky_call(name: str, args: dict, context: dict) -> ToolCallResult:
            _ = context
            engine._stub_registry.calls.append((name, dict(args)))
            if name == "web_search" and args.get("query") == "python":
                return ToolCallResult(
                    ok=False,
                    error="rate_limit:429",
                    display="搜索失败：请求频率过高",
                    data={},
                )
            return ToolCallResult(
                ok=True, data={"summary": "Python 教程"}, display="搜索完成"
            )

        engine._stub_registry.call = flaky_call  # type: ignore[method-assign]
        message = make_message(text="帮我搜下 python", message_id="m-flow-4")

        response = await engine.handle_message(message)

        self.assertEqual(response.reason, "agent_final_answer")
        self.assertIn("Python", response.reply_text)
        # 失败一次 + 改参重试一次
        self.assertEqual(
            engine._stub_registry.calls,
            [
                ("web_search", {"query": "python"}),
                ("web_search", {"query": "python 教程"}),
            ],
        )
        # 错误回喂确实进了模型输入：重试那一轮的 tool_result 带 retry_instruction
        retry_tool_msg = engine._stub_model_client.messages_seen[1][-1]
        self.assertEqual(retry_tool_msg["role"], "tool")
        self.assertIn("rate_limit:429", retry_tool_msg["content"])
        self.assertIn("retry_instruction", retry_tool_msg["content"])

    async def test_unwhitelisted_group_still_ignored_by_gate(self) -> None:
        """工厂不破坏早期 return 分支：未加白群 + silent 模式 → ignore。"""
        engine = make_engine()
        message = make_message(
            conversation_id="group:999:user:10086",
            group_id=999,
            message_id="m-flow-5",
            text="你好",
        )

        response = await engine.handle_message(message)

        self.assertEqual(response.action, "ignore")
        self.assertEqual(response.reason, "group_not_whitelisted")


class AgentLoopGuardWiringTests(unittest.IsolatedAsyncioTestCase):
    """loop_guard veto 接线端到端：同工具同 args 连发 → critical 阻断 + 回喂。"""

    def _loop_engine(self):
        return make_engine(
            responses=[
                '{"tool":"sum","args":{"a":1,"b":2}}',
                '{"tool":"sum","args":{"a":1,"b":2}}',
                '{"tool":"sum","args":{"a":1,"b":2}}',
                '{"tool":"sum","args":{"a":1,"b":2}}',
                '{"tool":"sum","args":{"a":1,"b":2}}',
                '{"tool":"final_answer","args":{"text":"计算完成，结果是 3，这就是你要的答案。"}}',
            ],
            tool_names={"sum", "final_answer", "think"},
            tool_results={
                "sum": ToolCallResult(
                    ok=True, data={"result": 3}, display="sum 执行完成: 3"
                )
            },
            config={"agent": {"repeat_tool_guard_enable": False}},
        )

    async def test_same_tool_same_args_escalates_to_critical_and_blocks(self) -> None:
        """连发 5 次同参数调用：前 4 次执行，第 5 次被 veto critical 拦截。"""
        engine = self._loop_engine()
        results: list[AgentResult] = []
        _wrap_run_to_capture(engine, results)
        message = make_message(text="算一下 1+2", message_id="m-loop-1")

        response = await engine.handle_message(message)

        self.assertEqual(response.reason, "agent_final_answer")
        # 模型请求了 5 次工具调用，第 5 次被 veto 阻断：真实执行只有 4 次
        self.assertEqual(engine._stub_registry.calls, [("sum", {"a": 1, "b": 2})] * 4)
        # 阻断步在 steps 里带 loop_guard 标记（error 形如 loop_guard:critical:4）
        guard_errors = [
            step.get("error")
            for step in results[0].steps
            if str(step.get("error", "")).startswith("loop_guard:")
        ]
        self.assertIn("loop_guard:critical:4", guard_errors)
        self.assertNotIn("loop_guard:circuit:", guard_errors)

    async def test_guard_payload_is_injected_back_into_model_messages(self) -> None:
        """critical 阻断后 guard_payload 回喂进模型输入（同参数空转说明）。"""
        engine = self._loop_engine()
        message = make_message(text="算一下 1+2", message_id="m-loop-2")

        await engine.handle_message(message)

        # 最后一轮模型调用（final_answer）的 messages 尾部就是 guard_payload
        last_messages = engine._stub_model_client.messages_seen[-1]
        guard_msg = last_messages[-1]
        self.assertEqual(guard_msg["role"], "tool")
        self.assertIn("loop_guard:critical:4", guard_msg["content"])
        self.assertIn("连续空转", guard_msg["content"])
        self.assertIn("不要重复相同调用", guard_msg["content"])

    async def test_warn_level_does_not_block_execution(self) -> None:
        """warn 级（streak=2）只记日志不阻断：同参数第三次调用照常执行。"""
        engine = make_engine(
            responses=[
                '{"tool":"sum","args":{"a":1,"b":2}}',
                '{"tool":"sum","args":{"a":1,"b":2}}',
                '{"tool":"sum","args":{"a":1,"b":2}}',
                '{"tool":"final_answer","args":{"text":"计算完成，结果是 3，这就是你要的答案。"}}',
            ],
            tool_names={"sum", "final_answer", "think"},
            tool_results={
                "sum": ToolCallResult(ok=True, data={"result": 3}, display="sum 执行完成: 3")
            },
            config={"agent": {"repeat_tool_guard_enable": False}},
        )
        results: list[AgentResult] = []
        _wrap_run_to_capture(engine, results)
        message = make_message(text="算一下 1+2", message_id="m-loop-3")

        response = await engine.handle_message(message)

        self.assertEqual(response.reason, "agent_final_answer")
        # 三次调用全部执行：warn 只告警不阻断
        self.assertEqual(engine._stub_registry.calls, [("sum", {"a": 1, "b": 2})] * 3)
        guard_errors = [
            step.get("error")
            for step in results[0].steps
            if str(step.get("error", "")).startswith("loop_guard:")
        ]
        self.assertEqual(guard_errors, [])


if __name__ == "__main__":
    unittest.main()
