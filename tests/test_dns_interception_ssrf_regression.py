"""DNS 拦截段不能让 SSRF 判定误伤真实站点。

## 缺陷（2026-08-06，机器人自己的日志里有真实受害记录）

Python 的 `ipaddress` 把 `198.18.0.0/15` 判为 `is_private=True`，但那是
**RFC 2544 基准测试段**，不是真实内网。VPN / 代理 / 企业网关会把公网域名
解析到这个段做流量拦截，于是所有真实站点都被 SSRF 判定拒掉。

实测业主本机：

```
v.douyin.com      -> 198.18.2.68    判为内网
www.bilibili.com  -> 198.18.0.54    判为内网
www.zhihu.com     -> 198.18.2.70    判为内网
music.163.com     -> 198.18.2.71    判为内网
github.com        -> 198.18.0.25    判为内网
```

**这不是测试环境的假象。** 机器人日志 trace `118886-4-6b429ed2`（2026-08-05，
早于本轮任何改动）：群友发抖音分享链接，`parse_video` 返回
「这个视频链接命中了安全限制（内网/本地地址不可访问）」，模型随后把同一 URL
重试 4 次、think 4 次，烧完 8 步 54 秒，`external_fact_ok=0`，零产出。

两处判定都受影响：
* `core/webui_chat_helpers.py::_is_private_ip`（webui + scrape_* 工具）
* `core/tools.py::_is_public_ip_obj`（parse_video / 搜索侧）

## 为什么排除这个段不削弱防护

* 云元数据端点 `169.254.169.254` 属 `link_local`，是独立的一条检查，不受影响
* loopback / `10.0.0.0/8` / `192.168.0.0/16` / `172.16.0.0/12` 全部照旧拦截
* `198.18.0.0/15` 是基准测试保留段，正常内网服务不会部署在这里

替代方案是打开 `_tool_interface_allow_private_network`，但那会**整个关掉**
校验、真的开放 SSRF —— 比精确排除一个非内网段危险得多。
"""

from __future__ import annotations

import ipaddress
import unittest

from core.tools import ToolExecutor
from core.webui_chat_helpers import _is_dns_interception_range, _is_private_ip
from utils.scrapy_llm import ScrapyLLM


class InterceptionRangeIsRecognisedTests(unittest.TestCase):
    def test_rfc2544_range_is_recognised(self) -> None:
        for addr in ("198.18.0.1", "198.18.2.68", "198.19.255.254"):
            with self.subTest(addr=addr):
                self.assertTrue(
                    _is_dns_interception_range(ipaddress.ip_address(addr)), addr
                )

    def test_real_private_ranges_are_not_recognised_as_interception(self) -> None:
        """不能把真内网也当成拦截段放行。"""

        for addr in (
            "10.0.0.5",
            "192.168.1.1",
            "172.16.0.1",
            "127.0.0.1",
            "169.254.169.254",
            "198.17.255.255",  # 紧邻下界
            "198.20.0.0",      # 紧邻上界
        ):
            with self.subTest(addr=addr):
                self.assertFalse(
                    _is_dns_interception_range(ipaddress.ip_address(addr)), addr
                )

    def test_non_ip_input_is_handled(self) -> None:
        for value in (None, "", "not-an-ip", 123):
            with self.subTest(value=value):
                self.assertFalse(_is_dns_interception_range(value))


class InternalTargetsStillBlockedTests(unittest.TestCase):
    """核心安全断言：排除拦截段之后，真内网仍必须全部拦住。"""

    def test_loopback_and_private_hosts_are_blocked(self) -> None:
        for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.1", "172.16.0.1", "::1"):
            with self.subTest(host=host):
                self.assertTrue(_is_private_ip(host), host)

    def test_cloud_metadata_endpoint_is_blocked(self) -> None:
        """169.254.169.254 属 link_local，与本次排除无关，必须仍被拦。"""

        self.assertTrue(_is_private_ip("169.254.169.254"))

    def test_scrape_still_blocks_metadata(self) -> None:
        scraper = ScrapyLLM()
        self.assertTrue(
            scraper._reject_internal_target("http://169.254.169.254/latest/meta-data/")
        )


class PublicIpObjectClassifierTests(unittest.TestCase):
    """`core/tools.py::_is_public_ip_obj`（parse_video 侧）同步修好。"""

    def test_interception_range_counts_as_public(self) -> None:
        self.assertTrue(
            ToolExecutor._is_public_ip_obj(ipaddress.ip_address("198.18.2.68")),
            "parse_video 侧仍会把 DNS 拦截段判成内网 —— 抖音/B站链接会被拒",
        )

    def test_real_private_is_still_not_public(self) -> None:
        for addr in ("10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.169.254", "::1"):
            with self.subTest(addr=addr):
                self.assertFalse(
                    ToolExecutor._is_public_ip_obj(ipaddress.ip_address(addr)), addr
                )

    def test_genuine_public_ip_is_public(self) -> None:
        for addr in ("8.8.8.8", "1.1.1.1", "223.5.5.5"):
            with self.subTest(addr=addr):
                self.assertTrue(
                    ToolExecutor._is_public_ip_obj(ipaddress.ip_address(addr)), addr
                )


class BothGuardsShareTheSameExclusionTests(unittest.TestCase):
    """两处判定必须用同一份定义，否则会各自漂移。

    本仓已有两份权限清单漂移的先例（registry 与 agent 的 _group_admin_tools）。
    """

    def test_tools_delegates_to_the_shared_helper(self) -> None:
        import inspect

        src = inspect.getsource(ToolExecutor._is_public_ip_obj)
        self.assertIn(
            "_is_dns_interception_range",
            src,
            "core/tools.py 自己写了一份判定 —— 会和 webui_chat_helpers 那份漂移",
        )


if __name__ == "__main__":
    unittest.main()
