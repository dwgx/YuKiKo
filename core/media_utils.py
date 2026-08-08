"""媒体 URL / 本地路径的纯函数工具集。

这些函数原本以 staticmethod 形式散落在 `core/agent.py` / `core/engine.py`
的 God class 里，既不依赖 self、也不依赖实例状态，因此收敛到独立模块。
搬移时保持逻辑完全不变。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from utils.text import clip_text, normalize_text

from core.text_utils import _RE_BARE_WEB_HOST

_RE_URL_EXTRACT = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_RE_IMAGE_EXT = re.compile(
    r"\.(?:jpg|jpeg|png|gif|webp|bmp|heic|heif|avif)(?=$|[?#&!@_/])"
)
_RE_VIDEO_EXT = re.compile(r"\.(?:mp4|webm|mov|m4v)(?:\?|$)")


def strip_trailing_url_noise(url: str) -> str:
    target = normalize_text(url).strip().rstrip(").,，。!?！？】》」』")
    if not target:
        return ""
    suffix_re = re.compile(
        r"(?:解析|分析|看看|看下|看一下|下载|下載|发我|發我|发出来|發出來|"
        r"转发|轉發|总结|總結|解说|解說|parse|analyze|download)+$",
        re.IGNORECASE,
    )
    for _ in range(4):
        cleaned = suffix_re.sub("", target).rstrip(").,，。!?！？】》」』")
        if cleaned == target:
            break
        target = cleaned
    return target


def extract_first_url(text: str) -> str:
    m = _RE_URL_EXTRACT.search(text or "")
    if not m:
        return ""
    return strip_trailing_url_noise(m.group(0))


def looks_like_image_url(url: str) -> bool:
    target = normalize_text(url).lower()
    if not target:
        return False
    if target.startswith("data:image/"):
        return True
    if _RE_IMAGE_EXT.search(target):
        return True
    # QQ/NT 常见图片下载链接没有文件后缀
    if "multimedia.nt.qq.com.cn/download" in target:
        return True
    return False


def looks_like_video_url(url: str) -> bool:
    target = normalize_text(url).lower()
    if not target:
        return False
    if _RE_VIDEO_EXT.search(target):
        return True
    return any(
        host in target
        for host in (
            "bilibili.com/video/",
            "b23.tv/",
            "douyin.com/",
            "iesdouyin.com/",
            "kuaishou.com/",
            "acfun.cn/v/ac",
            "acfun.com/v/ac",
            "m.acfun.cn/v/",
            "v.qq.com/",
            "m.v.qq.com/",
            "qq.com/x/",
            "youku.com/v_show/",
            "iqiyi.com/",
            "mgtv.com/",
        )
    )


def is_placeholder_media_url(url: str) -> bool:
    value = normalize_text(url).lower()
    if not value:
        return False
    if not (value.startswith("http://") or value.startswith("https://")):
        return False
    blocked_tokens = (
        "example.com",
        "example.org",
        "example.net",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        ".invalid/",
    )
    return any(token in value for token in blocked_tokens)


def is_local_media_path(url: str) -> bool:
    value = normalize_text(url)
    if not value:
        return False
    return not value.lower().startswith(("http://", "https://"))


def normalize_local_media_path(path: str) -> str:
    value = normalize_text(path).strip()
    if not value:
        return ""
    if value.lower().startswith(("http://", "https://")):
        return ""
    return value.replace("\\", "/").lower()


def extract_first_image_url(text: str) -> str:
    for match in _RE_URL_EXTRACT.finditer(text or ""):
        url = strip_trailing_url_noise(match.group(0))
        if url and looks_like_image_url(url):
            return url
    return ""


def extract_first_video_url(text: str) -> str:
    url = extract_first_url(text)
    if url and looks_like_video_url(url):
        return url
    return ""


def extract_first_web_url(text: str) -> str:
    explicit = extract_first_url(text)
    if explicit:
        return explicit
    content = normalize_text(text)
    if not content:
        return ""
    match = _RE_BARE_WEB_HOST.search(content)
    if not match:
        return ""
    url = normalize_text(match.group(1)).rstrip(").,，。!?！？")
    if not url:
        return ""
    return f"https://{url}"


def looks_like_webpage_fetch_request(text: str) -> bool:
    url = extract_first_web_url(text)
    if not url:
        return False
    if looks_like_image_url(url) or looks_like_video_url(url):
        return False
    content = normalize_text(text).lower()
    if not content:
        return False
    cues = (
        "网站",
        "网页",
        "页面",
        "官网",
        "打开",
        "看看",
        "看下",
        "帮我看",
        "分析",
        "介绍",
        "是什么",
        "安全吗",
        "看",
        "website",
        "webpage",
        "site",
        "page",
    )
    return any(cue in content for cue in cues)


def text_has_image_hint(text: str) -> bool:
    norm = normalize_text(text).lower()
    if not norm:
        return False
    if "image:" in norm:
        return True
    url = extract_first_url(norm)
    return bool(url and looks_like_image_url(url))


def normalize_media_url(url: str) -> str:
    value = normalize_text(url).strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        host = parsed.netloc.lower()
        path = parsed.path or ""
        query = parsed.query or ""
        # 去掉 fragment；query 保留，避免同路径不同资源被误合并。
        return f"{parsed.scheme.lower()}://{host}{path}" + (
            f"?{query}" if query else ""
        )
    except Exception:
        return ""


def extract_media_refs_from_segments(segments: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        data = seg.get("data", {}) or {}
        if not isinstance(data, dict):
            continue
        for key in ("memory_data_uri", "url", "file", "path"):
            value = normalize_text(str(data.get(key, "")))
            if value:
                refs.append(value)
    return refs


def extract_urls_from_text(text: str) -> list[str]:
    content = normalize_text(text)
    if not content:
        return []
    urls = re.findall(
        r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
        content,
        flags=re.IGNORECASE,
    )
    out: list[str] = []
    seen: set[str] = set()
    for item in urls:
        value = normalize_text(item).rstrip("，。！？!?,.;:)")
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def extract_first_image_url_from_text(text: str) -> str:
    urls = extract_urls_from_text(text)
    for url in urls:
        lower = url.lower()
        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", lower):
            return url
        if "multimedia.nt.qq.com.cn" in lower:
            return url
    return ""


def extract_first_video_url_from_text(text: str) -> str:
    content = normalize_text(text)
    urls = extract_urls_from_text(content)
    for url in urls:
        lower = url.lower()
        if re.search(r"\.(?:mp4|webm|mov|m4v)(?:\?|$)", lower):
            return url
        if any(
            host in lower
            for host in (
                "bilibili.com/video/",
                "b23.tv/",
                "douyin.com/",
                "kuaishou.com/",
                "acfun.cn/v/ac",
                "acfun.com/v/ac",
                "m.acfun.cn/v/",
                "v.qq.com/",
                "m.v.qq.com/",
                "iqiyi.com/",
                "iq.com/",
                "qiyi.com/",
                "youtube.com/",
                "youtu.be/",
                "tiktok.com/",
                "ixigua.com/",
            )
        ):
            return url
    bv_match = re.search(r"\b(BV[0-9A-Za-z]{10})\b", content, flags=re.IGNORECASE)
    if bv_match:
        return f"https://www.bilibili.com/video/{bv_match.group(1)}"
    return ""


def is_passive_multimodal_text(text: str) -> bool:
    content = normalize_text(text)
    if not content:
        return False
    if re.fullmatch(
        r"(?:\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?\]\s*)+",
        content,
        flags=re.IGNORECASE,
    ):
        return True
    return (
        content.startswith("MULTIMODAL_EVENT")
        or content.startswith("用户发送多模态消息：")
        or content.startswith("用户@了你并发送多模态消息：")
        or content.lower().startswith("user sent multimodal message:")
        or content.lower().startswith(
            "user mentioned bot and sent multimodal message:"
        )
    )


def extract_multimodal_user_text(text: str) -> str:
    content = normalize_text(text)
    if not content:
        return ""
    content = re.sub(
        r"\bMULTIMODAL_EVENT(?:_AT)?\b", " ", content, flags=re.IGNORECASE
    )
    content = content.replace("用户发送多模态消息：", " ").replace(
        "用户@了你并发送多模态消息：", " "
    )
    content = content.replace("user sent multimodal message:", " ").replace(
        "user mentioned bot and sent multimodal message:",
        " ",
    )
    content = re.sub(
        r"\[(?:image|video|record|audio|forward|face|at|reply)(?::[^\]]*)?\]",
        " ",
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(
        r"\b(?:image|video|record|audio|forward)\s*:\s*\S+",
        " ",
        content,
        flags=re.IGNORECASE,
    )
    content = normalize_text(content)
    parts = content.split()
    while parts and not re.search(r"[A-Za-z0-9一-龥]", parts[0]):
        parts.pop(0)
    return normalize_text(" ".join(parts))


def build_media_summary(
    raw_segments: list[dict[str, Any]], limit: int = 8
) -> list[str]:
    items: list[str] = []
    for seg in raw_segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = normalize_text(str(seg.get("type", ""))).lower()
        if not seg_type:
            continue
        data = seg.get("data", {}) or {}
        if seg_type in {"text", "at", "reply"}:
            continue
        if seg_type == "image":
            url = normalize_text(str(data.get("url", "")))
            data_uri = normalize_text(str(data.get("memory_data_uri", "")))
            summary = normalize_text(str(data.get("summary", ""))).lower()
            file_name = normalize_text(str(data.get("file", ""))).lower()
            sub_type = normalize_text(str(data.get("sub_type", ""))).lower()
            image_prefix = (
                "image:animated"
                if (
                    sub_type == "1"
                    or file_name.endswith(".gif")
                    or "gif" in summary
                    or "动画表情" in summary
                )
                else "image"
            )
            if data_uri.startswith("data:image"):
                items.append(f"{image_prefix}:base64:{clip_text(data_uri, 80)}")
            else:
                items.append(f"{image_prefix}:{clip_text(url or 'no_url', 80)}")
        elif seg_type == "video":
            url = normalize_text(str(data.get("url", "")))
            items.append(f"video:{clip_text(url or 'no_url', 80)}")
        elif seg_type in {"record", "audio"}:
            url = normalize_text(str(data.get("url", "")))
            items.append(f"audio:{clip_text(url or 'no_url', 80)}")
        elif seg_type == "forward":
            items.append("forward:message")
        else:
            items.append(seg_type)
        if len(items) >= max(1, limit):
            break
    return items
