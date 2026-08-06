"""输出截断可观测性 + 工具面预算建议回归测试。

背景：``max_tokens`` 把输出切断时，tool_call 的 arguments JSON 会断在半路，
和「模型吐坏 JSON」长得完全一样。``finish_reason`` 早就被解析并回传，
但全仓（``grep finish_reason core/``）没有任何调用方读取，于是这两类故障无法区分 ——
真实成因是输出预算不够，却只能去怀疑模型能力或 prompt。

畸形 arguments 在本仓至少有三个独立成因，混在一起就会修错地方：
1. 输出被 max_tokens 截断 —— 本文件锁定它必须自报；
2. 网关返回结构性畸形（实测 ``{}{"url": ...}``）—— 在 core/agent.py 修复，与截断无关；
3. 弱模型在大工具面下能力不足 —— 由 served_model_state 的 depth 指认。

本文件同时锁定 ``tool_budget_advice()`` 的契约：降级时收紧工具面上限，
但上限必须高于 navigator 单分区的 22(+3 控制)，否则会砍掉本次真正需要的工具，
把「可修复的畸形 JSON」换成「任务直接做不了」。
"""

from __future__ import annotations

import asyncio
import unittest

from services.model_client import (
    _TOOL_CEILING_DEGRADED,
    _TOOL_CEILING_PRIMARY,
    ModelClient,
)
from services.openai_compatible import OpenAICompatibleClient

# navigator 单分区实测最大工具数（web_research=22）+ 3 个控制工具。
_MAX_SECTION_TOOLS = 22
_CONTROL_TOOLS = ("think", "final_answer", "navigate_section")


def _make_client(
    provider: str = "skiapi",
    model: str = "claude-sonnet-5",
    max_tokens: int = 1600,
) -> OpenAICompatibleClient:
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    client.provider = provider
    client.model = model
    client.max_tokens = max_tokens
    client._served_model = ""
    client._served_depth = 0
    client._served_at = 0.0
    return client


class OutputTruncationWarningTests(unittest.TestCase):
    """``finish_reason == "length"`` 必须可区分于「模型吐坏 JSON」。"""

    def test_should_warn_when_truncated_with_tools(self) -> None:
        client = _make_client()
        with self.assertLogs("yukiko.openai_compatible", level="WARNING") as captured:
            client._warn_if_output_truncated(
                {"choices": [{"finish_reason": "length"}]}, "claude-haiku-4-5", 1600, True
            )
        self.assertIn("model_output_truncated", captured.output[0])
        self.assertIn("max_tokens=1600", captured.output[0])
        # 带 tools 时必须额外提示，避免下游把截断误判为畸形输出去「修 JSON」
        self.assertIn("勿误判", captured.output[0])

    def test_should_warn_without_tool_hint_when_no_tools(self) -> None:
        client = _make_client()
        with self.assertLogs("yukiko.openai_compatible", level="WARNING") as captured:
            client._warn_if_output_truncated(
                {"choices": [{"finish_reason": "length"}]}, "m", 1600, False
            )
        self.assertIn("model_output_truncated", captured.output[0])
        self.assertNotIn("勿误判", captured.output[0])

    def test_should_stay_silent_on_normal_stop(self) -> None:
        client = _make_client()
        with self.assertNoLogs("yukiko.openai_compatible", level="WARNING"):
            client._warn_if_output_truncated(
                {"choices": [{"finish_reason": "stop"}]}, "m", 1600, True
            )

    def test_should_not_raise_on_malformed_response_shapes(self) -> None:
        client = _make_client()
        for bad in (
            None,
            {},
            {"choices": []},
            {"choices": "x"},
            {"choices": [None]},
            {"choices": [{}]},
        ):
            with self.subTest(shape=repr(bad)):
                client._warn_if_output_truncated(bad, "m", 1600, True)


class TruncationReportsEffectiveMaxTokensTests(unittest.TestCase):
    """告警必须报「本次请求实际发出的 max_tokens」，而不是配置值。

    实测存在 per-call 覆盖：agent 循环按 ``agent.max_tokens``（缺省 4096）发请求，
    而 ``config/config.yml`` 的 ``api.max_tokens`` 是 1600。
    打配置值会把排查引向错误的旋钮。
    """

    def _run_chat(self, client: OpenAICompatibleClient, **kwargs) -> None:
        async def _fake_call(**call_kwargs):
            self.captured_max_tokens = call_kwargs["max_tokens"]
            return {"choices": [{"finish_reason": "length"}]}

        # enabled 是从 api_key 派生的只读 property，只能通过 api_key 打开
        client.api_key = "sk-test"
        client.fallback_models = []
        client._chat_completion_with_model = _fake_call  # type: ignore[method-assign]
        asyncio.run(client.chat_completion(messages=[{"role": "user", "content": "hi"}], **kwargs))

    def test_should_log_per_call_override_not_config_max_tokens(self) -> None:
        client = _make_client(max_tokens=1600)
        with self.assertLogs("yukiko.openai_compatible", level="WARNING") as captured:
            self._run_chat(client, max_tokens=4096)
        self.assertEqual(self.captured_max_tokens, 4096)
        line = "\n".join(captured.output)
        self.assertIn("model_output_truncated", line)
        self.assertIn("max_tokens=4096", line)
        self.assertNotIn("max_tokens=1600", line)

    def test_should_fall_back_to_config_max_tokens_without_override(self) -> None:
        client = _make_client(max_tokens=1600)
        with self.assertLogs("yukiko.openai_compatible", level="WARNING") as captured:
            self._run_chat(client)
        self.assertEqual(self.captured_max_tokens, 1600)
        self.assertIn("max_tokens=1600", "\n".join(captured.output))

    def test_should_report_served_depth_to_separate_causes(self) -> None:
        """同一行必须带 depth，才能区分「截断」和「弱模型撑不住大工具面」。"""
        client = _make_client()
        client._served_depth = 2
        with self.assertLogs("yukiko.openai_compatible", level="WARNING") as captured:
            client._warn_if_output_truncated(
                {"choices": [{"finish_reason": "length"}]}, "claude-haiku-4-5", 4096, True
            )
        self.assertIn("depth=2", "\n".join(captured.output))


class ResponsesEndpointTruncationTests(unittest.TestCase):
    """/responses 通道的截断信号原先被硬编码成 "stop"，等于整条通道对截断失明。

    Responses API 不返回 finish_reason，而用 ``status="incomplete"`` +
    ``incomplete_details.reason="max_output_tokens"`` 表达同一件事。
    """

    def test_should_map_max_output_tokens_to_length(self) -> None:
        reason = OpenAICompatibleClient._responses_finish_reason(
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        )
        self.assertEqual(reason, "length")

    def test_should_map_completed_response_to_stop(self) -> None:
        self.assertEqual(
            OpenAICompatibleClient._responses_finish_reason({"status": "completed"}), "stop"
        )

    def test_should_treat_incomplete_without_reason_as_truncated(self) -> None:
        self.assertEqual(
            OpenAICompatibleClient._responses_finish_reason({"status": "incomplete"}), "length"
        )

    def test_should_map_content_filter(self) -> None:
        self.assertEqual(
            OpenAICompatibleClient._responses_finish_reason(
                {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}}
            ),
            "content_filter",
        )

    def test_should_not_raise_on_malformed_payloads(self) -> None:
        for bad in (None, {}, {"incomplete_details": "x"}, {"status": 3}):
            with self.subTest(shape=repr(bad)):
                self.assertIsInstance(
                    OpenAICompatibleClient._responses_finish_reason(bad), str
                )

    def test_truncated_responses_payload_triggers_the_warning(self) -> None:
        """端到端：/responses 截断 -> finish_reason=length -> 告警真的打出来。"""
        client = _make_client()
        synthesized = {
            "choices": [
                {
                    "finish_reason": OpenAICompatibleClient._responses_finish_reason(
                        {
                            "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                        }
                    )
                }
            ]
        }
        with self.assertLogs("yukiko.openai_compatible", level="WARNING") as captured:
            client._warn_if_output_truncated(synthesized, "claude-sonnet-5", 4096, False)
        self.assertIn("model_output_truncated", "\n".join(captured.output))


class ToolBudgetAdviceTests(unittest.TestCase):
    """``served_model_state()`` 的第一个消费契约：降级时给出更紧的工具面上限。"""

    def _make_model_client(self, inner: object, active: str | None = None) -> ModelClient:
        mc = ModelClient.__new__(ModelClient)
        mc.provider = "skiapi"
        mc._primary_provider = "skiapi"
        mc._active_provider = active or "skiapi"
        mc.client = inner
        mc._fallback_clients = {} if active is None else {active: inner}
        mc._fallback_providers = []
        return mc

    def test_should_advise_primary_ceiling_when_healthy(self) -> None:
        inner = _make_client()
        inner._record_served_model("claude-sonnet-5", 0, True)
        advice = self._make_model_client(inner).tool_budget_advice()
        self.assertFalse(advice["degraded"])
        self.assertEqual(advice["max_tools"], _TOOL_CEILING_PRIMARY)
        self.assertEqual(advice["narrow_reason"], "")

    def test_should_tighten_ceiling_on_model_fallback(self) -> None:
        inner = _make_client()
        inner._record_served_model("claude-haiku-4-5", 2, True)
        advice = self._make_model_client(inner).tool_budget_advice()
        self.assertTrue(advice["degraded"])
        self.assertEqual(advice["max_tools"], _TOOL_CEILING_DEGRADED)
        self.assertEqual(advice["model"], "claude-haiku-4-5")
        # 原因必须写进日志可读的字符串，否则事后看不懂上限从哪来
        self.assertIn("model_fallback", advice["narrow_reason"])
        self.assertIn("depth=2", advice["narrow_reason"])

    def test_should_tighten_ceiling_on_provider_failover(self) -> None:
        inner = _make_client(provider="openrouter", model="anthropic/claude-sonnet-4.5")
        inner._record_served_model("anthropic/claude-sonnet-4.5", 0, True)
        advice = self._make_model_client(inner, active="openrouter").tool_budget_advice()
        self.assertTrue(advice["degraded"])
        self.assertEqual(advice["max_tools"], _TOOL_CEILING_DEGRADED)
        self.assertIn("provider_failover", advice["narrow_reason"])

    def test_degraded_ceiling_must_not_cut_into_a_full_navigator_section(self) -> None:
        """降级档上限必须容得下最大分区(22)+3 控制工具。

        砍到 25 以下会从分区里随机丢工具，有相当概率丢掉本次真正需要的那一个 ——
        把「可修复的畸形 JSON」换成「任务直接做不了」，是更差的结果。
        """
        self.assertGreaterEqual(
            _TOOL_CEILING_DEGRADED, _MAX_SECTION_TOOLS + len(_CONTROL_TOOLS)
        )
        self.assertGreaterEqual(_TOOL_CEILING_PRIMARY, _TOOL_CEILING_DEGRADED)

    def test_should_be_pure_and_repeatable(self) -> None:
        inner = _make_client()
        inner._record_served_model("claude-haiku-4-5", 1, True)
        mc = self._make_model_client(inner)
        self.assertEqual(mc.tool_budget_advice(), mc.tool_budget_advice())

    def test_should_not_raise_when_client_lacks_the_hook(self) -> None:
        class Bare:
            model = "some-model"

        advice = self._make_model_client(Bare()).tool_budget_advice()
        self.assertFalse(advice["degraded"])
        self.assertEqual(advice["max_tools"], _TOOL_CEILING_PRIMARY)


class EnforceToolCeilingTests(unittest.TestCase):
    def test_should_pass_through_a_normal_section_untouched(self) -> None:
        """分区化调用（<=25 个）永远不该被裁剪，顺序也不许变。"""
        tools = list(_CONTROL_TOOLS) + [f"tool_{i}" for i in range(_MAX_SECTION_TOOLS)]
        kept, dropped = ModelClient.enforce_tool_ceiling(
            tools, _TOOL_CEILING_DEGRADED, _CONTROL_TOOLS
        )
        self.assertEqual(dropped, 0)
        self.assertEqual(kept, tools)

    def test_should_cap_the_unscoped_fallback_tool_set(self) -> None:
        """navigator 未启用 / 交集为空时会回落到全量工具集，那才是需要兜住的场面。"""
        tools = [f"tool_{i}" for i in range(167)] + list(_CONTROL_TOOLS)
        kept, dropped = ModelClient.enforce_tool_ceiling(
            tools, _TOOL_CEILING_DEGRADED, _CONTROL_TOOLS
        )
        self.assertEqual(len(kept), _TOOL_CEILING_DEGRADED)
        self.assertEqual(dropped, len(tools) - _TOOL_CEILING_DEGRADED)

    def test_must_never_drop_control_tools(self) -> None:
        """控制工具被丢掉，模型连换分区和收尾都做不到，会卡死在循环里。"""
        tools = [f"tool_{i}" for i in range(200)] + list(_CONTROL_TOOLS)
        kept, _ = ModelClient.enforce_tool_ceiling(tools, 5, _CONTROL_TOOLS)
        for name in _CONTROL_TOOLS:
            self.assertIn(name, kept)

    def test_should_keep_control_tools_even_when_they_exceed_the_ceiling(self) -> None:
        kept, dropped = ModelClient.enforce_tool_ceiling(
            list(_CONTROL_TOOLS) + ["x"], 1, _CONTROL_TOOLS
        )
        self.assertEqual(kept, list(_CONTROL_TOOLS))
        self.assertEqual(dropped, 1)

    def test_should_ignore_blank_names_and_zero_ceiling(self) -> None:
        kept, dropped = ModelClient.enforce_tool_ceiling(["a", "", "  ", "b"], 0)
        self.assertEqual(kept, ["a", "b"])
        self.assertEqual(dropped, 0)


if __name__ == "__main__":
    unittest.main()
