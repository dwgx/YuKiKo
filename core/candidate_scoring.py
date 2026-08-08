"""视频解析候选评分与多来源聚合（移植 Reflection_King score_candidate 思路）。

纯函数模块：不依赖网络 / 子进程 / 配置，测试友好。
"""

from __future__ import annotations

import re
from typing import Any

# kind 基础分：越接近「可直接播放的视频直链」越高。
KIND_BASE_SCORES = {
    "video": 78,
    "manifest": 76,
    "audio": 70,
}
KIND_BASE_DEFAULT = 60

# 广告 / 营销 URL 关键词，命中即重罚（这类 URL 多半是占位或可播但带推广）。
AD_URL_KEYWORDS = ("ad_", "ads/", "marketing")
AD_URL_PENALTY = 80

# 多来源命中同一 URL 的证据加分（pick_best_candidate 内生效）。
EVIDENCE_BONUS = 5

# 签名 URL 标记：不改变分数，只标注（通常表示带时效签名，可播但可能过期）。
_SIGNED_URL_RE = re.compile(r"(?:sign|signature|auth_key|token|expires)=", re.IGNORECASE)

_RESOLUTION_TIERS = ((1080, 12), (720, 8), (480, 4))
_BITRATE_TIERS = ((2_000_000, 6), (1_000_000, 4), (500_000, 2))

_HEIGHT_QUALITY_RE = re.compile(r"(\d{3,4})\s*[pP]")


def kind_for_url(url: str) -> str:
    """按 URL 后缀推断候选 kind：m3u8 → manifest，音频后缀 → audio，其余 video。"""
    lower = normalize_url_text(url)
    if ".m3u8" in lower:
        return "manifest"
    if re.search(r"\.(?:m4a|mp3|aac|flac|wav|ogg)(?:\?|$)", lower):
        return "audio"
    return "video"


def parse_height_from_quality(quality: str) -> int | None:
    """从 you-get/streamlink 的清晰度文案（"1080P" / "720p" / "4K"）解析高度。"""
    text = normalize_url_text(quality)
    if not text:
        return None
    if text == "4k":
        return 2160
    match = _HEIGHT_QUALITY_RE.search(text)
    if not match:
        return None
    return int(match.group(1))


def score_candidate(
    kind: str,
    url: str,
    *,
    height: int | None = None,
    width: int | None = None,
    bitrate: int | None = None,
    url_lower: str = "",
) -> tuple[int, dict[str, Any]]:
    """给单个候选打总分，返回 (score, breakdown)。

    kind 基础分 + 分辨率加分 + 码率加分 - 广告 URL 扣分；签名 URL 只标记不加分。
    """
    base = KIND_BASE_SCORES.get(kind, KIND_BASE_DEFAULT)

    # 分辨率加分：优先 height；缺失时用 width 按 16:9 估算。
    resolution_bonus = 0
    effective_height = height
    if effective_height is None and width:
        effective_height = width * 9 // 16
    for tier_height, bonus in _RESOLUTION_TIERS:
        if effective_height is not None and effective_height >= tier_height:
            resolution_bonus = bonus
            break

    bitrate_bonus = 0
    if bitrate:
        for tier_bitrate, bonus in _BITRATE_TIERS:
            if bitrate >= tier_bitrate:
                bitrate_bonus = bonus
                break

    lower = url_lower if url_lower else normalize_url_text(url)
    ad_penalty = -AD_URL_PENALTY if any(keyword in lower for keyword in AD_URL_KEYWORDS) else 0
    signed = bool(_SIGNED_URL_RE.search(lower))

    total = base + resolution_bonus + bitrate_bonus + ad_penalty
    breakdown = {
        "kind": kind,
        "base": base,
        "resolution": resolution_bonus,
        "bitrate": bitrate_bonus,
        "ad_penalty": ad_penalty,
        "signed": signed,
        "total": total,
    }
    return total, breakdown


def pick_best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """按 score 降序选最优候选（同分保持原顺序），附 score / score_breakdown。

    多来源命中同一 URL（evidence_count >= 2）时加 EVIDENCE_BONUS 分。
    空列表返回 {}。
    """
    best: dict[str, Any] | None = None
    best_score = float("-inf")
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        url = cand.get("url")
        if not isinstance(url, str) or not url:
            continue
        score, breakdown = score_candidate(
            str(cand.get("kind") or ""),
            url,
            height=_as_int(cand.get("height")),
            width=_as_int(cand.get("width")),
            bitrate=_as_int(cand.get("bitrate")),
        )
        evidence_count = _as_int(cand.get("evidence_count")) or 0
        evidence_bonus = EVIDENCE_BONUS if evidence_count >= 2 else 0
        if evidence_bonus:
            score += evidence_bonus
        breakdown["evidence_bonus"] = evidence_bonus
        breakdown["total"] = score
        if score > best_score:
            best_score = score
            best = dict(cand)
            best["score"] = score
            best["score_breakdown"] = dict(breakdown)
    return best or {}


def normalize_url_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
