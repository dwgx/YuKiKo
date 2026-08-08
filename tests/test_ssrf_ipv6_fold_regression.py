"""SSRF：IPv6 嵌入 IPv4 的形态必须折叠回 IPv4 规则再判定。

## 缺陷（对照 Rust 抓取后端 Reflection_King 的 SSRF 实现）

`core/tools.py::_is_public_ip_obj` 直接把地址丢给 Python `ipaddress` 的
`is_private` / `is_reserved` 判公网，但 IPv6 里嵌入 IPv4 的形态不会被正确
折叠：

- `::ffff:169.254.169.254`（IPv4-mapped）在旧版 CPython 判 `is_private=False`，
  直接放行 —— 云元数据端点裸奔。
- `64:ff9b::7f00:1`（NAT64 回环）在本机 Python 上只是**恰好**被
  `is_reserved` 拦住，换个版本或换个前缀就漏。
- 6to4 / Teredo 同理：判定结果依赖 stdlib 版本而不是嵌入的 IPv4。

## 重定向

`_safe_public_http_get` 是手动逐跳跟随（`follow_redirects=False`），每一跳
（含重定向 Location 解析出的下一跳）都重新过 `_is_safe_public_http_url_async`。
断言中间跳指向私网时在发起请求前就被拒。
"""

from __future__ import annotations

import asyncio
import ipaddress
import unittest

import httpx
from core.tools import ToolExecutor, _UnsafeToolUrlError


def _build_executor() -> ToolExecutor:
    return ToolExecutor(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        lambda *args, **kwargs: None,
        {},
    )


class Ipv6TranslationFoldingTests(unittest.TestCase):
    """`_fold_ipv4_translation` 从四种形态里取出正确的 IPv4。"""

    CASES: list[tuple[str, str | None]] = [
        ("::ffff:169.254.169.254", "169.254.169.254"),
        ("::ffff:127.0.0.1", "127.0.0.1"),
        # getaddrinfo 对 fake-IP 主机实测会返回 `::ffff:0:c612:6a`
        # （多一段 :0:，非标准 ipv4_mapped 形状，`.ipv4_mapped` 返回 None）
        ("::ffff:0:c612:6a", "198.18.0.106"),
        ("2002:7f00:1::", "127.0.0.1"),
        ("64:ff9b::7f00:1", "127.0.0.1"),
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "192.0.2.45"),
        # 不折叠的形态
        ("2408:4003:1234::1", None),
        ("8.8.8.8", None),
    ]

    def test_embedded_ipv4_is_extracted(self) -> None:
        for addr, want in self.CASES:
            with self.subTest(addr=addr):
                folded = ToolExecutor._fold_ipv4_translation(
                    ipaddress.ip_address(addr)
                )
                self.assertEqual(
                    str(folded) if folded is not None else None,
                    want,
                    addr,
                )


class Ipv6EmbeddedPrivateBlockedTests(unittest.TestCase):
    """嵌入私网 IPv4 的 IPv6 形态必须被 SSRF 判定拒绝。"""

    def test_mapped_loopback_and_metadata_are_blocked(self) -> None:
        for addr in ("::ffff:169.254.169.254", "::ffff:127.0.0.1"):
            with self.subTest(addr=addr):
                self.assertFalse(
                    ToolExecutor._is_public_ip_obj(ipaddress.ip_address(addr)),
                    addr,
                )

    def test_six_to_four_loopback_is_blocked(self) -> None:
        self.assertFalse(
            ToolExecutor._is_public_ip_obj(ipaddress.ip_address("2002:7f00:1::"))
        )

    def test_nat64_loopback_is_blocked(self) -> None:
        self.assertFalse(
            ToolExecutor._is_public_ip_obj(ipaddress.ip_address("64:ff9b::7f00:1"))
        )

    def test_teredo_to_private_is_blocked(self) -> None:
        # 2001:0:4136:e378:8000:63bf:3fff:fdd2 折叠后是 192.0.2.45，
        # 属于文档保留段，不得放行。
        self.assertFalse(
            ToolExecutor._is_public_ip_obj(
                ipaddress.ip_address("2001:0:4136:e378:8000:63bf:3fff:fdd2")
            )
        )


class PublicIpv6StillAllowedTests(unittest.TestCase):
    """真实公网 IPv6 不受折叠影响。"""

    def test_public_ipv6_addresses_are_allowed(self) -> None:
        for addr in ("2408:4003:1234::1", "2606:4700:4700::1111"):
            with self.subTest(addr=addr):
                self.assertTrue(
                    ToolExecutor._is_public_ip_obj(ipaddress.ip_address(addr)),
                    addr,
                )


class RedirectChainHopValidationTests(unittest.TestCase):
    """重定向链逐跳校验：中间跳指向私网在发请求前就被拒。"""

    def test_redirect_into_metadata_endpoint_is_rejected(self) -> None:
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(
                    302,
                    headers={"location": "http://169.254.169.254/latest/meta-data/"},
                )
            self.fail(f"私网跳转不应被请求：{request.url}")

        async def run() -> None:
            executor = _build_executor()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=False,
            ) as client:
                with self.assertRaises(_UnsafeToolUrlError):
                    await executor._safe_public_http_get(
                        client, "https://8.8.8.8/start"
                    )

        asyncio.run(run())
        self.assertEqual(len(calls), 1, "只应发出首跳请求，私网跳转必须被拦截")

    def test_public_redirect_chain_is_followed(self) -> None:
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if str(request.url) == "https://8.8.8.8/a":
                return httpx.Response(
                    302, headers={"location": "https://1.1.1.1/b"}
                )
            return httpx.Response(200, content=b"final-hop-body")

        async def run() -> None:
            executor = _build_executor()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=False,
            ) as client:
                resp = await executor._safe_public_http_get(client, "https://8.8.8.8/a")

            self.assertEqual(resp.content, b"final-hop-body")

        asyncio.run(run())
        self.assertEqual(calls, ["https://8.8.8.8/a", "https://1.1.1.1/b"])


if __name__ == "__main__":
    unittest.main()
