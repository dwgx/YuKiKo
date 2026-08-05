"""降级可观测性回归测试。

背景（14 小时真实日志实测）：skiapi 返回 233 次 ``HTTP 429: All available accounts
exhausted``，触发模型链 claude-sonnet-5 -> claude-sonnet-4-6 -> claude-haiku-4-5 降级，
97 次触底到 haiku。而 haiku 撑不住 247 个工具槽位的 tool-calling schema，
产生 181 次 ``agent_tool_args_recovered_from_malformed_json``。

小时级相关性一次不差：降级为 0 的 12 个小时里坏 JSON 全部为 0；
06 时降 haiku 39 次 / 坏 JSON 146 次，07 时降 12 次 / 坏 JSON 34 次。

根因不是「降级」本身（降级让机器人活着），而是**降级不可观测**：
``services/openai_compatible.py`` 原先只在「尝试换模型」时打日志，
从不记录「哪个模型实际接管了流量」，因此 agent 循环无从得知自己正跑在弱模型上，
继续按主模型规格发送大 schema，表现为静默吐坏 JSON 而不是报错。

本测试锁定 ``served_model_state()`` 契约：降级必须可查、且 provider 级 failover
也要算作降级。
"""

from __future__ import annotations

import unittest

from services.model_client import ModelClient
from services.openai_compatible import OpenAICompatibleClient


def _make_client(provider: str = "skiapi", model: str = "claude-sonnet-5") -> OpenAICompatibleClient:
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    client.provider = provider
    client.model = model
    client._served_model = ""
    client._served_depth = 0
    client._served_at = 0.0
    return client


class ServedModelStateTests(unittest.TestCase):
    def test_should_report_not_degraded_before_any_request(self) -> None:
        client = _make_client()
        state = client.served_model_state()
        self.assertFalse(state["degraded"])
        self.assertEqual(state["depth"], 0)
        # 尚无请求时回落到配置的主模型名，而不是空串
        self.assertEqual(state["model"], "claude-sonnet-5")

    def test_should_report_not_degraded_when_primary_model_serves(self) -> None:
        client = _make_client()
        client._record_served_model("claude-sonnet-5", 0, True)
        state = client.served_model_state()
        self.assertFalse(state["degraded"])
        self.assertEqual(state["depth"], 0)
        self.assertEqual(state["model"], "claude-sonnet-5")

    def test_should_report_degraded_with_depth_when_fallback_model_serves(self) -> None:
        client = _make_client()
        client._record_served_model("claude-haiku-4-5", 2, True)
        state = client.served_model_state()
        self.assertTrue(state["degraded"])
        self.assertEqual(state["depth"], 2)
        self.assertEqual(state["model"], "claude-haiku-4-5")

    def test_should_warn_only_when_served_model_changes(self) -> None:
        """降级日志必须只在状态变化时打，否则每回合刷屏会淹没真实信号。"""
        client = _make_client()
        with self.assertLogs("yukiko.openai_compatible", level="WARNING") as captured:
            client._record_served_model("claude-haiku-4-5", 2, True)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("model_degraded_serving", captured.output[0])
        self.assertIn("depth=2", captured.output[0])

        # 同一模型重复服务不应再打 WARNING（assertNoLogs 需要 3.10+）
        with self.assertNoLogs("yukiko.openai_compatible", level="WARNING"):
            client._record_served_model("claude-haiku-4-5", 2, True)

    def test_should_recover_to_primary_state_after_upstream_recovers(self) -> None:
        client = _make_client()
        client._record_served_model("claude-haiku-4-5", 2, True)
        self.assertTrue(client.served_model_state()["degraded"])
        client._record_served_model("claude-sonnet-5", 0, True)
        self.assertFalse(client.served_model_state()["degraded"])


class ModelClientServedStateTests(unittest.TestCase):
    def _make_model_client(
        self, active: object, primary: str = "skiapi", active_provider: str | None = None
    ) -> ModelClient:
        mc = ModelClient.__new__(ModelClient)
        mc.provider = primary
        # ModelClient.model 是只读 property，从 self.client.model 派生，不能赋值
        mc._primary_provider = primary
        mc._active_provider = active_provider or primary
        mc.client = active
        mc._fallback_clients = {}
        mc._fallback_providers = []
        return mc

    def test_should_delegate_to_active_client_state(self) -> None:
        inner = _make_client()
        inner._record_served_model("claude-haiku-4-5", 2, True)
        mc = self._make_model_client(inner)
        state = mc.served_model_state()
        self.assertTrue(state["degraded"])
        self.assertEqual(state["model"], "claude-haiku-4-5")
        self.assertFalse(state["provider_failover"])

    def test_should_treat_provider_failover_as_degraded(self) -> None:
        """provider 已 failover 时，即便该 provider 内部用的是它自己的主模型，也算降级。"""
        inner = _make_client(provider="openrouter", model="anthropic/claude-sonnet-4.5")
        inner._record_served_model("anthropic/claude-sonnet-4.5", 0, True)
        mc = self._make_model_client(inner, primary="skiapi", active_provider="openrouter")
        mc._fallback_clients = {"openrouter": inner}
        state = mc.served_model_state()
        self.assertTrue(state["provider_failover"])
        self.assertTrue(state["degraded"])

    def test_should_degrade_gracefully_when_client_lacks_the_hook(self) -> None:
        """老 provider client 没实现 served_model_state 时不许抛异常。"""

        class Bare:
            model = "some-model"

        mc = self._make_model_client(Bare())
        state = mc.served_model_state()
        self.assertFalse(state["degraded"])
        self.assertEqual(state["model"], "some-model")


if __name__ == "__main__":
    unittest.main()
