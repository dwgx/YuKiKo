"""分离式审计流 — append-only JSONL。

为什么不复用 services/logger.py：那是单个 2MB×4 的滚动文本日志，所有事件混写，
群操作痕迹会被正常聊天挤掉，而且是自由文本、无法按字段查询。

本模块给每类事件一条独立的持久流，各自独立滚动，互不挤占：

    storage/audit/tool_calls/2026-08-04.jsonl    工具调用
    storage/audit/memory_writes/2026-08-04.jsonl  记忆写入与变更
    storage/audit/group_ops/2026-08-04.jsonl      QQ 群操作
    storage/audit/prompt_edits/2026-08-04.jsonl   自改 prompt
    storage/audit/knowledge/2026-08-04.jsonl      知识库写入与净化

按天分文件：天然可按日期查询，配合每日日记，且不需要滚动就能长期保留。
每行一个 JSON 对象，字段固定，便于 WebUI 与后续分析直接读。

设计约束：
- 写失败绝不影响主流程（审计是旁路），但失败必须在文本日志里可见，不静默。
- 不做异步队列：单行 append 在本地磁盘是微秒级，引入队列反而增加丢失窗口。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger("yukiko.audit")

# 审计流名称常量：调用方一律用这些，避免拼写漂移导致事件写进错误的流。
STREAM_TOOL_CALLS = "tool_calls"
STREAM_MEMORY_WRITES = "memory_writes"
STREAM_GROUP_OPS = "group_ops"
STREAM_PROMPT_EDITS = "prompt_edits"
STREAM_KNOWLEDGE = "knowledge"

_KNOWN_STREAMS = frozenset(
    {
        STREAM_TOOL_CALLS,
        STREAM_MEMORY_WRITES,
        STREAM_GROUP_OPS,
        STREAM_PROMPT_EDITS,
        STREAM_KNOWLEDGE,
    }
)

# 单条记录里任意字符串字段的上限。审计要能查，不需要存全文；
# 真正的大字段（图片 base64、网页正文）截断后留长度即可。
_MAX_FIELD_CHARS = 2000


class AuditTrail:
    """按流分文件、按天分片的 append-only 审计写入器。"""

    def __init__(self, base_dir: Path, enable: bool = True) -> None:
        self.base_dir = Path(base_dir)
        self.enable = bool(enable)
        self._locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()
        self._warned_streams: set[str] = set()

    def _lock_for(self, stream: str) -> threading.Lock:
        with self._lock_guard:
            if stream not in self._locks:
                self._locks[stream] = threading.Lock()
            return self._locks[stream]

    @staticmethod
    def _clip(value: Any) -> Any:
        """截断超长字符串，保留原长度以便判断是否被截。"""
        if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
            return {
                "_truncated": True,
                "_original_chars": len(value),
                "text": value[:_MAX_FIELD_CHARS],
            }
        if isinstance(value, dict):
            return {k: AuditTrail._clip(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [AuditTrail._clip(v) for v in value]
        return value

    def path_for(self, stream: str, day: str | None = None) -> Path:
        day_key = day or datetime.now(UTC).strftime("%Y-%m-%d")
        return self.base_dir / stream / f"{day_key}.jsonl"

    def write(self, stream: str, event: str, **fields: Any) -> bool:
        """写一条审计记录。返回是否成功；失败不抛异常。"""
        if not self.enable:
            return False
        if stream not in _KNOWN_STREAMS:
            # 未知流名几乎总是拼写错误，会让事件悄悄进错文件。
            if stream not in self._warned_streams:
                self._warned_streams.add(stream)
                _log.warning("audit_unknown_stream | stream=%s | event=%s", stream, event)
            return False

        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **{k: self._clip(v) for k, v in fields.items()},
        }
        target = self.path_for(stream)
        try:
            with self._lock_for(stream):
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except Exception:
            # 审计是旁路，不能影响主流程；但失败必须可见。
            _log.warning(
                "audit_write_failed | stream=%s | event=%s", stream, event, exc_info=True
            )
            return False

    def read(
        self, stream: str, day: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """读回最近 limit 条（供 WebUI 查询）。坏行跳过而不是整体失败。"""
        if stream not in _KNOWN_STREAMS:
            return []
        target = self.path_for(stream, day)
        if not target.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with target.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            _log.warning("audit_read_failed | stream=%s", stream, exc_info=True)
            return []
        return rows[-max(1, int(limit)):]

    def available_days(self, stream: str) -> list[str]:
        """该流有记录的日期，升序。"""
        if stream not in _KNOWN_STREAMS:
            return []
        d = self.base_dir / stream
        if not d.is_dir():
            return []
        try:
            return sorted(p.stem for p in d.glob("*.jsonl") if p.is_file())
        except Exception:
            return []

    def streams(self) -> list[str]:
        return sorted(_KNOWN_STREAMS)
