"""重复工具调用死循环回归测试。

线上实测：模型拿到工具结果后仍连续调同一个工具，args 只在中英文/全角半角之间
抖动，最后以 agent_fallback_repeated_tool_call 收场（61 个 preflight 成功回合里
占 13 个）。根因不是「结果没进 messages」——结果进了——而是：

- 守卫拦截时只回喂一句「禁止再次调用」，既不带上一次已拿到的产物，
  也不重述上一次失败的真实原因，模型无据可依只能重试；
- 副作用工具（发表情、记事实）的重复阈值是 3，意味着真的执行三次；
- 全角/半角一个字符就能绕过 (tool, args) 重复判定；
- 熔断那一步还会 append 一条模型永远看不到的 tool 消息。

这些测试锁住修复后的行为契约。
"""
from __future__ import annotations

import asyncio
import json
import unittest

from core.agent_tools import ToolCallResult
from tests.test_agent_smoke import (
    _make_ctx,
    _make_loop,
    _SequencedModelClient,
    _StubRegistry,
)


class _ScriptedRegistry(_StubRegistry):
    """记录真实执行次数、可指定成功/失败的工具注册表。"""

    def __init__(
        self,
        names: set[str],
        *,
        ok: bool = True,
        data: dict | None = None,
        display: str = "工具执行完成",
        error: str = "",
    ):
        super().__init__(names)
        self.calls: list[tuple[str, dict]] = []
        self._ok = ok
        self._data = data or {}
        self._display = display
        self._error = error

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        return ToolCallResult(
            ok=self._ok,
            data=dict(self._data),
            display=self._display,
            error=self._error,
        )

    def executions_of(self, tool_name: str) -> int:
        return sum(1 for name, _ in self.calls if name == tool_name)


class _MessageRecordingClient(_SequencedModelClient):
    """持有 run() 内部那个 messages 列表本体，便于检查回喂内容。

    AgentLoop 把同一个 list 传给每次模型调用，所以抓住第一次的引用就能看到
    循环结束时（含最后一次 append）的完整历史。
    """

    def __init__(self, responses: list[str]):
        super().__init__(responses)
        self.live_messages: list[dict] | None = None

    async def chat_completion_with_retry(self, messages, **kwargs):
        if self.live_messages is None:
            self.live_messages = messages
        return await super().chat_completion_with_retry(messages, **kwargs)

    def tool_results(self) -> list[dict]:
        """按顺序返回回喂给模型的每个 tool_result dict。"""
        out: list[dict] = []
        for msg in self.live_messages or []:
            if msg.get("role") != "tool":
                continue
            try:
                payload = json.loads(msg.get("content", "{}"))
            except json.JSONDecodeError:
                continue
            result = payload.get("tool_result")
            if isinstance(result, dict):
                out.append(result)
        return out


def _run(responses: list[str], registry: _ScriptedRegistry, *, max_same: int = 2):
    """跑一轮 AgentLoop，返回 (result, registry, client)。"""
    loop = _make_loop(responses, registry=registry)
    client = _MessageRecordingClient(responses)
    loop.model_client = client
    loop.max_same_tool_call = max_same
    result = asyncio.run(loop.run(_make_ctx(message_text="帮我找个猫咪视频")))
    return result, registry, client


class RepeatGuardFeedbackTests(unittest.TestCase):
    """守卫拦截时回喂的内容必须让模型有据可依。"""

    def test_should_carry_previous_artifact_when_blocking_repeat_call(self):
        """工具已拿到图片后重复调用 → 拦截回喂里应带上那张图，而不只是一句禁令。"""
        registry = _ScriptedRegistry(
            {"search_media", "final_answer", "think"},
            data={"image_url": "https://example.com/cat.jpg"},
            display="先给你一张图",
        )
        _, _, client = _run(
            ['{"tool":"search_media","args":{"query":"cute cat photo","media_type":"image"}}'] * 8,
            registry,
        )
        blocked = [r for r in client.tool_results() if not r.get("ok")]
        self.assertTrue(blocked, "应至少有一条拦截回喂")
        obtained = blocked[0].get("already_obtained")
        self.assertIsInstance(obtained, dict, "拦截回喂应带 already_obtained")
        self.assertEqual(obtained.get("image_urls"), ["https://example.com/cat.jpg"])
        self.assertEqual(obtained.get("summary"), "先给你一张图")

    def test_should_restate_real_error_when_crash_guard_blocks(self):
        """工具连续真失败 → 熔断回喂里应重述真实错误，模型才能向用户解释。"""
        registry = _ScriptedRegistry(
            {"parse_video", "final_answer", "think"},
            ok=False,
            error="url_blocked",
            display="这个视频链接命中了安全限制",
        )
        # 每次换一个 url，避开重复守卫，逼出 consecutive_crashes_guard
        responses = [
            f'{{"tool":"parse_video","args":{{"url":"https://v.douyin.com/{suffix}/"}}}}'
            for suffix in ("a", "b", "c", "d", "e", "f")
        ]
        result, _, client = _run(responses, registry)
        guard_errors = [
            r.get("error", "") for r in client.tool_results() if not r.get("ok")
        ]
        self.assertTrue(
            any("连续崩溃" in err for err in guard_errors),
            f"应触发连续崩溃守卫，实际回喂={guard_errors}",
        )
        crash_feedback = [
            r for r in client.tool_results() if "连续崩溃" in str(r.get("error", ""))
        ]
        self.assertTrue(
            any("url_blocked" in str(r.get("display", "")) for r in crash_feedback),
            f"熔断回喂应重述真实错误 url_blocked，实际={crash_feedback}",
        )
        self.assertIn("consecutive_crashes_guard", [s.get("error") for s in result.steps])

    def test_should_not_claim_artifact_from_a_different_tool(self):
        """只有被拦工具自己成功过才算已拿到产物，别的工具的图不能冒名顶替。"""
        loop = _make_loop([])
        steps = [
            {"step": 0, "tool": "search_media", "ok": True, "display": "找到图",
             "data": {"image_url": "https://example.com/other.jpg"}},
            {"step": 1, "tool": "parse_video", "ok": False, "display": "解析失败",
             "error": "url_blocked"},
        ]
        payload = loop._build_guard_feedback_payload(
            tool_name="parse_video",
            steps=steps,
            reason_key="consecutive_crashes_guard",
            reason_text="该工具已连续崩溃或报错，底层拒绝执行，不要再调用它。",
        )
        self.assertNotIn("already_obtained", payload)
        self.assertIn("url_blocked", payload["display"])


class SideEffectRepeatTests(unittest.TestCase):
    """有对外副作用的工具，同一回合同 args 只能真执行一次。"""

    def test_should_execute_side_effect_tool_once_for_identical_args(self):
        """连发同一个表情 → 只应真发一次，不是三次。"""
        registry = _ScriptedRegistry(
            {"send_face", "final_answer", "think"}, display="已发送表情 [开心] (id=13)"
        )
        _run(
            ['{"tool":"send_face","args":{"face_id":13}}'] * 8,
            registry,
            max_same=3,
        )
        self.assertEqual(
            registry.executions_of("send_face"),
            1,
            f"send_face 应只执行一次，实际={registry.calls}",
        )

    def test_should_still_allow_side_effect_tool_with_different_args(self):
        """换一个表情 id → 不受同 args 限制，应照常执行。"""
        registry = _ScriptedRegistry(
            {"send_face", "final_answer", "think"}, display="已发送表情"
        )
        _run(
            [
                '{"tool":"send_face","args":{"face_id":13}}',
                '{"tool":"send_face","args":{"face_id":21}}',
                '{"tool":"final_answer","args":{"text":"发好了"}}',
            ],
            registry,
            max_same=3,
        )
        self.assertEqual(registry.executions_of("send_face"), 2)

    def test_should_not_bypass_repeat_guard_with_fullwidth_punctuation(self):
        """同一条事实换成全角括号 → 仍算重复，不能再入库一次。"""
        registry = _ScriptedRegistry(
            {"remember_user_fact", "final_answer", "think"}, display="已记住"
        )
        _run(
            [
                '{"tool":"remember_user_fact","args":{"fact":"用户1019(QQ:1)喜欢拿铁"}}',
                '{"tool":"remember_user_fact","args":{"fact":"用户1019（QQ:1）喜欢拿铁"}}',
                '{"tool":"remember_user_fact","args":{"fact":"用户1019（QQ:1）喜欢拿铁"}}',
                '{"tool":"remember_user_fact","args":{"fact":"用户1019（QQ:1）喜欢拿铁"}}',
            ],
            registry,
            max_same=3,
        )
        self.assertEqual(
            registry.executions_of("remember_user_fact"),
            1,
            f"同一条事实应只入库一次，实际={registry.calls}",
        )

    def test_should_treat_fullwidth_and_halfwidth_args_as_one_signature(self):
        """签名层面：全角与半角括号应归一为同一个签名。"""
        loop = _make_loop([])
        half = loop._build_args_signature({"fact": "用户1019(QQ:1)喜欢拿铁"})
        full = loop._build_args_signature({"fact": "用户1019（QQ:1）喜欢拿铁"})
        self.assertEqual(half, full)


class EscalationMessageTests(unittest.TestCase):
    """熔断返回时不应再产生模型永远看不到的 tool 消息。"""

    def test_should_not_append_tool_message_when_escalating_to_fallback(self):
        """熔断那一步只记 steps，不再 append 无人消费的回喂。"""
        registry = _ScriptedRegistry(
            {"probe_tool", "final_answer", "think"}, display="探针工具完成"
        )
        result, _, client = _run(
            ['{"tool":"probe_tool","args":{"q":"x"}}'] * 8,
            registry,
            max_same=2,
        )
        self.assertEqual(result.reason, "agent_fallback_repeated_tool_call")
        blocked_steps = [
            s for s in result.steps if str(s.get("error", "")).startswith("repeated_tool_call:")
        ]
        blocked_feedback = [
            r for r in client.tool_results() if "重复过多" in str(r.get("error", ""))
        ]
        self.assertEqual(
            len(blocked_steps),
            2,
            f"应记两条拦截步骤（第二条触发熔断），实际={blocked_steps}",
        )
        self.assertEqual(
            len(blocked_feedback),
            1,
            f"只有非熔断那一步该回喂给模型，实际回喂={blocked_feedback}",
        )


if __name__ == "__main__":
    unittest.main()
