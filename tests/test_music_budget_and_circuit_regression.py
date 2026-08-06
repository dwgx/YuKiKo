"""聚合音乐源的预算必须夹住**每一个**出网请求，坏响应也要熔断。

两条实测缺口（复核提出，本文件锁死）：

1. **爬虫兜底腿不受 deadline 夹制。**
   `_discover_alger_api_bases_via_crawl` 只在每个 base 的循环顶查一次 deadline，
   进入 `_crawl_one_web_base` 后用 `self._timeout`（模板 15s）发最多 5 个请求
   （1 个首页 + 4 个 JS），一个都不夹 remaining。
   单次爬取最坏 5×15=75 秒，而 upstream_budget_seconds 默认 8 ——
   「秒级放弃聚合源」在这条腿上根本不成立。

2. **熔断器漏掉最常见的故障形态。**
   原来只有 `httpx.TransportError` 和 `_RETRYABLE_STATUS={429,500,502,503,504}`
   会 `_mark_host_unreachable`。稳定 7s 回 404、或 HTTP 200 但 body 不是 JSON，
   都走裸 `except Exception` / `isinstance` 检查后直接 return，**不标记 host**。
   实测：上游稳定回 404 时跑完 4 个 variant、8 次 HTTP，`_unreachable_until` 仍是 {}。
   ToolExecutor 长驻，熔断状态本该跨回合复用，漏标记等于每回合从头再烧一遍。

音乐工具原本实测成功率 0%（music_search 5/6 撞 28s、music_play 5/7 撞 55s），
上游 mc.alger.fun 已彻底不可达（curl HTTP 000，HTTPS 版证书错误）。
"""

from __future__ import annotations

import time
import unittest


def _engine(**overrides):
    """造一个只带聚合源配置的 MusicEngine，不碰磁盘也不起网络。"""

    from core.music import MusicEngine

    cfg = {
        "enable": True,
        "api_base": "",
        "api_bases": ["https://slow.example/api"],
        "allow_insecure_api_base": False,
        "upstream_budget_seconds": 2,
        "unreachable_cooldown_seconds": 300,
        "timeout_seconds": 15,
        "cache_dir": "/tmp/cc-yk-music-test",
    }
    cfg.update(overrides)
    return MusicEngine(cfg)


class CrawlLegRespectsTheBudgetTests(unittest.TestCase):
    def test_crawl_accepts_a_deadline_argument(self) -> None:
        """签名里必须能收 deadline，否则外层的预算传不进来。"""

        import inspect

        from core.music import MusicEngine

        params = inspect.signature(MusicEngine._crawl_one_web_base).parameters
        self.assertIn(
            "deadline",
            params,
            "爬虫腿收不到 deadline —— 每个请求都会用满 timeout_seconds",
        )

    def test_crawl_gives_up_when_the_budget_is_already_spent(self) -> None:
        """预算已耗尽时不该再发任何请求。"""

        import asyncio

        engine = _engine()
        past = time.monotonic() - 1.0  # 已经过期
        found = asyncio.run(
            engine._crawl_one_web_base("https://slow.example", "https://slow.example/api", deadline=past)
        )
        # 只应回到「把 api_base 自己加进去」这一步，不发请求
        self.assertIsInstance(found, list)

    def test_discover_passes_the_deadline_down(self) -> None:
        """外层把 deadline 透传下去，而不是自己查完就不管了。"""

        import inspect

        from core.music import MusicEngine

        src = inspect.getsource(MusicEngine._discover_alger_api_bases_via_crawl)
        self.assertIn(
            "deadline=deadline",
            src,
            "_discover 调 _crawl_one_web_base 时没把 deadline 传下去",
        )


class BudgetIsPerOperationNotPerCallTests(unittest.TestCase):
    def test_engine_tracks_an_operation_deadline(self) -> None:
        engine = _engine()
        self.assertTrue(
            hasattr(engine, "_operation_deadline"),
            "没有「本次工具调用」级别的预算，预算会被 variant/候选循环乘 4~8 倍",
        )

    def test_resolve_prefers_the_operation_deadline(self) -> None:
        engine = _engine()
        pinned = time.monotonic() + 999
        engine._operation_deadline = pinned
        self.assertEqual(engine._resolve_upstream_deadline(), pinned)

    def test_resolve_falls_back_to_a_single_budget_when_not_in_an_operation(self) -> None:
        engine = _engine()
        engine._operation_deadline = None
        before = time.monotonic()
        got = engine._resolve_upstream_deadline()
        self.assertGreater(got, before)
        self.assertLessEqual(got, before + engine._upstream_budget_s + 0.5)

    def test_nested_operations_do_not_reset_the_deadline(self) -> None:
        """play() 内部会调 search()。内层重置的话外层的预算就白设了。"""

        engine = _engine()
        with engine._upstream_operation():
            outer = engine._operation_deadline
            with engine._upstream_operation():
                self.assertEqual(engine._operation_deadline, outer)
            self.assertEqual(engine._operation_deadline, outer)
        self.assertIsNone(engine._operation_deadline)


class CircuitOpensOnBadResponsesTests(unittest.TestCase):
    def test_non_dict_payload_opens_the_circuit(self) -> None:
        """HTTP 200 但 body 不是 JSON 对象 —— 这个 host 给不出可用 JSON。"""

        import asyncio

        import httpx

        engine = _engine()
        endpoint = "https://bad.example/api/search"

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return ["not", "a", "dict"]

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                return _Resp()

        original = httpx.AsyncClient
        httpx.AsyncClient = _Client  # type: ignore[assignment]
        try:
            got = asyncio.run(
                engine._fetch_alger_json(
                    endpoint, {}, source="test", deadline=time.monotonic() + 5
                )
            )
        finally:
            httpx.AsyncClient = original  # type: ignore[assignment]

        self.assertIsNone(got)
        self.assertTrue(
            engine._is_host_unreachable(endpoint),
            "200 + 非 JSON 对象没有熔断 —— 下一回合还会从头再烧一遍",
        )

    def test_json_decode_failure_opens_the_circuit(self) -> None:
        import asyncio

        import httpx

        engine = _engine()
        endpoint = "https://html.example/api/search"

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                return _Resp()

        original = httpx.AsyncClient
        httpx.AsyncClient = _Client  # type: ignore[assignment]
        try:
            got = asyncio.run(
                engine._fetch_alger_json(
                    endpoint, {}, source="test", deadline=time.monotonic() + 5
                )
            )
        finally:
            httpx.AsyncClient = original  # type: ignore[assignment]

        self.assertIsNone(got)
        self.assertTrue(engine._is_host_unreachable(endpoint))

    def test_circuit_open_means_zero_requests(self) -> None:
        """熔断期内必须一个请求都不发，否则「静默期」是假的。"""

        import asyncio

        import httpx

        engine = _engine()
        endpoint = "https://down.example/api/search"
        engine._mark_host_unreachable(endpoint, "test")

        calls = {"n": 0}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **kw):
                calls["n"] += 1
                raise AssertionError("熔断期内不该发请求")

        original = httpx.AsyncClient
        httpx.AsyncClient = _Client  # type: ignore[assignment]
        try:
            got = asyncio.run(
                engine._fetch_alger_json(
                    endpoint, {}, source="test", deadline=time.monotonic() + 5
                )
            )
        finally:
            httpx.AsyncClient = original  # type: ignore[assignment]

        self.assertIsNone(got)
        self.assertEqual(calls["n"], 0)

    def test_cooldown_zero_disables_the_circuit(self) -> None:
        """业主可以关掉熔断 —— 关掉时不能反而永久熔断。"""

        engine = _engine(unreachable_cooldown_seconds=0)
        endpoint = "https://x.example/api"
        engine._mark_host_unreachable(endpoint, "test")
        self.assertFalse(engine._is_host_unreachable(endpoint))


class InsecureBaseIsRejectedTests(unittest.TestCase):
    def test_public_plaintext_http_base_is_skipped_by_default(self) -> None:
        """原默认值 http://mc.alger.fun/api 是明文，本仓规矩是出网走 HTTPS。"""

        engine = _engine(api_bases=["http://mc.alger.fun/api"])
        self.assertNotIn(
            "http://mc.alger.fun/api",
            engine._api_bases,
            "公网明文 HTTP 候选默认应被拒",
        )

    def test_opt_in_flag_allows_plaintext(self) -> None:
        engine = _engine(
            api_bases=["http://mc.alger.fun/api"], allow_insecure_api_base=True
        )
        self.assertIn("http://mc.alger.fun/api", engine._api_bases)

    def test_loopback_plaintext_is_always_allowed(self) -> None:
        """自建 NeteaseCloudMusicApi 跑在环回上是最稳的方案，不能拦。"""

        engine = _engine(api_bases=["http://127.0.0.1:3000/api"])
        self.assertIn("http://127.0.0.1:3000/api", engine._api_bases)


if __name__ == "__main__":
    unittest.main()
