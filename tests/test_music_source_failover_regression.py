"""音乐源换源 / 快速失败回归。

上游 mc.alger.fun 实测已挂（HTTP 503，每次响应约 5.1s），旧逻辑一个 query variant
能对同一个死端点发 7 次请求 ≈ 39s，吃满 agent 的 tool_timeout_seconds=28，
可用的 netease 腿（实测 0.9s 出 10 条）永远轮不到。这里锁住四条行为：

1. 明文 HTTP 的公网聚合源默认被拒（出网必须 HTTPS），环回/内网自建服务放行。
2. api_base 支持多个候选，前一个失败就转下一个并留 WARNING。
3. 上游不可达时在秒级放弃，不烧满 tool 超时；同一 host 熔断后后续调用零请求。
4. 未配置聚合源时不再拼出裸 "/lyric" 之类的坏 URL，如实跳过并留日志。
"""
from __future__ import annotations

import asyncio
import unittest
from typing import Any

import httpx
from core.music import MusicEngine


def _make_engine(**music_cfg: Any) -> MusicEngine:
    cfg = {
        "timeout_seconds": 5,
        "local_source_enable": False,
        "cache_dir": "storage/cache/music",
    }
    cfg.update(music_cfg)
    return MusicEngine({"music": cfg})


class _FakeTransport:
    """记录每次请求，按 host 返回预置响应。"""

    def __init__(self, responses: dict[str, Any], *, latency: float = 0.0) -> None:
        self._responses = responses
        self._latency = latency
        self.requests: list[str] = []

    def install(self, engine: MusicEngine) -> None:
        transport = self

        async def _fake_get(client: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
            transport.requests.append(str(url))
            if transport._latency:
                await asyncio.sleep(transport._latency)
            for host, outcome in transport._responses.items():
                if host in str(url):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
            raise httpx.ConnectError("unmapped host")

        httpx.AsyncClient.get = _fake_get  # type: ignore[method-assign]
        self._engine = engine


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._payload = payload or {}
        self.text = ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


def _songs_payload(name: str) -> dict[str, Any]:
    return {
        "result": {
            "songs": [
                {"id": 1, "name": name, "artists": [{"name": "某人"}], "duration": 180000}
            ]
        }
    }


class MusicSourceFailoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._real_get = httpx.AsyncClient.get

    def tearDown(self) -> None:
        httpx.AsyncClient.get = self._real_get  # type: ignore[method-assign]

    def test_should_reject_plaintext_public_api_base_by_default(self) -> None:
        engine = _make_engine(api_base="http://mc.alger.fun/api")
        self.assertEqual(engine._api_bases, [])
        self.assertEqual(engine._candidate_alger_api_bases(), [])

    def test_should_keep_plaintext_api_base_when_operator_opts_in(self) -> None:
        engine = _make_engine(api_base="http://mc.alger.fun/api", allow_insecure_api_base=True)
        self.assertEqual(engine._api_bases, ["http://mc.alger.fun/api"])

    def test_should_allow_plaintext_api_base_on_loopback_and_private_hosts(self) -> None:
        engine = _make_engine(
            api_bases=["http://127.0.0.1:3000/api", "http://192.168.1.9:3000/api", "http://localhost:3000/api"]
        )
        self.assertEqual(
            engine._api_bases,
            ["http://127.0.0.1:3000/api", "http://192.168.1.9:3000/api", "http://localhost:3000/api"],
        )

    def test_should_accept_multiple_https_api_base_candidates_in_order(self) -> None:
        engine = _make_engine(
            api_bases=["https://first.example/api", "https://second.example/api"],
            api_base="https://legacy.example/api",
        )
        self.assertEqual(
            engine._api_bases,
            ["https://first.example/api", "https://second.example/api", "https://legacy.example/api"],
        )

    def test_should_default_to_no_plaintext_aggregator(self) -> None:
        self.assertFalse(MusicEngine._DEFAULT_API_BASE.startswith("http://"))
        self.assertEqual(_make_engine()._api_bases, [])

    async def test_should_fail_over_to_next_candidate_when_first_base_is_down(self) -> None:
        engine = _make_engine(api_bases=["https://dead.example/api", "https://alive.example/api"])
        transport = _FakeTransport({
            "dead.example": _FakeResponse(503),
            "alive.example": _FakeResponse(200, _songs_payload("能听的歌")),
        })
        transport.install(engine)

        rows = await engine._search_alger("测试", 10)

        self.assertEqual([row.name for row in rows], ["能听的歌"])
        self.assertTrue(any("alive.example" in url for url in transport.requests))

    async def test_should_give_up_dead_upstream_well_within_tool_timeout(self) -> None:
        # 每次请求 0.4s；旧逻辑对同一端点发 7 次 + 退避 sleep，新逻辑必须秒级收手。
        engine = _make_engine(
            api_bases=["https://dead.example/api"],
            timeout_seconds=15,
            upstream_budget_seconds=3,
        )
        transport = _FakeTransport({"dead.example": _FakeResponse(503)}, latency=0.4)
        transport.install(engine)

        loop = asyncio.get_running_loop()
        started = loop.time()
        rows = await engine._search_alger("测试", 10)
        elapsed = loop.time() - started

        self.assertEqual(rows, [])
        self.assertLessEqual(len(transport.requests), 2)
        self.assertLess(elapsed, 3.0)

    async def test_should_skip_requests_entirely_after_host_circuit_opens(self) -> None:
        engine = _make_engine(api_bases=["https://dead.example/api"], upstream_budget_seconds=3)
        transport = _FakeTransport({"dead.example": httpx.ConnectTimeout("blackhole")})
        transport.install(engine)

        await engine._search_alger("第一次", 10)
        first_round = len(transport.requests)
        self.assertGreaterEqual(first_round, 1)

        await engine._search_alger("第二次", 10)

        self.assertEqual(len(transport.requests), first_round)
        self.assertEqual(engine._candidate_alger_api_bases(), [])

    async def test_should_skip_lyrics_lookup_when_no_aggregator_configured(self) -> None:
        engine = _make_engine()
        transport = _FakeTransport({})
        transport.install(engine)

        self.assertEqual(await engine.get_lyrics(123), "")
        self.assertEqual(transport.requests, [])


class MusicSetupDefaultTests(unittest.TestCase):
    def test_setup_wizard_should_not_write_plaintext_music_api_base(self) -> None:
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "core" / "webui_setup_support.py"
        text = source.read_text(encoding="utf-8")
        offending = [
            line.strip()
            for line in text.splitlines()
            if "music_api_base" in line and 'or "http://' in line
        ]
        self.assertEqual(offending, [])


if __name__ == "__main__":
    unittest.main()
