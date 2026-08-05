from __future__ import annotations

import ipaddress
import unittest

from core.tools import _BENCHMARK_FAKE_IP_NETWORKS, ToolExecutor


def _addrinfo(*addresses: str) -> list:
    """伪造 getaddrinfo 的返回形状：只有 index 4 的 sockaddr 被读取。"""

    return [(None, None, None, None, (addr, 0)) for addr in addresses]


class BenchmarkFakeIpRecognitionTests(unittest.TestCase):
    """透明代理（Clash fake-IP 模式）下所有域名都解析进 RFC 2544 基准测试段。

    Python 的 ipaddress 把 198.18.0.0/15 判为 private，于是 SSRF 护栏拒绝一切
    外部域名 —— 实测 bilibili / peps.python.org 全被拦，视频解析完全不可用。
    但那个段不是可路由的真实主机地址，正因如此才被选作 fake-IP 池：解析结果
    不携带目的地信息，既不证明私网也不证明公网。
    """

    def test_ipv4_fake_ip_is_recognized(self) -> None:
        self.assertTrue(
            ToolExecutor._is_benchmark_fake_ip(ipaddress.ip_address("198.18.0.106"))
        )
        self.assertTrue(
            ToolExecutor._is_benchmark_fake_ip(ipaddress.ip_address("198.19.255.255"))
        )

    def test_real_private_targets_are_not_mistaken_for_fake_ip(self) -> None:
        for addr in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "172.16.0.1"):
            with self.subTest(addr):
                self.assertFalse(
                    ToolExecutor._is_benchmark_fake_ip(ipaddress.ip_address(addr))
                )

    def test_public_address_is_not_fake_ip(self) -> None:
        self.assertFalse(
            ToolExecutor._is_benchmark_fake_ip(ipaddress.ip_address("104.16.0.1"))
        )

    def test_ipv4_in_ipv6_wrapper_is_unwrapped(self) -> None:
        """实测 getaddrinfo 同时返回 `::ffff:0:c612:6a` —— 同一个 fake-IP 的
        IPv6 包裹形式，且不是标准 ipv4_mapped（多一段 :0:），`.ipv4_mapped`
        返回 None。第一版补丁漏了这个，导致 fake_ip_only 被判 False 仍然拦。
        """

        wrapped = ipaddress.ip_address("::ffff:0:c612:6a")
        self.assertIsNone(wrapped.ipv4_mapped)
        self.assertTrue(ToolExecutor._is_benchmark_fake_ip(wrapped))

    def test_ipv6_wrapper_of_real_private_address_still_rejected(self) -> None:
        """包裹形式不能变成绕过手段：低 32 位是真实私网时必须仍判为非 fake-IP。"""

        # 0a00:0005 == 10.0.0.5
        self.assertFalse(
            ToolExecutor._is_benchmark_fake_ip(ipaddress.ip_address("::ffff:0:a00:5"))
        )

    def test_benchmark_networks_cover_both_families(self) -> None:
        self.assertIn(ipaddress.ip_network("198.18.0.0/15"), _BENCHMARK_FAKE_IP_NETWORKS)


class ResolutionVerdictTests(unittest.TestCase):
    """`_verdict_from_resolution` 是同步/异步两条路径共用的判定 ——
    它们此前是两份逐字重复的实现。"""

    def _fresh(self) -> ToolExecutor:
        executor = ToolExecutor.__new__(ToolExecutor)
        executor._tool_interface_allow_private_network = False
        executor._url_host_safety_cache = {}
        return executor

    def test_all_fake_ip_resolution_is_allowed(self) -> None:
        executor = self._fresh()
        verdict = executor._verdict_from_resolution(
            "www.bilibili.com", _addrinfo("198.18.0.106", "::ffff:0:c612:6a")
        )
        self.assertTrue(verdict)
        self.assertIs(executor._url_host_safety_cache["www.bilibili.com"], True)

    def test_private_resolution_is_still_rejected(self) -> None:
        executor = self._fresh()
        self.assertFalse(
            executor._verdict_from_resolution("evil.example", _addrinfo("10.0.0.5"))
        )

    def test_mixed_fake_and_private_is_rejected(self) -> None:
        """混合结果不能被 fake-IP 那条放行掉 —— 只要有一个真实私网就必须拒。"""

        executor = self._fresh()
        self.assertFalse(
            executor._verdict_from_resolution(
                "mixed.example", _addrinfo("198.18.0.106", "127.0.0.1")
            )
        )

    def test_public_resolution_is_allowed(self) -> None:
        executor = self._fresh()
        self.assertTrue(
            executor._verdict_from_resolution("cdn.example", _addrinfo("104.16.0.1"))
        )

    def test_empty_resolution_is_rejected(self) -> None:
        executor = self._fresh()
        self.assertFalse(executor._verdict_from_resolution("nowhere.example", []))


class GuardEndToEndTests(unittest.TestCase):
    """护栏整体行为：fake-IP 域名放行，真实私网目标一律拒 ——
    且不需要 `allow_private_network`（那个开关会连 localhost 和云元数据一起放行）。
    """

    def _fresh(self) -> ToolExecutor:
        executor = ToolExecutor.__new__(ToolExecutor)
        executor._tool_interface_allow_private_network = False
        executor._url_host_safety_cache = {}
        return executor

    def test_literal_private_targets_rejected_without_dns(self) -> None:
        executor = self._fresh()
        for url in (
            "http://127.0.0.1:8081/api",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:8081/x",
        ):
            with self.subTest(url):
                self.assertFalse(executor._is_safe_public_http_url(url))

    def test_local_hostnames_rejected(self) -> None:
        executor = self._fresh()
        for url in (
            "http://localhost:8081/x",
            "http://foo.internal/x",
            "http://router.local/x",
            "http://metadata.google.internal/x",
        ):
            with self.subTest(url):
                self.assertFalse(executor._is_safe_public_http_url(url))

    def test_non_http_scheme_rejected(self) -> None:
        executor = self._fresh()
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/1"):
            with self.subTest(url):
                self.assertFalse(executor._is_safe_public_http_url(url))


if __name__ == "__main__":
    unittest.main()
