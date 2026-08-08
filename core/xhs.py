"""小红书（xiaohongshu.com / xhslink.com）图文/视频解析器。

小红书没有公开解析 API，这里走分享页 HTML：

1. xhslink.com 短链先跟随重定向拿到详情页 URL
2. 抓详情页 HTML，优先解析 `window.__INITIAL_STATE__` 里的 noteDetailMap
   （图文多图在 imageList，视频直链在 video.media.stream.h264）
3. 备选 og:image / og:video / og:title 元数据兜底
4. 反爬（登录墙/验证码）或没有可提取数据时返回明确错误，绝不编造内容
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from utils.text import normalize_text

_log = logging.getLogger("yukiko.xhs")

_XHS_HOSTS = ("xiaohongshu.com", "xhslink.com")

# 登录墙 / 验证码特征：出现即判定反爬拦截（页面给了壳但不是内容）
_BLOCKED_CUES = (
    "登录后查看",
    "请先登录",
    "验证码",
    "captcha",
    "verify",
    "login?",
)

_INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL
)

# og 元数据（property 可能在 content 前或后，两种顺序都试）
_OG_PROP_FIRST_RE = re.compile(
    r'<meta[^>]+property=["\'](og:[a-z:]+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_OG_CONTENT_FIRST_RE = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\'](og:[a-z:]+)["\']',
    re.IGNORECASE,
)

_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL)

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class XhsResult:
    """小红书作品解析结果。

    kind: "image"（图文）| "video" | ""（解析失败时为空）
    error: "" 表示成功；否则为 invalid_url / blocked / no_data / timeout /
            network_error / http_<status>
    """

    kind: str = ""
    title: str = ""
    uploader: str = ""
    image_urls: list[str] = field(default_factory=list)
    video_url: str = ""
    source_url: str = ""
    error: str = ""


def is_xhs_url(url: str) -> bool:
    """小红书域名判定（xiaohongshu.com / xhslink.com，含子域）。"""
    target = normalize_text(url)
    if not target or not re.match(r"^https?://", target, flags=re.IGNORECASE):
        return False
    try:
        host = normalize_text(urlparse(target).netloc).lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in _XHS_HOSTS)


def is_xhs_detail_url(url: str) -> bool:
    """详情页判定：xhslink 短链直接 True；详情路径 /explore/xxx、/discovery/item/xxx、/user/profile/xxx。"""
    target = normalize_text(url)
    if not is_xhs_url(target):
        return False
    try:
        parsed = urlparse(target)
        host = normalize_text(parsed.netloc).lower()
        path = normalize_text(parsed.path or "")
    except Exception:
        return False
    if host.endswith("xhslink.com"):
        return bool(path.strip("/"))
    if not path:
        return False
    # 详情路径要求剥掉首尾斜杠后仍有内容（裸 /explore/ 是发现页，不是详情）
    return any(
        path.startswith(prefix) and bool(path[len(prefix):].strip("/"))
        for prefix in ("/explore/", "/discovery/item/", "/user/profile/")
    )


def _clean_image_url(raw: str) -> str:
    """小红书图床 URL 形如 xxx.jpg!nd_dft_wlteh_webp_3，`!` 后是处理参数。

    截掉处理参数拿原图 —— 带参数的版本可能附短时签名，直发 QQ 容易失效。
    """
    url = normalize_text(raw)
    if not url:
        return ""
    return url.split("!", 1)[0]


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _find_meta_content(html: str, prop: str) -> str:
    for pattern in (_OG_PROP_FIRST_RE, _OG_CONTENT_FIRST_RE):
        for match in pattern.finditer(html):
            groups = match.groups()
            if len(groups) == 2:
                first, second = groups
                if normalize_text(first).lower() == prop:
                    return normalize_text(second)
                if normalize_text(second).lower() == prop:
                    return normalize_text(first)
    return ""


def _extract_note_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    """从 __INITIAL_STATE__ 里取出 noteDetailMap 的第一条 note。"""
    if not isinstance(state, dict):
        return None
    note_root = state.get("note")
    if not isinstance(note_root, dict):
        return None
    note_detail_map = note_root.get("noteDetailMap")
    if not isinstance(note_detail_map, dict):
        return None
    for value in note_detail_map.values():
        if not isinstance(value, dict):
            continue
        note = value.get("note")
        if not isinstance(note, dict):
            continue
        if note.get("type") or note.get("title") or note.get("imageList") or note.get("video"):
            return note
    return None


def _extract_images_from_note(note: dict[str, Any]) -> list[str]:
    """图文多图提取：imageList 每项优先 urlDefault，其次 infoList 里最大图。"""
    urls: list[str] = []
    for item in note.get("imageList") or []:
        if not isinstance(item, dict):
            continue
        candidates: list[Any] = [item.get("urlDefault"), item.get("url")]
        info_list = item.get("infoList")
        if isinstance(info_list, list) and info_list:
            for entry in reversed(info_list):
                if isinstance(entry, dict):
                    candidates.append(entry.get("url"))
        for candidate in candidates:
            cleaned = _clean_image_url(str(candidate or ""))
            if cleaned:
                urls.append(cleaned)
                break
    return _dedupe(urls)


def _extract_video_from_note(note: dict[str, Any]) -> str:
    """视频直链提取：h264/h265 的 masterUrl，其次 backupUrls / video.url。"""
    video = note.get("video")
    if not isinstance(video, dict):
        return ""
    media = video.get("media")
    if isinstance(media, dict):
        stream = media.get("stream")
        if isinstance(stream, dict):
            for codec in ("h264", "h265"):
                items = stream.get(codec)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    master_url = normalize_text(str(item.get("masterUrl", "")))
                    if master_url:
                        return master_url
                    backup = item.get("backupUrls")
                    if isinstance(backup, list) and backup:
                        candidate = normalize_text(str(backup[0]))
                        if candidate:
                            return candidate
    raw = normalize_text(str(video.get("url", "")))
    if raw:
        return raw
    consumer = video.get("consumer")
    if isinstance(consumer, dict):
        consumer_url = normalize_text(str(consumer.get("url", "")))
        if consumer_url:
            return consumer_url
    return ""


def parse_xhs_html(html: str, source_url: str) -> XhsResult:
    """从详情页 HTML 提取图文/视频数据（纯函数，便于测试）。"""
    result = XhsResult(source_url=normalize_text(source_url))
    text = html or ""
    if not text:
        result.error = "no_data"
        return result

    lower = text.lower()
    if any(cue in lower for cue in _BLOCKED_CUES):
        result.error = "blocked"
        return result

    state_match = _INITIAL_STATE_RE.search(text)
    if state_match:
        try:
            state = json.loads(state_match.group(1))
        except Exception:
            state = None
        note = _extract_note_from_state(state) if isinstance(state, dict) else None
        if note:
            result.image_urls = _extract_images_from_note(note)
            result.video_url = _extract_video_from_note(note)
            note_type = normalize_text(str(note.get("type", ""))).lower()
            result.title = normalize_text(
                str(note.get("title", "") or note.get("desc", ""))
            )
            user = note.get("user")
            if isinstance(user, dict):
                result.uploader = normalize_text(str(user.get("nickname", "")))
            if result.video_url:
                result.kind = "video"
            elif result.image_urls:
                result.kind = "image"
            if note_type == "video" and result.video_url:
                result.kind = "video"

    # og 元数据兜底：INITIAL_STATE 缺失或该页没走 SPA 渲染时仍能拿到封面/直链
    if not result.image_urls and not result.video_url:
        og_image = _find_meta_content(text, "og:image")
        if og_image:
            result.image_urls = [_clean_image_url(og_image)]
            result.kind = "image"
        og_video = _find_meta_content(text, "og:video") or _find_meta_content(text, "og:video:url")
        if og_video and not result.video_url:
            result.video_url = normalize_text(og_video)
            result.kind = "video"

    if not result.title:
        og_title = _find_meta_content(text, "og:title")
        if og_title:
            result.title = normalize_text(og_title)
    if not result.title:
        title_match = _TITLE_TAG_RE.search(text)
        if title_match:
            result.title = normalize_text(re.sub(r"\s+", " ", title_match.group(1)))

    if not result.error and not result.image_urls and not result.video_url:
        result.error = "no_data"
    return result


async def fetch_xhs_post(url: str, cookie: str = "", timeout: float = 12.0) -> XhsResult:
    """抓取小红书作品详情。

    cookie 非空时注入请求头（配置 video_analysis.xiaohongshu.cookie）。
    短链 xhslink.com 由 follow_redirects 自动跟随到详情页。
    """
    target = normalize_text(url)
    if not target or not re.match(r"^https?://", target, flags=re.IGNORECASE):
        return XhsResult(source_url=target, error="invalid_url")

    headers = {
        "User-Agent": _DESKTOP_UA,
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    cookie_header = normalize_text(cookie)
    if cookie_header:
        headers["Cookie"] = cookie_header

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=6.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            resp = await client.get(target)
            if resp.status_code != 200:
                if resp.status_code in (401, 403):
                    return XhsResult(source_url=target, error="blocked")
                return XhsResult(source_url=target, error=f"http_{resp.status_code}")
            final_url = normalize_text(str(resp.url))
            html = resp.text or ""
    except httpx.TimeoutException:
        return XhsResult(source_url=target, error="timeout")
    except Exception as exc:
        _log.warning("xhs_fetch_error | url=%s | %s", target[:100], str(exc)[:200])
        return XhsResult(source_url=target, error="network_error")

    result = parse_xhs_html(html, final_url or target)
    result.source_url = final_url or target
    return result
