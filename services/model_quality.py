from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

# 每模型保留的最近调用次数（环形缓冲上限）
_MAX_RECORDS_PER_MODEL = 100
# 延迟评分的基准时长：平均延迟达到该值（毫秒）时延迟分衰减到 0
_LATENCY_REFERENCE_MS = 20_000.0
# 错误类型权重：成功 0，超时/网络类最轻，拒绝类（401/403/配额/限流）最重
_ERROR_WEIGHTS = {
    "success": 0.0,
    "timeout": 0.5,
    "error": 0.8,
    "reject": 1.0,
}
# 加权打分系数：成功率 / 平均延迟 / 错误类型
_WEIGHT_SUCCESS_RATE = 0.6
_WEIGHT_LATENCY = 0.3
_WEIGHT_ERROR = 0.1


class ModelQualityTracker:
    """模型调用质量评测：按 (provider, model) 记录每次调用的成败与延迟，加权打分排序。

    仅供 failover 链排序使用，不改变任何调用行为。内存环形缓冲，每个模型最多保留
    ``_MAX_RECORDS_PER_MODEL`` 条记录，超出后丢弃最旧记录。
    """

    def __init__(self, max_records_per_model: int = _MAX_RECORDS_PER_MODEL) -> None:
        self._max_records = max_records_per_model
        self._records: dict[tuple[str, str], deque[Any]] = defaultdict(lambda: deque(maxlen=max_records_per_model))

    def record_outcome(
        self,
        provider: str,
        model: str,
        ok: bool,
        latency_ms: float,
        error_type: str = "success",
    ) -> None:
        """记录一次模型调用的结果。

        ``ok=True`` 时 ``error_type`` 应为 ``"success"``；失败时按错误类型给
        ``"timeout"`` / ``"error"`` / ``"reject"`` 之一（拒绝最重：认证/配额/限流）。
        """
        key = (str(provider or "").strip(), str(model or "").strip())
        if not key[0] or not key[1]:
            return
        self._records[key].append((bool(ok), max(0.0, float(latency_ms)), str(error_type or "success")))

    def score_for(self, provider: str, model: str, min_samples: int = 3) -> float | None:
        """返回 (provider, model) 的当前质量分；样本不足 ``min_samples`` 时返回 None。"""
        records = self._records.get((str(provider or "").strip(), str(model or "").strip()))
        if not records or len(records) < min_samples:
            return None
        return self._score(records)

    def get_rankings(self, min_samples: int = 3) -> list[tuple[str, float]]:
        """按质量分降序返回 [(model, score)]，样本不足的模型不参与排序。"""
        ranked: list[tuple[str, float]] = []
        for (provider, model), records in self._records.items():
            if len(records) < min_samples:
                continue
            ranked.append((model, self._score(records)))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked

    def stats(self, provider: str, model: str) -> dict[str, Any] | None:
        """可观测性快照：样本数 / 成功率 / 成功平均延迟 / 当前分。无样本返回 None。"""
        records = self._records.get((str(provider or "").strip(), str(model or "").strip()))
        if not records:
            return None
        n = len(records)
        ok_count = sum(1 for rec in records if rec[0])
        latencies = [rec[1] for rec in records if rec[0]]
        return {
            "samples": n,
            "success_rate": ok_count / n,
            "avg_success_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
            "score": self._score(records),
        }

    def reset(self) -> None:
        self._records.clear()

    @staticmethod
    def _score(records: deque[Any]) -> float:
        n = len(records)
        ok_count = sum(1 for rec in records if rec[0])
        success_rate = ok_count / n
        latencies = [rec[1] for rec in records if rec[0]]
        if latencies:
            latency_score = max(0.0, 1.0 - (sum(latencies) / len(latencies)) / _LATENCY_REFERENCE_MS)
        else:
            latency_score = 0.0
        error_penalty = sum(_ERROR_WEIGHTS.get(rec[2], 1.0) for rec in records) / n
        return (
            _WEIGHT_SUCCESS_RATE * success_rate
            + _WEIGHT_LATENCY * latency_score
            + _WEIGHT_ERROR * (1.0 - error_penalty)
        )


# 模块级默认实例，供不持有 tracker 的调用方使用
_default_tracker = ModelQualityTracker()


def record_outcome(
    provider: str,
    model: str,
    ok: bool,
    latency_ms: float,
    error_type: str = "success",
) -> None:
    _default_tracker.record_outcome(provider, model, ok, latency_ms, error_type)


def get_model_rankings(min_samples: int = 3) -> list[tuple[str, float]]:
    return _default_tracker.get_rankings(min_samples)
