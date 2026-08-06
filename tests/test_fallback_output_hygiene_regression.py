"""兜底路径的输出卫生回归测试。

业主原话是机器人"很机器人、人机、烦人"。最刺眼的一类是内部技术状态直接进群：

- 通道一：兜底 LLM 收到的"情况"描述是 f"{工具名}:{错误码}" 拼出来的原文
  （旧 core/agent.py `failed_tools` / `fail_hint`），而那条路的 system prompt
  硬编码在 `_ai_fallback_reply` 里，没有一句禁止复述工具名或错误码 ——
  线上表现是"analyze_image 又超时了"。
- 通道二：`_build_fallback_result` 的第二个循环把失败步骤的 display 截 280 字
  **直接当回复返回**，连 LLM 都不过。唯一的过滤器只挡英文长段，中文错误原文
  一律放行 —— 线上表现是"工具那边只拿到了文件信息"。

修完后的契约：

1. 失败类别由 `ToolCallResult.error`（本仓自己写的机器码）结构化判定，
   喂给兜底 LLM 的只有类别标签，没有工具名也没有错误码。
2. 循环自己合成的 "<tool> 失败: <error>" / "<tool> 执行超时" 永不外发。
3. 工具自己写的那句人话仍然外发（不丢真实原因），但先剥掉机器标识符。
4. 预算已耗尽的兜底原因（模型超时/报错）不再打第二次 LLM。

另外锁住两条同批修复：副作用工具的"一回合一次"数成功次数而不是调用次数；
以及那段判据恒假的表情媒体清空逻辑已删除。
"""
from __future__ import annotations

import asyncio
import unittest

from core.agent import AgentLoop
from core.agent_tools import ToolCallResult
from tests.test_agent_smoke import _make_ctx, _make_loop, _StubRegistry


class _RecordingFallbackLoop:
    """抓住 `_ai_fallback_reply` 收到的 error_hint。"""

    @staticmethod
    def attach(loop: AgentLoop, reply: str = "这个我没弄成，你换个说法我再来。") -> list[str]:
        hints: list[str] = []

        async def _fake(ctx, error_hint: str) -> str:
            _ = ctx
            hints.append(error_hint)
            return reply

        loop._ai_fallback_reply = _fake  # type: ignore[method-assign]
        return hints


def _fallback(loop: AgentLoop, steps: list[dict], reason: str = "max_steps_reached"):
    return asyncio.run(
        loop._build_fallback_result(
            _make_ctx(message_text="看看这张图"),
            steps,
            tool_calls_made=1,
            t0=0.0,
            reason=reason,
        )
    )


class FailureCategoryClassificationTests(unittest.TestCase):
    """失败类别只从 error 机器码里取，取不到就归 unknown。"""

    def test_should_map_tool_timeout_code_to_timeout_category(self):
        self.assertEqual(
            AgentLoop._classify_tool_failure("tool_timeout:analyze_image"), "timeout"
        )

    def test_should_map_permission_denied_code_to_permission_category(self):
        self.assertEqual(
            AgentLoop._classify_tool_failure("permission_denied:need_super_admin"),
            "permission",
        )

    def test_should_map_unavailable_component_code_to_unavailable_category(self):
        for code in (
            "memory_engine_unavailable",
            "crawler_unavailable",
            "video_parser_unavailable",
            "no_api_call_available",
        ):
            with self.subTest(code):
                self.assertEqual(AgentLoop._classify_tool_failure(code), "unavailable")

    def test_should_map_missing_argument_code_to_missing_args_category(self):
        self.assertEqual(
            AgentLoop._classify_tool_failure("missing_required_args:url"), "missing_args"
        )

    def test_should_map_empty_error_to_unknown_category(self):
        self.assertEqual(AgentLoop._classify_tool_failure(""), "unknown")

    def test_should_ignore_exception_prose_when_classifying(self):
        """异常文本里的自然语言不该影响类别，`_error` 后缀才是判据。"""
        self.assertEqual(
            AgentLoop._classify_tool_failure(
                "qzone_error: HTTPSConnectionPool(host='user.qzone.qq.com', port=443)"
            ),
            "upstream",
        )


class FallbackHintHygieneTests(unittest.TestCase):
    """通道一：喂给兜底 LLM 的情况描述不能带工具名或错误码。"""

    def test_should_not_pass_tool_names_or_error_codes_to_fallback_model(self):
        loop = _make_loop([])
        hints = _RecordingFallbackLoop.attach(loop)
        _fallback(
            loop,
            [
                {"step": 1, "tool": "analyze_image", "ok": False, "error": "tool_timeout:analyze_image"},
                {"step": 2, "tool": "qzone_browse", "ok": False, "error": "qzone_api_error: 500"},
            ],
        )
        self.assertEqual(len(hints), 1, f"应只调一次兜底模型，实际={hints}")
        hint = hints[0]
        for leaked in ("analyze_image", "qzone_browse", "tool_timeout", "qzone_api_error", "500"):
            self.assertNotIn(leaked, hint, f"情况描述泄漏了 {leaked}：{hint}")

    def test_should_describe_failure_by_category_label(self):
        loop = _make_loop([])
        hints = _RecordingFallbackLoop.attach(loop)
        _fallback(
            loop,
            [{"step": 1, "tool": "analyze_image", "ok": False, "error": "tool_timeout:analyze_image"}],
        )
        self.assertIn("超时", hints[0])

    def test_should_skip_second_model_call_when_budget_already_exhausted(self):
        """模型侧超时进来的兜底不该再打一次 LLM —— 那次注定二次超时。"""
        loop = _make_loop([])
        hints = _RecordingFallbackLoop.attach(loop)
        result = _fallback(
            loop,
            [{"step": 1, "tool": "analyze_image", "ok": False, "error": "tool_timeout:analyze_image"}],
            reason="llm_timeout",
        )
        self.assertEqual(hints, [], "预算耗尽时不应调用兜底模型")
        self.assertTrue(result.reply_text.strip(), "仍然要给用户一句话")
        self.assertNotIn("analyze_image", result.reply_text)

    def test_should_scrub_machine_identifiers_from_model_reply(self):
        """兜底模型万一还是吐了工具名，发出去之前要剥掉。"""
        loop = _make_loop([])
        _RecordingFallbackLoop.attach(loop, reply="analyze_image 又超时了，等下再试")
        result = _fallback(
            loop,
            [{"step": 1, "tool": "analyze_image", "ok": False, "error": "tool_timeout:analyze_image"}],
        )
        self.assertNotIn("analyze_image", result.reply_text)
        self.assertIn("等下再试", result.reply_text)


class RawFailureDisplayHygieneTests(unittest.TestCase):
    """通道二：失败 display 不再原样外发。"""

    def test_should_not_send_loop_synthesized_diagnostic_to_user(self):
        """循环自己拼的 "<tool> 失败: <error>" 是给模型看的诊断串，不是回复。"""
        loop = _make_loop([])
        _RecordingFallbackLoop.attach(loop)
        result = _fallback(
            loop,
            [
                {
                    "step": 1,
                    "tool": "analyze_image",
                    "ok": False,
                    "display": "analyze_image 失败: tool_timeout:analyze_image",
                    "error": "tool_timeout:analyze_image",
                    "display_synthetic": True,
                }
            ],
        )
        self.assertNotIn("analyze_image", result.reply_text)
        self.assertNotIn("tool_timeout", result.reply_text)

    def test_should_not_send_failure_display_that_is_only_machine_tokens(self):
        """剥掉机器标识符后不成句的 display，一律换成类别兜底句。"""
        loop = _make_loop([])
        _RecordingFallbackLoop.attach(loop)
        result = _fallback(
            loop,
            [
                {
                    "step": 1,
                    "tool": "qzone_browse",
                    "ok": False,
                    "display": "qzone_api_error retcode=-5503022",
                    "error": "qzone_api_error",
                }
            ],
        )
        self.assertNotIn("qzone_api_error", result.reply_text)
        self.assertNotIn("retcode", result.reply_text)
        self.assertNotIn("5503022", result.reply_text)

    def test_should_strip_machine_identifiers_from_a_human_written_failure_display(self):
        """工具自己写的人话仍然外发（不丢真实原因），但内嵌的机器码要剥掉。"""
        loop = _make_loop([])
        _RecordingFallbackLoop.attach(loop)
        result = _fallback(
            loop,
            [
                {
                    "step": 1,
                    "tool": "parse_video",
                    "ok": False,
                    "display": "B站限流了（412），稍等一会儿再试就好。错误码 bilibili_412_throttled",
                    "error": "bilibili_412_throttled",
                }
            ],
        )
        self.assertIn("稍等一会儿再试就好", result.reply_text)
        self.assertNotIn("bilibili_412_throttled", result.reply_text)

    def test_should_keep_media_url_intact_while_scrubbing(self):
        """脱敏不能顺手把链接剪断 —— 媒体投递靠它。"""
        scrubbed = AgentLoop._scrub_internal_state_text(
            "拿到了 https://example.com/a_b_c/v_1.mp4 但 parse_video_error 没跑完"
        )
        self.assertIn("https://example.com/a_b_c/v_1.mp4", scrubbed)
        self.assertNotIn("parse_video_error", scrubbed)


class CategoryReplyTests(unittest.TestCase):
    """类别兜底句本身要是人话，且不含内部状态。"""

    def test_should_give_a_plain_sentence_for_every_category(self):
        for category in (
            "timeout",
            "permission",
            "missing_args",
            "blocked",
            "unavailable",
            "not_found",
            "upstream",
            "unknown",
        ):
            with self.subTest(category):
                reply = AgentLoop._failure_category_reply([category])
                self.assertTrue(reply.strip(), category)
                self.assertNotIn("_", reply, f"{category} 兜底句含机器标识符：{reply}")


class _FlakyOnceRegistry(_StubRegistry):
    """第一次失败、之后成功的副作用工具。"""

    def __init__(self, names: set[str]):
        super().__init__(names)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        if name == "send_face" and len([c for c in self.calls if c[0] == "send_face"]) == 1:
            return ToolCallResult(
                ok=False, data={}, display="", error="tool_timeout:send_face"
            )
        return ToolCallResult(ok=True, data={}, display="已发送表情")

    def executions_of(self, tool_name: str) -> int:
        return sum(1 for name, _ in self.calls if name == tool_name)


class OncePerTurnCountsSuccessesTests(unittest.TestCase):
    """副作用工具的"一回合只做一次"数的是成功次数，不是调用次数。"""

    def test_should_allow_retry_after_a_transient_failure_of_a_side_effect_tool(self):
        """首次瞬时失败后，同 args 重试必须放行 —— 否则本回合再也发不出去。"""
        registry = _FlakyOnceRegistry({"send_face", "final_answer", "think"})
        loop = _make_loop(
            [
                '{"tool":"send_face","args":{"face_id":13}}',
                '{"tool":"send_face","args":{"face_id":13}}',
                '{"tool":"final_answer","args":{"text":"发好了"}}',
            ],
            registry=registry,
        )
        loop.max_same_tool_call = 3
        asyncio.run(loop.run(_make_ctx(message_text="给我发个笑脸")))
        self.assertEqual(
            registry.executions_of("send_face"),
            2,
            f"首次失败后应放行一次重试，实际={registry.calls}",
        )


class DeadStickerMediaStripTests(unittest.TestCase):
    """那段判据恒假的表情媒体清空逻辑已删除，不留半死不活的代码。"""

    def test_should_not_keep_a_never_executed_sticker_media_strip_branch(self):
        import inspect

        source = inspect.getsource(AgentLoop.run)
        self.assertNotIn("_STICKER_LIKE_TOOLS", source)
        self.assertNotIn("user_wants_preview", source)


if __name__ == "__main__":
    unittest.main()
