"""Phase 2：PlatformManager —— 平台生命周期管理。

注册 → start（create_task 跑每个 run() + wrapper 捕获异常进 record_error）→
stop（terminate + cancel 任务）。对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.4（1）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.platform.base import Platform, PlatformStatus

_log = logging.getLogger("yukiko.platform")


class PlatformManager:
    """管理多个平台适配器的生命周期（当前只保 QQ，单实例即可）。"""

    def __init__(self) -> None:
        self._platforms: dict[str, Platform] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def register(self, name: str, platform: Platform) -> None:
        self._platforms[name] = platform

    def get(self, name: str) -> Platform | None:
        return self._platforms.get(name)

    def all(self) -> list[Platform]:
        return list(self._platforms.values())

    async def start(self) -> None:
        """为每个平台 create_task(run())，wrapper 捕获异常并记录状态。"""
        for name, platform in self._platforms.items():
            if platform.status not in (PlatformStatus.PENDING, PlatformStatus.STOPPED):
                continue

            async def _wrapper(platform: Platform = platform, name: str = name) -> None:
                platform.status = PlatformStatus.RUNNING
                try:
                    await platform.run()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    platform.status = PlatformStatus.ERROR
                    _log.warning("platform_run_error | name=%s", name, exc_info=True)

            self._tasks[name] = asyncio.create_task(_wrapper())

    async def stop(self) -> None:
        """先 terminate 每个平台，再 cancel 任务。"""
        for name, platform in self._platforms.items():
            try:
                await platform.terminate()
            except Exception:
                _log.warning("platform_terminate_error | name=%s", name, exc_info=True)
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
        for name, task in self._tasks.items():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
