"""Phase 2：Platform 抽象（AstrBot 风格最小集）。

契约只有 run/meta 抽象方法，其余默认实现。事件经 commit_event 投进共享事件队列，
发送经 send_by_session。对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4（1）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PlatformStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass(frozen=True)
class PlatformMetadata:
    name: str
    support_streaming_message: bool = False
    support_proactive_message: bool = False


class Platform(ABC):
    """IM 平台适配器基类。子类实现 run/meta，其余为默认实现。"""

    def __init__(self, config: dict[str, Any] | None = None, event_queue: Any = None) -> None:
        self.config = config or {}
        self.event_queue = event_queue
        self.status = PlatformStatus.PENDING
        self._started_at: float | None = None

    @abstractmethod
    async def run(self) -> None:
        """启动平台监听（阻塞直至 terminate）。"""

    @abstractmethod
    def meta(self) -> PlatformMetadata:
        """平台元数据。"""

    async def terminate(self) -> None:
        """停止平台。子类应覆盖并清理资源。"""
        self.status = PlatformStatus.STOPPED

    async def send_by_session(self, session_id: str, chain: Any) -> bool:
        """按会话发送（默认只记指标，子类实现真实发送）。"""
        return True

    def commit_event(self, event: Any) -> None:
        """把入站事件投进共享事件队列。"""
        if self.event_queue is not None:
            self.event_queue.put_nowait(event)

    def get_client(self) -> Any:
        return None
