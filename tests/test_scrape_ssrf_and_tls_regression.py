"""scrape_* 工具必须拦内网目标、且必须校验 TLS。

## 两个缺陷（2026-08-06，两个独立 workflow 都报到 utils/scrapy_llm.py）

**(a) SSRF：防护存在但没接上。** 仓库里有现成的
`core/webui_chat_helpers.py::_is_private_ip` —— 解析 DNS 后检查
private/loopback/link-local/reserved，解析失败按拒绝处理（`link_local` 正好覆盖
`169.254.169.254` 这个云元数据端点）。但它**只被 `core/webui.py:2955` 用**：

```
grep -rn "_is_private_ip" core/ utils/
  core/webui_chat_helpers.py:572:  def _is_private_ip(...)
  core/webui.py:2931 / :2955       ← 唯二两个使用点
  core/agent_tools_web.py          0 处
  utils/scrapy_llm.py              0 处
```

四个 `scrape_*` 工具（scrape_extract / scrape_summarize / scrape_structured /
scrape_follow_links）全部经 `ScrapyLLM.scrape()`，那里没有任何校验。
和出站敏感词过滤那次是同一类 guard bypass：同一个 sink 有多条路，只有一条设了防护。

而且 navigator 提示词恰好把 `scrape_summarize` 写成「`fetch_webpage` 拒绝内网 URL
之后的重试方案」—— 等于给模型指了一条绕过路径。

**(b) TLS 校验被关掉。** `_get_client` 里硬编码 `verify=False`。抓回来的网页会被
LLM 摘要**发进群**，关掉证书校验等于任何网络中间人都能替换那段内容。
CLAUDE.md 明确要求「Verify TLS certificates」。

## 跳转必须逐跳校验

原来是 `follow_redirects=True`。只校验初始 URL 不够 —— 一个指向
`169.254.169.254` 的 302 会在我们有机会检查之前就被 httpx 发出去，
而对云元数据端点来说**请求本身就是危害**。改成手动跟跳转，每跳先校验。

## 这个测试为什么用 IP 字面量而不是域名

本机沙箱把**所有**公网域名都解析到 `198.18.x.x`（RFC 2544 基准测试段），
而 Python 的 `ipaddress` 把它归为 `is_private=True`。所以在这里用
`example.com` 测「应放行」会假失败 —— 我第一次验证就撞上了，
`example.com` 和 `bilibili.com` 双双被判内网。

用 IP 字面量可以绕开 DNS，让断言只考验分类逻辑本身。
"""

from __future__ import annotations

import asyncio
import inspect
import unittest

from utils.scrapy_llm import ScrapyLLM


class InternalTargetsAreRejectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = ScrapyLLM()

    def test_cloud_metadata_endpoint_is_rejected(self) -> None:
        """最重要的一条：云元数据端点会泄露实例凭证。"""

        reason = self.scraper._reject_internal_target(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
        )
        self.assertTrue(reason, "云元数据端点没被拦")
        self.assertIn("169.254.169.254", reason)

    def test_loopback_and_private_ranges_are_rejected(self) -> None:
        for url in (
            "http://127.0.0.1:8081/api/webui",   # 本机 WebUI，带 token 的那个
            "http://localhost/x",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://[::1]/",
            "http://0.0.0.0/",
        ):
            with self.subTest(url=url):
                self.assertTrue(self.scraper._reject_internal_target(url), url)

    def test_unparseable_or_hostless_urls_are_rejected(self) -> None:
        """拿不到 host 时按拒绝处理（fail closed）。"""

        for url in ("", "not a url", "file:///etc/passwd", "http://"):
            with self.subTest(url=url):
                self.assertTrue(self.scraper._reject_internal_target(url), url)


class PublicTargetsAreAllowedTests(unittest.TestCase):
    """反向：拦得过宽会打断全部四个 scrape 工具。

    用 IP 字面量绕开本机沙箱的 DNS 拦截（见模块 docstring）。
    """

    def setUp(self) -> None:
        self.scraper = ScrapyLLM()

    def test_public_ip_literals_pass(self) -> None:
        for url in (
            "http://8.8.8.8/",
            "http://1.1.1.1/",
            "https://93.184.215.14/",
            "http://223.5.5.5/",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    self.scraper._reject_internal_target(url), "", url
                )


class ScrapeRefusesBlockedTargetsEndToEndTests(unittest.TestCase):
    def test_scrape_returns_blocked_error_without_fetching(self) -> None:
        scraper = ScrapyLLM()
        result = asyncio.run(scraper.scrape("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "blocked_internal_target")


class TlsVerificationIsOnByDefaultTests(unittest.TestCase):
    def test_default_verifies_tls(self) -> None:
        self.assertTrue(
            ScrapyLLM()._verify_tls,
            "TLS 校验默认关着 —— 抓回来的内容会被 LLM 摘要进群，"
            "等于给中间人一条注入通道",
        )

    def test_client_passes_the_flag_through(self) -> None:
        """用 AST 读实参，不用子串匹配源码。

        第一版写的是 `assertNotIn("verify=False", src)`，结果匹配到了解释这段
        历史的**注释**里的 `verify=False`，测试假失败。判据要落在语法结构上。
        """

        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(ScrapyLLM._get_client)))
        verify_args = [
            kw.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "verify"
        ]
        self.assertTrue(verify_args, "_get_client 没给 httpx 传 verify 参数")
        for value in verify_args:
            rendered = ast.unparse(value)
            self.assertNotEqual(
                rendered, "False", "还硬编码着 verify=False"
            )
            self.assertIn(
                "_verify_tls", rendered, f"verify 不是来自开关: {rendered}"
            )

    def test_opt_out_is_possible_but_logged(self) -> None:
        """自签证书站点仍可关掉，但必须留痕。"""

        scraper = ScrapyLLM(verify_tls=False)
        self.assertFalse(scraper._verify_tls)
        src = inspect.getsource(ScrapyLLM._get_client)
        self.assertIn("scrapy_tls_verification_disabled", src, "关掉时没有告警")


class RedirectsAreCheckedPerHopTests(unittest.TestCase):
    """只校验初始 URL 不够：指向内网的那一跳会先被发出去。"""

    def test_client_does_not_auto_follow_redirects(self) -> None:
        src = inspect.getsource(ScrapyLLM._get_client)
        self.assertIn(
            "follow_redirects=False",
            src,
            "httpx 自动跟跳转时，指向内网的那一跳在校验之前就发出去了",
        )

    def test_manual_redirect_loop_validates_each_hop(self) -> None:
        src = inspect.getsource(ScrapyLLM._get_following_redirects)
        self.assertIn("_reject_internal_target", src, "逐跳循环里没有校验")

    def test_redirect_loop_has_a_hop_ceiling(self) -> None:
        src = inspect.getsource(ScrapyLLM._get_following_redirects)
        self.assertIn("max_hops", src, "没有跳转次数上限，可被跳转环拖死")


class GuardReusesTheExistingImplementationTests(unittest.TestCase):
    """不要写第二份 SSRF 判定 —— 两份会各自漂移。"""

    def test_reject_helper_delegates_to_shared_is_private_ip(self) -> None:
        src = inspect.getsource(ScrapyLLM._reject_internal_target)
        self.assertIn(
            "_is_private_ip",
            src,
            "没复用 core/webui_chat_helpers 里已有的实现",
        )


if __name__ == "__main__":
    unittest.main()
