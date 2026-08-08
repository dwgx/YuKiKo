"""fetch_webpage 正文提取回归测试（S2：真正文净化）。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from core.tools import ToolExecutor


async def _dummy_plugin_runner(_name: str, _tool_name: str, _args: dict) -> str:
    return ""


def _make_response(
    url: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        content=content,
        request=httpx.Request("GET", url),
    )


class _FakeAsyncClient:
    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self._responses = responses

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        _ = (exc_type, exc, tb)
        return False

    async def aclose(self) -> None:
        pass

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        response = self._responses.get(str(url))
        if response is None:
            raise AssertionError(f"unexpected url: {url}")
        return response


class _DummyExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__(None, None, _dummy_plugin_runner, {})


_ARTICLE_HTML = """
<html><head>
<title>测试文章标题</title>
<meta name="description" content="这是文章描述。">
<meta property="og:site_name" content="示例站点">
</head><body>
<nav><a href="/menu">菜单</a><span>导航噪音文本</span></nav>
<header><h1>站点头部噪音</h1></header>
<script>var noise = "脚本噪音不应出现";</script>
<style>.noise { color: red; }</style>
<footer>页脚噪音文本</footer>
<aside>侧栏噪音文本</aside>
<div class="sidebar">推荐阅读噪音一 推荐阅读噪音二</div>
<article class="post-content">
  <h2>第一节：真正的内容</h2>
  <p>这是文章第一段正文，包含足够长度的有效信息，用于验证提取是否保留了真正内容。</p>
  <p>这是文章第二段正文，同样包含足够长度的有效信息，应当被提取出来并去重合并。</p>
  <p>这是文章第三段正文，包含一个<a href="/detail/1">详情链接</a>和<a href="/detail/2">另一个链接</a>。</p>
  <blockquote>引用块内容也应当进入正文候选。</blockquote>
  <p>这是文章第四段正文，重复段落不应当重复出现。</p>
</article>
</body></html>
"""


class FetchWebpageExtractRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.executor = _DummyExecutor()

    def test_extract_html_denoises_and_keeps_article_content(self) -> None:
        extract = self.executor._extract_html_summary(_ARTICLE_HTML)

        self.assertEqual(extract.title, "测试文章标题")
        self.assertEqual(extract.summary, "这是文章描述。")
        self.assertEqual(extract.site_name, "示例站点")
        joined = "\n".join(extract.paragraphs)
        self.assertIn("这是文章第一段正文", joined)
        self.assertIn("这是文章第四段正文", joined)
        self.assertNotIn("导航噪音", joined)
        self.assertNotIn("站点头部噪音", joined)
        self.assertNotIn("页脚噪音", joined)
        self.assertNotIn("侧栏噪音", joined)
        self.assertNotIn("脚本噪音", joined)
        self.assertNotIn("推荐阅读噪音", joined)
        self.assertGreaterEqual(len(extract.paragraphs), 3)
        # article 内链接计入，导航链接不计入
        self.assertEqual(extract.links_count, 2)

    def test_extract_html_mobile_page_without_p_tags(self) -> None:
        html = (
            "<html><head><title>移动版页面</title></head><body>"
            "<nav>移动导航菜单</nav>"
            '<div class="content">'
            "手机版正文第一句，没有段落标签。"
            "手机版正文第二句，同样没有段落标签，需要按句切分提取。"
            "</div>"
            "</body></html>"
        )
        extract = self.executor._extract_html_summary(html)

        joined = " ".join(extract.paragraphs)
        self.assertIn("手机版正文第一句", joined)
        self.assertIn("手机版正文第二句", joined)
        self.assertNotIn("移动导航菜单", joined)

    def test_extract_body_truncated_to_2000_chars(self) -> None:
        long_paragraph = "长正文段落" * 200  # 1000 字
        paragraphs_html = "".join(f"<p>{long_paragraph}{idx}。</p>" for idx in range(6))
        html = (
            "<html><head><title>长文</title></head><body>"
            f'<article>{paragraphs_html}</article></body></html>'
        )
        extract = self.executor._extract_html_summary(html)

        self.assertLessEqual(len(extract.body), 2003)  # 截断 + "..."
        self.assertTrue(extract.body.startswith("长正文段落"))
        self.assertLessEqual(len(extract.paragraphs), 6)

    def test_extract_prefers_dense_content_block_over_noise(self) -> None:
        html = (
            "<html><head><title>密度测试</title></head><body>"
            '<div class="related-articles">'
            + "".join(f'<li><a href="/r/{i}">推荐文章链接文本{i}</a></li>' for i in range(30))
            + "</div>"
            '<div class="content">'
            "<p>真正的内容段落，文本密度高且信息量大，应当成为主块。</p>"
            "<p>另一段真正的内容，进一步拉开密度差距。</p>"
            "</div>"
            "</body></html>"
        )
        extract = self.executor._extract_html_summary(html)

        joined = " ".join(extract.paragraphs)
        self.assertIn("真正的内容段落", joined)
        self.assertNotIn("推荐文章链接文本", joined)

    async def test_fetch_webpage_summary_returns_structured_fields(self) -> None:
        executor = _DummyExecutor()
        executor._is_safe_public_http_url = lambda _url: True  # type: ignore[method-assign]
        executor._is_low_signal_web_summary = lambda **_kwargs: False  # type: ignore[method-assign]
        fake_client = _FakeAsyncClient(
            {
                "https://public.example/page": _make_response(
                    "https://public.example/page",
                    200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    content=_ARTICLE_HTML.encode("utf-8"),
                )
            }
        )
        with patch("core.tools.httpx.AsyncClient", return_value=fake_client):
            page = await executor._fetch_webpage_summary("https://public.example/page")

        self.assertIsNotNone(page)
        self.assertEqual(page["title"], "测试文章标题")
        self.assertEqual(page["site_name"], "示例站点")
        self.assertIsInstance(page["links_count"], int)
        self.assertTrue(page["body"])
        self.assertTrue(page["paragraphs"])


if __name__ == "__main__":
    unittest.main()
