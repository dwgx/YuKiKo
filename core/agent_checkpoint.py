"""Phase 5b：AgentLoop 轻量 checkpoint（每步工具调用日志）。

对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.5 第 3 条与 Phase 5b：
每步工具调用落一行结构化记录（JSONL），供诊断与超时重试回溯。

`AgentStepJournal` 是内存 + 可选 JSONL 落盘的轻量实现；不阻塞主流程，
落盘失败只记 warning。完整的状态恢复（恢复 messages/steps 继续循环）不在此
模块 —— 那需要与 queue 的超时重试联动，作为后续增量。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class AgentTurnCheckpoint:
    """单回合 agent 状态检查点（step_idx/messages/steps），超时重试可恢复。

    按 trace_id 存成独立 JSON 文件（天然隔离），带 TTL 过期清理。save 失败不抛
    （IO 异常记日志级降级），load 时 TTL 过期当作不存在。
    """

    def __init__(self, directory: Path | str | None = None, *, ttl_seconds: int = 3600) -> None:
        self.directory = Path(directory) if directory else None
        self.ttl_seconds = ttl_seconds

    def _path(self, trace_id: str) -> Path | None:
        if not self.directory:
            return None
        return self.directory / f"{trace_id}.json"

    def save(
        self,
        *,
        trace_id: str,
        step_idx: int,
        messages: list[dict[str, Any]],
        steps: list[dict[str, Any]],
    ) -> bool:
        path = self._path(trace_id)
        if not path:
            return False
        payload = {
            "step_idx": int(step_idx),
            "messages": messages,
            "steps": steps,
            "saved_at": time.time(),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
            return True
        except OSError:
            return False

    def load(self, trace_id: str) -> dict[str, Any] | None:
        path = self._path(trace_id)
        if not path or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            saved_at = float(payload.get("saved_at", 0))
        except (TypeError, ValueError):
            saved_at = 0.0
        if time.time() - saved_at > self.ttl_seconds:
            self.clear(trace_id)
            return None
        return payload

    def clear(self, trace_id: str) -> None:
        path = self._path(trace_id)
        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def cleanup_expired(self) -> int:
        """清理过期 checkpoint，返回删除数。"""
        if not self.directory or not self.directory.exists():
            return 0
        removed = 0
        now = time.time()
        for file in self.directory.glob("*.json"):
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
                if now - float(payload.get("saved_at", 0)) > self.ttl_seconds:
                    file.unlink()
                    removed += 1
            except (OSError, json.JSONDecodeError):
                continue
        return removed


class AgentStepJournal:
    """每步工具调用的结构化日志（JSONL）。"""

    def __init__(self, path: Path | str | None = None, *, max_memory_lines: int = 200) -> None:
        self.path = Path(path) if path else None
        self._lines: list[dict[str, Any]] = []
        self._max_memory_lines = max_memory_lines

    def record(
        self,
        *,
        trace_id: str,
        step: int,
        tool: str,
        ok: bool,
        error: str = "",
        elapsed_ms: float = 0.0,
    ) -> None:
        """记录一次工具调用。内存保留最近 max_memory_lines 行，落盘追加。"""
        row = {
            "trace_id": str(trace_id),
            "step": int(step),
            "tool": str(tool),
            "ok": bool(ok),
            "error": str(error),
            "elapsed_ms": round(float(elapsed_ms), 1),
        }
        self._lines.append(row)
        if len(self._lines) > self._max_memory_lines:
            self._lines = self._lines[-self._max_memory_lines:]
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def snapshot(self) -> list[dict[str, Any]]:
        """返回当前内存快照（用于超时恢复/诊断）。"""
        return list(self._lines)

    def load(self) -> list[dict[str, Any]]:
        """从 JSONL 读取全部记录（若落盘）。内存优先。"""
        if self.path and self.path.exists():
            rows: list[dict[str, Any]] = []
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            except (OSError, json.JSONDecodeError):
                return self.snapshot()
            return rows
        return self.snapshot()
