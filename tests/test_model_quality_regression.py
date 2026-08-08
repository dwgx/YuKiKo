from __future__ import annotations

import asyncio
import unittest

from services.model_client import ModelClient
from services.model_quality import ModelQualityTracker

# 全局尝试记录：_CLIENTS monkeypatch 是进程级的，测试间通过 reset 隔离
_attempts: list[str] = []


class _QualityPrimaryClient:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = True
        self.model = "primary-model"
        self.base_url = "https://primary.example/v1"

    async def chat_completion(
        self,
        messages: list[dict],
        response_format: dict | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        **_kw,
    ) -> dict:
        _ = messages, response_format, max_tokens, tools, tool_choice
        _attempts.append(self.model)
        raise RuntimeError("HTTP 401: invalid token")


class _QualityProbeClient:
    def __init__(self, config: dict):
        self.config = config
        self.enabled = True
        self.model = str(config.get("probe_model", "probe"))
        self.base_url = f"https://{self.model}.example/v1"
        self.fail = bool(config.get("probe_fail", False))
        self.error = config.get("probe_error", RuntimeError("HTTP 503: upstream unavailable"))

    async def chat_completion(
        self,
        messages: list[dict],
        response_format: dict | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        **_kw,
    ) -> dict:
        _ = messages, response_format, max_tokens, tools, tool_choice
        _attempts.append(self.model)
        if self.fail:
            raise self.error
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    async def chat_json(self, messages: list[dict]) -> dict:
        _attempts.append(self.model)
        if self.fail:
            raise self.error
        return {"ok": True}


class _NoChatJsonPrimaryClient(_QualityPrimaryClient):
    # 故意不实现 chat_json：不支持的方法不该被当成质量差
    pass


class ModelQualityTrackerTests(unittest.TestCase):
    def test_should_prefer_higher_success_rate(self) -> None:
        tracker = ModelQualityTracker()
        for _ in range(5):
            tracker.record_outcome("p", "stable-model", True, 500.0, "success")
        for i in range(5):
            tracker.record_outcome("p", "flaky-model", i < 2, 500.0, "success" if i < 2 else "timeout")

        rankings = tracker.get_rankings()
        self.assertEqual([model for model, _ in rankings], ["stable-model", "flaky-model"])
        self.assertGreater(rankings[0][1], rankings[1][1])

    def test_should_prefer_lower_latency(self) -> None:
        tracker = ModelQualityTracker()
        for _ in range(5):
            tracker.record_outcome("p", "fast-model", True, 100.0, "success")
        for _ in range(5):
            tracker.record_outcome("p", "slow-model", True, 15_000.0, "success")

        rankings = dict(tracker.get_rankings())
        self.assertGreater(rankings["fast-model"], rankings["slow-model"])

    def test_should_penalize_reject_harder_than_generic_error(self) -> None:
        tracker = ModelQualityTracker()
        for _ in range(5):
            tracker.record_outcome("p", "error-model", False, 1000.0, "error")
        for _ in range(5):
            tracker.record_outcome("p", "reject-model", False, 1000.0, "reject")

        rankings = dict(tracker.get_rankings())
        self.assertGreater(rankings["error-model"], rankings["reject-model"])

    def test_should_exclude_models_below_min_samples(self) -> None:
        tracker = ModelQualityTracker()
        tracker.record_outcome("p", "new-model", True, 100.0, "success")
        tracker.record_outcome("p", "new-model", True, 100.0, "success")

        self.assertIsNone(tracker.score_for("p", "new-model"))
        self.assertEqual(tracker.get_rankings(), [])
        self.assertEqual(tracker.get_rankings(min_samples=2)[0][0], "new-model")

    def test_should_keep_ring_buffer_cap_per_model(self) -> None:
        tracker = ModelQualityTracker(max_records_per_model=100)
        for i in range(120):
            tracker.record_outcome("p", "m", True, float(i), "success")

        records = tracker._records[("p", "m")]
        self.assertEqual(len(records), 100)
        # 最旧的 20 条被挤出，剩下的第一条延迟是第 21 次调用（i=20）
        self.assertEqual(records[0][1], 20.0)
        self.assertEqual(tracker.stats("p", "m")["samples"], 100)

    def test_stats_should_report_snapshot(self) -> None:
        tracker = ModelQualityTracker()
        tracker.record_outcome("p", "m", True, 200.0, "success")
        tracker.record_outcome("p", "m", False, 3000.0, "timeout")
        tracker.record_outcome("p", "m", True, 400.0, "success")

        stats = tracker.stats("p", "m")
        self.assertIsNotNone(stats)
        self.assertEqual(stats["samples"], 3)
        self.assertAlmostEqual(stats["success_rate"], 2 / 3)
        self.assertAlmostEqual(stats["avg_success_latency_ms"], 300.0)
        self.assertIsNone(tracker.stats("p", "unknown"))


class ModelClientQualityIntegrationTests(unittest.TestCase):
    def _reset(self) -> None:
        _attempts.clear()

    def _build_client(self, rank_failover: bool = False) -> ModelClient:
        cfg = {
            "provider": "primary_test_provider",
            "fallback_providers": ["probe_a_test_provider", "probe_b_test_provider"],
            "providers": {
                "primary_test_provider": {"api_key": "x-primary"},
                "probe_a_test_provider": {"api_key": "x-a", "probe_model": "probe-a-model"},
                "probe_b_test_provider": {"api_key": "x-b", "probe_model": "probe-b-model"},
            },
        }
        if rank_failover:
            cfg["rank_failover"] = True
        return ModelClient(cfg)

    def _install(self, primary: type, probes: dict[str, dict]) -> dict:
        original = dict(ModelClient._CLIENTS)
        ModelClient._CLIENTS["primary_test_provider"] = primary
        for name, probe_cfg in probes.items():

            class _ProbeWithConfig(_QualityProbeClient):
                def __init__(self, config: dict):
                    merged = dict(config)
                    merged.update(probe_cfg)
                    super().__init__(merged)

            ModelClient._CLIENTS[name] = _ProbeWithConfig
        return original

    def _restore(self, original: dict) -> None:
        ModelClient._CLIENTS.clear()
        ModelClient._CLIENTS.update(original)

    def _seed(
        self, client: ModelClient, provider: str, model: str, ok: bool, n: int = 5, latency: float = 100.0
    ) -> None:
        for _ in range(n):
            client.record_outcome(
                provider,
                model,
                ok,
                latency,
                "success" if ok else "reject",
            )

    def test_default_off_keeps_config_order(self) -> None:
        self._reset()
        original = self._install(
            _QualityPrimaryClient,
            {
                "probe_a_test_provider": {"probe_fail": True},
                "probe_b_test_provider": {"probe_fail": True},
            },
        )
        try:
            client = self._build_client(rank_failover=False)
            # 即使 B 历史质量更高，默认关闭时仍按配置顺序尝试
            self._seed(client, "probe_b_test_provider", "probe-b-model", True)
            self._seed(client, "probe_a_test_provider", "probe-a-model", False)

            with self.assertRaises(RuntimeError):
                asyncio.run(client.chat_text(messages=[{"role": "user", "content": "ping"}]))
            self.assertEqual(_attempts, ["primary-model", "probe-a-model", "probe-b-model"])
        finally:
            self._restore(original)

    def test_rank_failover_reorders_fallback_chain(self) -> None:
        self._reset()
        original = self._install(
            _QualityPrimaryClient,
            {
                "probe_a_test_provider": {"probe_fail": True},
                "probe_b_test_provider": {},
            },
        )
        try:
            client = self._build_client(rank_failover=True)
            # A 历史质量差（全 reject），B 历史质量好（全 success）→ B 应先被尝试
            self._seed(client, "probe_a_test_provider", "probe-a-model", False)
            self._seed(client, "probe_b_test_provider", "probe-b-model", True)

            result = asyncio.run(client.chat_text(messages=[{"role": "user", "content": "ping"}]))
            self.assertEqual(result, "ok")
            self.assertEqual(_attempts, ["primary-model", "probe-b-model"])
            self.assertEqual(client._active_provider, "probe_b_test_provider")
        finally:
            self._restore(original)

    def test_should_record_outcomes_during_failover(self) -> None:
        self._reset()
        original = self._install(
            _QualityPrimaryClient,
            {"probe_b_test_provider": {}},
        )
        try:
            client = self._build_client(rank_failover=False)
            asyncio.run(client.chat_text(messages=[{"role": "user", "content": "ping"}]))

            primary_stats = client._quality_tracker.stats("primary_test_provider", "primary-model")
            backup_stats = client._quality_tracker.stats("probe_b_test_provider", "probe-b-model")
            self.assertIsNotNone(primary_stats)
            self.assertEqual(primary_stats["samples"], 1)
            self.assertEqual(primary_stats["success_rate"], 0.0)
            self.assertIsNotNone(backup_stats)
            self.assertEqual(backup_stats["success_rate"], 1.0)
            # 401 应归类为 reject
            last = client._quality_tracker._records[("primary_test_provider", "primary-model")][-1]
            self.assertEqual(last[2], "reject")
        finally:
            self._restore(original)

    def test_unsupported_method_should_not_record_quality(self) -> None:
        self._reset()
        original = self._install(
            _NoChatJsonPrimaryClient,
            {"probe_b_test_provider": {}},
        )
        try:
            client = self._build_client(rank_failover=False)
            result = asyncio.run(client.chat_json(messages=[{"role": "user", "content": "ping"}]))
            self.assertEqual(result, {"ok": True})
            self.assertEqual(_attempts, ["probe-b-model"])
            # 主 provider 只是不支持该方法，不是质量差：不应产生质量记录
            self.assertIsNone(client._quality_tracker.stats("primary_test_provider", "primary-model"))
            self.assertEqual(
                client._quality_tracker.stats("probe_b_test_provider", "probe-b-model")["samples"],
                1,
            )
        finally:
            self._restore(original)

    def test_classify_error_mapping(self) -> None:
        self.assertEqual(ModelClient._classify_error(RuntimeError("HTTP 401: invalid token")), "reject")
        self.assertEqual(ModelClient._classify_error(RuntimeError("quota exceeded")), "reject")
        self.assertEqual(ModelClient._classify_error(TimeoutError("request timed out")), "timeout")
        self.assertEqual(ModelClient._classify_error(RuntimeError("bad gateway")), "timeout")
        self.assertEqual(ModelClient._classify_error(RuntimeError("model exploded")), "error")


if __name__ == "__main__":
    unittest.main()
