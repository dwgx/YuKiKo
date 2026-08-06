"""ScrapyLLM — 智能网页抓取 + LLM 结构化提取。

将 Scrapy 式的网页爬取能力与 LLM 智能提取结合:
1. scrape_and_extract: 抓取网页 → LLM 按指令提取结构化数据
2. multi_page_scrape: 多页面批量抓取 → 聚合提取
3. smart_summarize: 抓取网页 → LLM 智能摘要

依赖: httpx (已有), 可选 readability-lxml / beautifulsoup4
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

_log = logging.getLogger("yukiko.scrapy_llm")

# ── HTML → 纯文本清洗 ──

_TAG_RE = re.compile(r"<script[^>]*>[\s\S]*?</script>", re.I)
_STYLE_RE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.I)
_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_MULTI_SP_RE = re.compile(r"[ \t]{2,}")


def _html_to_text(html: str, max_len: int = 12000) -> str:
    """粗粒度 HTML → 纯文本，保留段落结构。"""
    text = _TAG_RE.sub("", html)
    text = _STYLE_RE.sub("", text)
    text = _COMMENT_RE.sub("", text)
    # 块级标签换行
    text = re.sub(r"<(?:br|p|div|h[1-6]|li|tr|blockquote)[^>]*>", "\n", text, flags=re.I)
    text = _HTML_TAG_RE.sub("", text)
    # HTML entities
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    text = _MULTI_SP_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "\n...(已截断)"
    return text


def _extract_links(html: str, base_url: str) -> list[dict[str, str]]:
    """从 HTML 中提取所有链接。"""
    links: list[dict[str, str]] = []
    for m in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href, anchor = m.group(1), _HTML_TAG_RE.sub("", m.group(2)).strip()
        if href.startswith(("javascript:", "mailto:", "#")):
            continue
        full_url = urljoin(base_url, href)
        links.append({"url": full_url, "text": anchor[:100]})
    return links[:50]


# ── 数据结构 ──

@dataclass
class ScrapeResult:
    """单页抓取结果。"""
    url: str
    status: int = 0
    title: str = ""
    text: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    ok: bool = True


@dataclass
class ExtractionResult:
    """LLM 提取结果。"""
    url: str
    extracted: str = ""
    raw_text_len: int = 0
    error: str = ""
    ok: bool = True


# ── 核心引擎 ──

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class ScrapyLLM:
    """智能网页抓取 + LLM 提取引擎。

    用法:
        engine = ScrapyLLM(model_client)
        result = await engine.scrape_and_extract(url, "提取文章标题和摘要")
    """

    def __init__(
        self,
        model_client: Any = None,
        timeout: float = 30.0,
        max_text_len: int = 10000,
        llm_max_tokens: int = 2000,
        verify_tls: bool = True,
    ):
        self.model_client = model_client
        self.timeout = timeout
        self.max_text_len = max_text_len
        self.llm_max_tokens = llm_max_tokens
        # 默认开 TLS 校验。原实现硬编码 verify=False，抓回来的内容会被 LLM
        # 摘要进群，等于给中间人一条注入通道。留开关是为了自签证书站点，
        # 关掉时会留 WARNING。
        self._verify_tls = bool(verify_tls)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=_DEFAULT_HEADERS,
                timeout=httpx.Timeout(self.timeout),
                # 手动跟跳转：每一跳都要过 SSRF 校验。
                # follow_redirects=True 时 httpx 在内部跟完，一个指向
                # 169.254.169.254 的跳转会在我们有机会检查之前就已经发出去了。
                follow_redirects=False,
                # TLS 校验默认开。原来是 verify=False —— 抓回来的网页会被 LLM
                # 摘要进群，关掉校验等于任何网络中间人都能替换那段内容。
                # 需要抓自签证书站点的部署可以显式关掉，但会留一条 WARNING。
                verify=self._verify_tls,
            )
            if not self._verify_tls:
                _log.warning(
                    "scrapy_tls_verification_disabled | 抓取内容可被中间人替换，"
                    "而它会被 LLM 摘要进群"
                )
        return self._client

    @staticmethod
    def _reject_internal_target(url: str) -> str:
        """指向内网/环回/链路本地时返回拒绝原因，否则返回空串。

        复用 `core/webui_chat_helpers.py::_is_private_ip` —— 那个实现已经在
        webui 路径上用着（`core/webui.py:2955`），会解析 DNS 后检查
        private/loopback/link-local/reserved，且解析失败按拒绝处理。
        `link_local` 覆盖了 169.254.169.254 这个云元数据端点。

        2026-08-06 两个独立 workflow 都报了这里：防护存在但**只接在 webui 上**，
        四个 scrape_* 工具和 fetch_webpage 都没接 —— 和出站过滤那次同一类
        guard bypass（同一个 sink 有多条路，只有一条设了防护）。
        """

        # 局部 import 避免 utils -> core 的模块级依赖（utils 是被 core 依赖的下层）。
        from core.webui_chat_helpers import _is_private_ip

        try:
            hostname = urlparse(url).hostname or ""
        except Exception:
            return "url_unparseable"
        if not hostname:
            return "url_without_host"
        if _is_private_ip(hostname):
            return f"internal_target:{hostname}"
        return ""

    async def _get_following_redirects(
        self, client: httpx.AsyncClient, url: str, *, max_hops: int = 5
    ) -> httpx.Response | None:
        """逐跳跟随跳转，每一跳都先过 SSRF 校验。None = 被拦。

        必须自己跟跳转：交给 httpx 的 follow_redirects 时，指向内网的那一跳
        在我们能检查之前就已经发出去了，而对云元数据端点来说**请求本身就是危害**。
        """

        current = url
        for hop in range(max_hops):
            reason = self._reject_internal_target(current)
            if reason:
                _log.warning(
                    "scrapy_blocked_internal_target | hop=%d | reason=%s | url=%s",
                    hop,
                    reason,
                    current[:120],
                )
                return None
            resp = await client.get(current)
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            location = resp.headers.get("location", "")
            if not location:
                return resp
            current = str(httpx.URL(current).join(location))
        _log.warning("scrapy_too_many_redirects | url=%s", url[:120])
        return None

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── 抓取 ──

    async def scrape(self, url: str) -> ScrapeResult:
        """抓取单个网页，返回清洗后的纯文本。"""
        try:
            client = await self._get_client()
            resp = await self._get_following_redirects(client, url)
            if resp is None:
                return ScrapeResult(
                    url=url, ok=False, error="blocked_internal_target"
                )
            html = resp.text
            title = ""
            title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            if title_m:
                title = _HTML_TAG_RE.sub("", title_m.group(1)).strip()
            text = _html_to_text(html, self.max_text_len)
            links = _extract_links(html, url)
            return ScrapeResult(
                url=url, status=resp.status_code, title=title,
                text=text, links=links, ok=resp.status_code < 400,
            )
        except Exception as e:
            _log.warning("scrape_error | url=%s | err=%s", url, e)
            return ScrapeResult(url=url, error=str(e), ok=False)

    # ── LLM 提取 ──

    async def scrape_and_extract(
        self,
        url: str,
        instruction: str,
        system_hint: str = "",
    ) -> ExtractionResult:
        """抓取网页 + LLM 按指令提取结构化信息。"""
        page = await self.scrape(url)
        if not page.ok:
            return ExtractionResult(url=url, error=page.error, ok=False)
        return await self._llm_extract(page, instruction, system_hint)

    async def smart_summarize(
        self,
        url: str,
        focus: str = "",
    ) -> ExtractionResult:
        """抓取网页 + LLM 智能摘要。"""
        instruction = "请对以下网页内容生成简洁的中文摘要，保留关键信息。"
        if focus:
            instruction += f"\n重点关注: {focus}"
        return await self.scrape_and_extract(url, instruction)

    async def multi_page_scrape(
        self,
        urls: list[str],
        instruction: str,
        max_concurrent: int = 3,
    ) -> list[ExtractionResult]:
        """批量抓取多个页面并提取。"""
        sem = asyncio.Semaphore(max_concurrent)

        async def _one(u: str) -> ExtractionResult:
            async with sem:
                return await self.scrape_and_extract(u, instruction)

        return await asyncio.gather(*[_one(u) for u in urls[:8]])

    async def extract_structured(
        self,
        url: str,
        schema_desc: str,
    ) -> ExtractionResult:
        """抓取网页 + 按 schema 描述提取结构化 JSON。"""
        instruction = (
            f"从以下网页内容中提取结构化数据，按照这个格式输出 JSON:\n{schema_desc}\n"
            "只输出 JSON，不要其他文字。如果某个字段找不到，填 null。"
        )
        return await self.scrape_and_extract(url, instruction)

    async def find_and_follow(
        self,
        url: str,
        link_instruction: str,
        extract_instruction: str,
    ) -> list[ExtractionResult]:
        """抓取页面 → LLM 选择相关链接 → 跟进抓取提取。"""
        page = await self.scrape(url)
        if not page.ok or not page.links:
            return [ExtractionResult(url=url, error=page.error or "no_links", ok=False)]

        # LLM 选择链接
        links_text = "\n".join(
            f"{i+1}. [{l['text']}]({l['url']})" for i, l in enumerate(page.links[:20])
        )
        select_prompt = (
            f"以下是网页中的链接列表:\n{links_text}\n\n"
            f"任务: {link_instruction}\n"
            "请只输出你选择的链接编号（逗号分隔），最多5个。例如: 1,3,5"
        )
        selected_text = await self._llm_chat(select_prompt)
        # 解析编号
        nums = [int(x.strip()) for x in re.findall(r"\d+", selected_text)]
        selected_urls = []
        for n in nums[:5]:
            if 1 <= n <= len(page.links):
                selected_urls.append(page.links[n - 1]["url"])

        if not selected_urls:
            return [ExtractionResult(url=url, extracted="未找到匹配的链接", ok=True)]

        return await self.multi_page_scrape(selected_urls, extract_instruction)

    # ── 内部方法 ──

    async def _llm_extract(
        self,
        page: ScrapeResult,
        instruction: str,
        system_hint: str = "",
    ) -> ExtractionResult:
        """用 LLM 从页面文本中提取信息。"""
        if not self.model_client:
            # 无 LLM 时直接返回原文摘要
            return ExtractionResult(
                url=page.url,
                extracted=f"[{page.title}]\n{page.text[:2000]}",
                raw_text_len=len(page.text),
                ok=True,
            )
        prompt = f"{instruction}\n\n---\n网页标题: {page.title}\n网页内容:\n{page.text}"
        try:
            result = await self._llm_chat(prompt, system_hint)
            return ExtractionResult(
                url=page.url, extracted=result,
                raw_text_len=len(page.text), ok=True,
            )
        except Exception as e:
            _log.warning("llm_extract_error | url=%s | err=%s", page.url, e)
            return ExtractionResult(url=page.url, error=str(e), ok=False)

    async def _llm_chat(self, prompt: str, system: str = "") -> str:
        """调用 LLM 获取文本回复。"""
        if not self.model_client:
            return "(LLM 不可用)"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            result = await self.model_client.chat_text_with_retry(
                messages=messages,
                max_tokens=max(300, int(self.llm_max_tokens)),
            )
            return str(result or "").strip()
        except Exception as e:
            _log.warning("scrapy_llm_chat_error | %s", e)
            return f"(LLM 调用失败: {e})"

