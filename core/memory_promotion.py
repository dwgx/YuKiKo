"""Phase 0.5a：记忆晋升门（OpenClaw 双层晋升的确定性 + 模型回合）。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.1（2）。

**第一层（确定性，代码）** `rank_promotion_candidates()`：
  - 从候选记忆里**结构性排除** untrusted / system 来源与非 interactive 会话
    （排除不是降权：这些来源无论被召回多少次都不晋升）。
  - 按 OpenClaw `DEFAULT_PROMOTION_WEIGHTS` 权重评分排序，阈值 0.75。

**第二层（模型回合）** `consolidate_memory()`：
  - 调模型把候选合并进既有 Curated（explicit_facts / KnowledgeBase）。
  - 输出过 `validate_consolidated_memory()` 校验（operation 数匹配 / action 合法 /
    prior 引用存在 / 有界丢失 / 字符预算）。
  - **失败回退 append-only，永不丢记忆**（OpenClaw 的 bounded loss 原则）。

`consolidate_memory` 的模型客户端可注入（测试用假客户端），不强制依赖真实 provider。
本模块零第三方依赖，只 import 标准库。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from typing import Any

# ── 常量（与 OpenClaw 一手源码对齐，勿改值，除非有实测证据）──
_PROMOTION_WEIGHTS: dict[str, float] = {
    "freq": 0.24,
    "avg_score": 0.30,
    "diversity": 0.15,
    "recency": 0.15,
    "consolidation": 0.10,
    "conceptual": 0.06,
}
PROMOTION_MIN_SCORE = 0.75
PROMOTION_MIN_SIGNAL = 3
PROMOTION_MAX_AGE_DAYS = 30
PROMOTION_HALF_LIFE_DAYS = 14
CURATED_MAX_CHARS = 10_000
MAX_PRIOR_ENTRY_LOSS_FRACTION = 0.25

# 结构性排除的来源：untrusted（群聊未指向）/ system（系统生成）永不可晋升。
_BLOCKED_ORIGINS = frozenset({"untrusted", "system"})
# 非 interactive 会话的产物不产生晋升候选。
_NON_INTERACTIVE_SESSION_KINDS = frozenset({"cron", "heartbeat", "subagent"})

_VALID_ACTIONS = frozenset({"added", "merged", "superseded"})

# 事实动词的弱信号（仅影响 0.06 权重的 conceptual 分量，不是语义门控）。
_FACT_VERB_HINTS = ("是", "住", "喜欢", "工作", "在", "有", "爱", "毕业于", "养")

_CONSOLIDATION_SYSTEM_PROMPT = (
    "你是记忆整理引擎。把候选记忆合并进已有的长期记忆（Curated）。\n"
    "逐条候选输出一个 operation，action ∈ {added, merged, superseded}：\n"
    "  - added：新事实，直接加入\n"
    "  - merged：与现有条目合并（priorEntries 列出被合并的现有条目）\n"
    "  - superseded：取代一条现有条目（priorEntries 列出被取代的条目，resultEntry 是新表述）\n"
    "规则：禁止改写与候选无关的现有条目；priorEntries 必须引用 existing 里的原样文本；\n"
    "被取代的条目总数不得超过现有条目数的 25%。只输出 JSON："
    '{"operations": [{"candidateKey": "<候选原文>", "action": "added|merged|superseded", '
    '"resultEntry": "<最终条目文本>", "priorEntries": ["<被合并/取代的现有条目>"]}]}'
)


@dataclass(frozen=True)
class PromotionCandidate:
    """一条待晋升的记忆候选。score 由 rank_promotion_candidates 计算。"""

    content: str
    origin_class: str = "untrusted"
    session_kind: str = "interactive"
    conversation_id: str = ""
    user_id: str = ""
    created_at: str = ""
    signal_count: int = 1
    recall_days: int = 1
    context_diversity: int = 1
    importance: float = 5.0
    score: float = 0.0
    is_blocked: bool = False
    block_reason: str = ""


# ── 评分公式（OpenClaw DEFAULT_PROMOTION_WEIGHTS）──

def _parse_iso_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        # naive 时间戳按 UTC 补齐，避免与 aware `now` 相减抛 TypeError。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_days(created_at: str, now: datetime) -> float:
    parsed = _parse_iso_ts(created_at)
    if parsed is None:
        return 0.0
    delta = now - parsed
    if delta.total_seconds() < 0:
        return 0.0
    return delta.total_seconds() / 86400.0


def _freq_score(signal_count: int) -> float:
    return math.log1p(max(0, signal_count)) / math.log1p(10.0)


def _recency_score(age_days: float) -> float:
    # exp(-ln2 * age / half_life)，14 天半衰。
    return math.exp(-math.log(2.0) * age_days / PROMOTION_HALF_LIFE_DAYS)


def _consolidation_score(recall_days: int, grounded_count: int = 0) -> float:
    # 跨天/被锚定次数越多，越像稳定事实。
    return max(min(max(0, recall_days), 5) / 5.0, min(max(0, grounded_count), 3) / 3.0)


def _diversity_score(context_diversity: int) -> float:
    return min(max(0, context_diversity), 5) / 5.0


def _conceptual_score(content: str) -> float:
    """陈述句倾向。弱信号（权重 0.06），不是语义门控。"""
    text = (content or "").strip()
    if not text:
        return 0.0
    score = 0.5
    if text.endswith(("。", ".", "！")):
        score += 0.2
    if any(k in text for k in _FACT_VERB_HINTS):
        score += 0.3
    return min(score, 1.0)


def _compute_score(candidate: PromotionCandidate, now: datetime) -> float:
    age_days = _age_days(candidate.created_at, now)
    avg_score = min(max(candidate.importance, 1.0), 10.0) / 10.0
    return (
        _PROMOTION_WEIGHTS["freq"] * _freq_score(candidate.signal_count)
        + _PROMOTION_WEIGHTS["avg_score"] * avg_score
        + _PROMOTION_WEIGHTS["diversity"] * _diversity_score(candidate.context_diversity)
        + _PROMOTION_WEIGHTS["recency"] * _recency_score(age_days)
        + _PROMOTION_WEIGHTS["consolidation"] * _consolidation_score(candidate.recall_days)
        + _PROMOTION_WEIGHTS["conceptual"] * _conceptual_score(candidate.content)
    )


# ── 第一层：确定性排名 ──

def rank_promotion_candidates(
    candidates: Iterable[Mapping[str, Any]],
    now: datetime | None = None,
) -> list[PromotionCandidate]:
    """确定性晋升门：结构性排除 + 权重评分排序。

    入参每项 dict 可含：content / origin_class / session_kind / conversation_id /
    user_id / created_at / signal_count / recall_days / context_diversity / importance。

    `is_blocked=True` 的候选（untrusted/system/非 interactive）会保留在返回列表里
    （带 block_reason），但调用方**必须**丢弃它们；返回它们是为了可观测（审计能看到
    为什么没晋升）。
    """
    now = now or datetime.now(UTC)
    ranked: list[PromotionCandidate] = []
    for raw in candidates:
        content = str(raw.get("content", "")).strip()
        if not content:
            continue
        origin_class = str(raw.get("origin_class", "untrusted")).lower()
        session_kind = str(raw.get("session_kind", "interactive")).lower()
        blocked, block_reason = False, ""
        if origin_class in _BLOCKED_ORIGINS:
            blocked, block_reason = True, f"origin_{origin_class}"
        elif session_kind in _NON_INTERACTIVE_SESSION_KINDS:
            blocked, block_reason = True, f"session_{session_kind}"
        base = PromotionCandidate(
            content=content,
            origin_class=origin_class,
            session_kind=session_kind,
            conversation_id=str(raw.get("conversation_id", "")),
            user_id=str(raw.get("user_id", "")),
            created_at=str(raw.get("created_at", "")),
            signal_count=int(raw.get("signal_count", 1) or 1),
            recall_days=int(raw.get("recall_days", 1) or 1),
            context_diversity=int(raw.get("context_diversity", 1) or 1),
            importance=float(raw.get("importance", 5.0) or 5.0),
            is_blocked=blocked,
            block_reason=block_reason,
        )
        score = _compute_score(base, now)
        ranked.append(PromotionCandidate(**{**base.__dict__, "score": score}))
    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked


def promotionable_candidates(
    candidates: Iterable[Mapping[str, Any]],
    now: datetime | None = None,
) -> list[PromotionCandidate]:
    """rank 之后过滤：丢弃 blocked，且满足 min_signal / max_age / min_score。"""
    now = now or datetime.now(UTC)
    out: list[PromotionCandidate] = []
    for candidate in rank_promotion_candidates(candidates, now=now):
        if candidate.is_blocked:
            continue
        if candidate.signal_count < PROMOTION_MIN_SIGNAL:
            continue
        age_days = _age_days(candidate.created_at, now)
        if age_days > PROMOTION_MAX_AGE_DAYS:
            continue
        if candidate.score < PROMOTION_MIN_SCORE:
            continue
        out.append(candidate)
    return out


# ── 第二层：模型 consolidation 回合 ──

def build_consolidation_prompt(
    candidates: list[PromotionCandidate],
    existing_curated: list[str],
) -> str:
    """构造 consolidation prompt（把候选 + 现有 Curated 序列化给模型）。"""
    candidate_lines = "\n".join(
        f"- [{i}] ({c.origin_class}) {c.content}" for i, c in enumerate(candidates)
    )
    existing_lines = "\n".join(f"- {e}" for e in existing_curated) or "- （无）"
    return (
        "候选记忆（待决定是否晋升）：\n"
        f"{candidate_lines}\n\n"
        "现有长期记忆（existing，只能被 merged/superseded 引用，禁止改写）：\n"
        f"{existing_lines}\n\n"
        "请按系统提示输出 operations JSON。"
    )


def validate_consolidated_memory(
    operations: list[Mapping[str, Any]],
    expected_count: int,
    existing_curated: list[str],
    *,
    max_chars: int = CURATED_MAX_CHARS,
    max_prior_loss_fraction: float = MAX_PRIOR_ENTRY_LOSS_FRACTION,
) -> tuple[bool, str]:
    """校验模型 consolidation 输出（OpenClaw validateConsolidatedMemory 的精简版）。

    规则：
    - operations 数 == 候选数
    - action ∈ {added, merged, superseded}
    - 每条 op 的 priorEntries 必须引用 existing_curated 里的原样文本
    - 被 superseded 的 prior 数 / len(existing) <= max_prior_loss_fraction（有界丢失）
    - 应用后的 curated 总字符 <= max_chars（字符预算）
    """
    if not isinstance(operations, list):
        return False, "operations_not_list"
    if len(operations) != expected_count:
        return False, f"operation_count_mismatch:{len(operations)}!={expected_count}"
    existing_set = {str(e) for e in existing_curated}
    superseded_prior: set[str] = set()
    applied: list[str] = list(existing_curated)
    for op in operations:
        if not isinstance(op, dict):
            return False, "operation_not_object"
        action = str(op.get("action", "")).lower()
        if action not in _VALID_ACTIONS:
            return False, f"invalid_action:{action}"
        priors = op.get("priorEntries") or []
        if not isinstance(priors, list):
            return False, "priorEntries_not_list"
        for prior in priors:
            prior_text = str(prior)
            if prior_text not in existing_set:
                return False, f"prior_not_in_existing:{prior_text}"
            # 只有 superseded（取代）算"丢失"；merged 是增补，prior 信息保留在新条目里。
            if action == "superseded":
                superseded_prior.add(prior_text)
        result_entry = str(op.get("resultEntry", "")).strip()
        if not result_entry:
            return False, "missing_resultEntry"
        if action == "added":
            applied.append(result_entry)
        elif action == "superseded":
            applied = [e for e in applied if e not in set(priors)]
            applied.append(result_entry)
        # merged：保留 prior，resultEntry 作为新表述（不删旧，保守）
        elif action == "merged" and result_entry not in applied:
            applied.append(result_entry)
    # 有界丢失：被取代的条目不能超过现有条目的一定比例。
    if existing_curated:
        loss_fraction = len(superseded_prior) / len(existing_curated)
        if loss_fraction > max_prior_loss_fraction:
            return False, f"prior_loss_exceeded:{loss_fraction:.2f}>{max_prior_loss_fraction}"
    if sum(len(e) for e in applied) > max_chars:
        return False, "curated_budget_exceeded"
    return True, "ok"


async def consolidate_memory(
    candidates: list[PromotionCandidate],
    existing_curated: list[str],
    *,
    model_client: Any,
    system_prompt: str = _CONSOLIDATION_SYSTEM_PROMPT,
) -> dict[str, Any]:
    """模型回合：把候选合并进既有 Curated。

    `model_client` 需提供 `async chat_json(messages: list[dict]) -> dict`（与项目
    测试里的假客户端同接口）。任何失败都返回 `{"ok": False, "append_only": True}`，
    调用方应把候选**原样追加**而不是丢弃（bounded loss：永不丢记忆）。

    返回：`{"ok": bool, "operations": list, "error": str, "append_only": bool}`。
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": build_consolidation_prompt(candidates, existing_curated)},
    ]
    try:
        payload = await model_client.chat_json(messages)
    except Exception as exc:  # noqa: BLE001 - 模型调用失败按 OpenClaw 回退 append-only
        return {"ok": False, "error": f"consolidation_call_failed:{exc}", "operations": [], "append_only": True}
    operations = payload.get("operations") if isinstance(payload, dict) else None
    if not isinstance(operations, list):
        return {
            "ok": False,
            "error": "consolidation_missing_operations",
            "operations": [],
            "append_only": True,
        }
    ok, reason = validate_consolidated_memory(
        operations, len(candidates), existing_curated
    )
    if not ok:
        return {
            "ok": False,
            "error": f"consolidation_invalid:{reason}",
            "operations": operations,
            "append_only": True,
        }
    return {"ok": True, "operations": operations, "append_only": False}


def apply_operations(
    operations: list[Mapping[str, Any]],
    existing_curated: list[str],
) -> list[str]:
    """把校验通过的 operations 应用到 Curated，返回新的 curated 列表。

    与 validate_consolidated_memory 里的模拟应用保持一致。
    """
    applied: list[str] = list(existing_curated)
    for op in operations:
        action = str(op.get("action", "")).lower()
        priors = {str(p) for p in (op.get("priorEntries") or [])}
        result_entry = str(op.get("resultEntry", "")).strip()
        if action == "added":
            if result_entry and result_entry not in applied:
                applied.append(result_entry)
        elif action == "superseded":
            applied = [e for e in applied if e not in priors]
            if result_entry and result_entry not in applied:
                applied.append(result_entry)
        elif action == "merged":
            if result_entry and result_entry not in applied:
                applied.append(result_entry)
    return applied
