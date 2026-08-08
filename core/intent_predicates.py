"""意图判断纯函数集合（E1 从 core/engine.py 收敛而来）。

只放无 self / 无实例状态依赖的纯函数：同输入必同输出，无副作用。
engine.py 保留同名 `_looks_like_*` / `_extract_topic_terms_for_memory` 薄转发，
外部（含既有测试）仍按 `YukikoEngine._looks_like_*` 调用，行为不变。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from core import text_utils
from utils.text import normalize_text, tokenize


def looks_like_video_text_only_intent(text: str) -> bool:
    return text_utils.has_control_token(
        text, "output=text", "mode=text", "text-only", "/text"
    )


def looks_like_summary_followup(text: str) -> bool:
    return text_utils.has_control_token(
        text, "/summary", "/summarize", "mode=summary", "output=summary"
    )


def looks_like_resend_followup(text: str) -> bool:
    return text_utils.has_control_token(text, "/resend", "/retry", "mode=resend")


def looks_like_choice_next(text: str) -> bool:
    return text_utils.has_control_token(text, "/next", "page=next")


def looks_like_choice_prev(text: str) -> bool:
    return text_utils.has_control_token(text, "/prev", "page=prev")


def looks_like_source_trace_followup(text: str) -> bool:
    return text_utils.has_control_token(
        text, "/source", "/sources", "mode=sources"
    )


def looks_like_sticker_request(text: str) -> bool:
    return text_utils.has_control_token(text, "/sticker", "/emoji", "/meme")


def looks_like_choice_prompt_text(text: str) -> bool:
    _ = text
    # “回复数字/选第几个”链路已下线。
    return False


def looks_like_image_url(url: str) -> bool:
    value = normalize_text(url).lower()
    if not value:
        return False

    if "multimedia.nt.qq.com.cn" in value:
        return True

    return bool(re.search(r"\.(?:jpg|jpeg|png|gif|webp|bmp)(?=$|[?#&!@_/])", value))


def looks_like_video_url(url: str) -> bool:
    value = normalize_text(url).lower()
    if not value:
        return False

    return bool(
        re.search(r"\.(?:mp4|mov|webm|m4v)(?:\?|$)", value)
        or any(
            host in value
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
        )
    )


def extract_topic_terms_for_memory(text: str, max_terms: int = 6) -> list[str]:
    content = normalize_text(text)
    if not content or max_terms <= 0:
        return []

    out: list[str] = []
    seen: set[str] = set()
    strip_chars = "`\"'[](){}<>.,;:!?\uFF0C\u3002\uFF1F\uFF01\uFF1A"

    def add_candidate(raw: str) -> None:
        item = normalize_text(str(raw)).strip(strip_chars)
        if not item:
            return
        lower = item.lower()
        if lower in seen:
            return
        if lower.startswith("/") or "=" in lower:
            return
        if re.search(r"https?://", lower, flags=re.IGNORECASE):
            return
        if re.fullmatch(r"[1-9]\d*", item):
            return
        if re.search(r"[a-z0-9]", lower):
            compact = re.sub(r"[^a-z0-9_.-]+", "", lower)
            if len(compact) < 3:
                return
        elif re.fullmatch(r"[\u4e00-\u9fff]+", item):
            if len(item) < 3:
                return
        elif len(item) < 3:
            return
        seen.add(lower)
        out.append(item)

    explicit_patterns = (
        r"`([^`]{2,80})`",
        r"\*\*([^*]{2,80})\*\*",
        r"[\u201c\"]([^\u201d\"]{2,80})[\u201d\"]",
        r"\u300a([^\u300b]{2,80})\u300b",
    )
    for pattern in explicit_patterns:
        for raw in re.findall(pattern, content):
            add_candidate(raw)
            if len(out) >= max_terms:
                return out[:max_terms]

    for token in tokenize(content):
        add_candidate(token)
        if len(out) >= max_terms:
            break

    return out[:max_terms]


def looks_like_ambiguous_link_memory_query(text: str) -> bool:
    content = normalize_text(text)
    if not content:
        return False

    if re.search(r"https?://", content, flags=re.IGNORECASE):
        return False

    if not text_utils.has_control_token(
        text, "/link", "/url", "type=link", "type=url", "mode=url"
    ):
        return False

    cleaned = re.sub(r"(?i)(?<!\S)/(?:link|url)\b", " ", content)
    cleaned = re.sub(r"(?i)\b(?:type|mode)\s*=\s*(?:link|url)\b", " ", cleaned)
    return not extract_topic_terms_for_memory(cleaned, max_terms=3)


def looks_like_short_context_sensitive_query(text: str) -> bool:
    content = normalize_text(text)
    if not content:
        return False

    if re.search(r"https?://", content, flags=re.IGNORECASE):
        return False

    if re.fullmatch(r"[?!.,\uFF1F\uFF01\uFF0C\u3002]+", content):
        return True

    if len(content) > 24:
        return False

    if extract_topic_terms_for_memory(content, max_terms=2):
        return False

    compact = re.sub(r"\s+", "", content)
    if len(compact) <= 8:
        return True

    tokens = [normalize_text(str(token)) for token in tokenize(content)]
    tokens = [token for token in tokens if token]
    return len(tokens) <= 2


def looks_like_recent_bot_reply_echo(
    text: str, recent_bot_replies: list[str]
) -> bool:
    incoming = text_utils.normalize_reply_echo_text(text)
    if len(incoming) < 80:
        return False

    for reply in recent_bot_replies[-3:]:
        candidate = text_utils.normalize_reply_echo_text(reply)
        if len(candidate) < 60:
            continue

        shorter = min(len(incoming), len(candidate))
        longer = max(len(incoming), len(candidate))
        if shorter < 60 or shorter / max(longer, 1) < 0.72:
            continue

        if candidate in incoming and len(incoming) - len(candidate) <= 48:
            return True

        if incoming in candidate and len(candidate) - len(incoming) <= 24:
            return True

        if SequenceMatcher(None, incoming[:2000], candidate[:2000]).ratio() >= 0.92:
            return True

    return False
