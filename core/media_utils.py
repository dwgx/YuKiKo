"""媒体 URL / 本地路径的纯函数工具集。

这些函数原本以 staticmethod 形式散落在 `core/agent.py` / `core/engine.py`
的 God class 里，既不依赖 self、也不依赖实例状态，因此收敛到独立模块。
搬移时保持逻辑完全不变。
"""

from __future__ import annotations

import re

from utils.text import normalize_text

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
