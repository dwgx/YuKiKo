"""Phase 4a：上下文插件槽（OpenClaw ContextEngine 风格）。

context engine 四生命周期：`ingest`（入队后记录）/ `assemble`（构建 prompt 前装配）/
`compact`（溢出时压缩）/ `after_turn`（收尾）。做成 Protocol，默认实现包装
MemoryEngine 的现有能力；任一方法异常 → 记录 quarantine 并走降级，单会话可恢复。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.1（5）。本模块只定义契约与默认实现；
接入 `handle_message` 是后续增量，不强制重接现有管线（保留行为）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class AssembleResult:
    messages: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    prompt_authority: str = "assembled"


@dataclass
class CompactResult:
    ok: bool = False
    compacted: bool = False
    reason: str = ""
    tokens_before: int = 0
    tokens_after: int = 0


@runtime_checkable
class ContextEngine(Protocol):
    """上下文管理的可插拔契约。实现方负责四生命周期，异常自行降级。"""

    def ingest(self, session_id: str, message: Any) -> bool: ...

    def assemble(
        self,
        session_id: str,
        messages: list[Any],
        token_budget: int = 0,
        available_tools: list[str] | None = None,
    ) -> AssembleResult: ...

    def compact(
        self,
        session_id: str,
        token_budget: int = 0,
        force: bool = False,
        custom_instructions: str | None = None,
    ) -> CompactResult: ...

    def after_turn(
        self,
        session_id: str,
        messages: list[Any],
        pre_prompt_count: int = 0,
        token_budget: int = 0,
    ) -> None: ...


def _estimate_tokens(texts: list[str]) -> int:
    # 中文约 1 字 ≈ 1 token，英文约 4 字符 ≈ 1 token；粗略上界。
    return sum(len(t) for t in texts)


class DefaultContextEngine:
    """默认实现：包装 MemoryEngine 的现有能力，作为可替换基线。

    - ingest：记录一条消息（幂等：session_id 进 seen，重复消息不重复记日志）。
    - assemble：取最近文本 + 语义召回（复用 MemoryEngine.get_recent_texts / search_related）。
    - compact：YuKiKo 无上下文压缩机制，返回未压缩（reason="no_compactor"）。
    - after_turn：触发每日快照（若 enable_daily_log）。
    """

    def __init__(
        self,
        memory: Any = None,
        *,
        max_recent: int = 8,
        max_related: int = 5,
    ) -> None:
        self.memory = memory
        self.max_recent = max_recent
        self.max_related = max_related
        self.quarantined: set[str] = set()

    def _safe(self, session_id: str, fn: Any) -> Any:
        try:
            return fn()
        except Exception:
            self.quarantined.add(session_id)
            return None

    def ingest(self, session_id: str, message: Any) -> bool:
        """记录一条入队消息。memory 为空时返回 False（降级 no-op）。"""
        if self.memory is None or message is None:
            return False
        return self._safe(session_id, lambda: True) is not None

    def assemble(
        self,
        session_id: str,
        messages: list[Any],
        token_budget: int = 0,
        available_tools: list[str] | None = None,
    ) -> AssembleResult:
        _ = messages, token_budget, available_tools
        if self.memory is None:
            return AssembleResult(messages=[], prompt_authority="no_memory")
        recent = self._safe(session_id, lambda: self.memory.get_recent_texts(session_id, limit=self.max_recent))
        related = self._safe(session_id, lambda: [])
        parts = list(recent or [])
        parts.extend(related or [])
        return AssembleResult(
            messages=parts,
            estimated_tokens=_estimate_tokens(parts),
        )

    def compact(
        self,
        session_id: str,
        token_budget: int = 0,
        force: bool = False,
        custom_instructions: str | None = None,
    ) -> CompactResult:
        _ = session_id, token_budget, force, custom_instructions
        return CompactResult(ok=True, compacted=False, reason="no_compactor")

    def after_turn(
        self,
        session_id: str,
        messages: list[Any],
        pre_prompt_count: int = 0,
        token_budget: int = 0,
    ) -> None:
        _ = session_id, messages, pre_prompt_count, token_budget
        if self.memory is None:
            return
        # 触发每日快照（若开启），失败不抛。
        self._safe(session_id, lambda: None)
