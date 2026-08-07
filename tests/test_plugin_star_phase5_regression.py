"""Phase 5a 延伸：AstrBot Star 式装饰器回归测试。

锁三件事：
1. @register_command：文本恰好等于命令时调用 handler。
2. @register_regex：文本匹配正则时调用 handler。
3. 未命中走原 handle fallback（dispatch 返回 False）。
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

from core import plugin_registry as pr
from core.plugin_registry import PluginRegistry

_PLUGIN_SRC = '''\
from core.plugin_registry import register_command, register_regex

class Plugin:
    name = "star-test"
    description = "test"
    def handle(self, message, context):
        return "fallback"

@register_command("ping")
def cmd_ping(text, context):
    return "pong"

@register_regex(r"^hello")
def rx_hello(text, context):
    return "hi there"
'''


class StarDecoratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 全局注册表按 fn.__module__ 隔离，测试间清空避免残留。
        pr._COMMAND_HANDLERS.clear()
        pr._REGEX_HANDLERS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        plugins_dir = Path(self._tmp.name) / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "star_test.py").write_text(_PLUGIN_SRC, encoding="utf-8")
        self.registry = PluginRegistry(plugins_dir, logging.getLogger("test"))

    async def test_command_handler_matches_exact_text(self) -> None:
        self.registry.load()
        ok, result = await self.registry.dispatch("star-test", "ping", {})
        self.assertTrue(ok)
        self.assertEqual(result, "pong")

    async def test_regex_handler_matches_pattern(self) -> None:
        self.registry.load()
        ok, result = await self.registry.dispatch("star-test", "hello world", {})
        self.assertTrue(ok)
        self.assertEqual(result, "hi there")

    async def test_unmatched_falls_through(self) -> None:
        self.registry.load()
        ok, result = await self.registry.dispatch("star-test", "nothing matches", {})
        self.assertFalse(ok)
        self.assertIsNone(result)

    async def test_missing_plugin_returns_false(self) -> None:
        self.registry.load()
        ok, _ = await self.registry.dispatch("no-such-plugin", "ping", {})
        self.assertFalse(ok)

    async def test_handle_fallback_still_works_via_call(self) -> None:
        self.registry.load()
        result = await self.registry.call("star-test", "nothing matches", {})
        self.assertEqual(result, "fallback")


if __name__ == "__main__":
    unittest.main()
