"""C1b 工具单轨回归测试 — 继续拆除 agent 工具穿墙调 router 私有 _method_*。

架构收敛 C1b：首批 C1 拆掉 5 个工具后，剩余 agent 工具仍直调 ToolExecutor 的
私有 `_method_*`。本测试锁定这批 call-through 工具改为调用底层能力模块的
模块级公开函数。

被拆的 4 个工具与目标公开接口：
- github_search  → core.tools_github.browser_github_search
- github_readme  → core.tools_github.browser_github_readme
- analyze_image  → core.tools_vision.media_analyze_image
- smart_download → core.tools_video.browser_resolve_video（复用 C1 公开函数）

判据：
1. 源码判据（AST 提取 handler 函数体内的属性调用，不匹配注释/字符串）：
   handler 内不再出现 `_method_*` 属性调用。
2. 功能判据：mock 底层模块后，4 个 handler 均能正常返回 ToolCallResult。
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent_tools_types import ToolCallResult
from core.tools_types import ToolResult

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 4 个被拆工具 → (agent_tools 文件相对路径, handler 函数名)
REFACTORED_TOOLS: dict[str, tuple[str, str]] = {
    "github_search": ("core/agent_tools_web.py", "_handle_github_search"),
    "github_readme": ("core/agent_tools_web.py", "_handle_github_readme"),
    "analyze_image": ("core/agent_tools_media.py", "_handle_analyze_image"),
    "smart_download": ("core/agent_tools_napcat.py", "_handle_smart_download"),
}


def _handler_attribute_calls(file_path: str, handler_name: str) -> set[str]:
    """提取 handler 函数体内出现的所有属性调用名（AST，忽略注释/字符串）。"""
    path = os.path.join(ROOT, file_path)
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == handler_name:
            attrs: set[str] = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    attrs.add(sub.func.attr)
            return attrs
    raise AssertionError(f"handler {handler_name} not found in {file_path}")


# ---------------------------------------------------------------------------
# Mock 依赖
# ---------------------------------------------------------------------------

def _mock_executor() -> MagicMock:
    """模拟 ToolExecutor：私有 _method_* 返回 ToolResult（与真实路径一致）。"""
    te = MagicMock()
    te._method_browser_github_search = AsyncMock(
        return_value=ToolResult(ok=True, tool_name="browser.github_search",
                                payload={"text": "搜索结果：nonebot\nStars: 1000"}, evidence=[])
    )
    te._method_browser_github_readme = AsyncMock(
        return_value=ToolResult(ok=True, tool_name="browser.github_readme",
                                payload={"text": "仓库：owner/repo\nStars: 100\n简介：测试仓库"}, evidence=[])
    )
    te._method_media_analyze_image = AsyncMock(
        return_value=ToolResult(ok=True, tool_name="analyze_image",
                                payload={"text": "这是一只猫", "analysis": "这是一只猫"}, evidence=[])
    )
    te._method_browser_resolve_video = AsyncMock(
        return_value=ToolResult(ok=True, tool_name="smart_download",
                                payload={"video_url": "https://example.com/v.mp4"}, evidence=[])
    )
    return te


def _build_context(**overrides: Any) -> dict[str, Any]:
    base = {
        "tool_executor": _mock_executor(),
        "api_call": lambda **_: {"status": "ok", "data": {"file": "/tmp/nonexistent"}},
        "message_text": "测试消息",
        "conversation_id": "group:999:user:10001",
        "raw_segments": [],
        "reply_media_segments": [],
        "message_id": "1",
        "reply_to_message_id": "",
        "user_id": "10001",
        "group_id": 999,
        "permission_level": "super_admin",
        "config": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 源码判据
# ---------------------------------------------------------------------------

class ToolSingleTrackPhase2SourceTests(unittest.TestCase):
    """断言 4 个被拆工具的 handler 不再穿墙调私有方法。"""

    def test_no_private_method_calls_in_refactored_handlers(self) -> None:
        for tool, (file_path, handler) in REFACTORED_TOOLS.items():
            attrs = _handler_attribute_calls(file_path, handler)
            private_calls = sorted(a for a in attrs if a.startswith("_method_"))
            self.assertEqual(
                private_calls,
                [],
                f"{tool} handler 仍直接调私有 _method_*: {private_calls}",
            )

    def test_public_interface_functions_exist(self) -> None:
        """公开接口函数应存在于目标能力模块。"""
        from core.tools_github import browser_github_readme, browser_github_search
        from core.tools_video import browser_resolve_video
        from core.tools_vision import media_analyze_image

        self.assertTrue(callable(browser_github_search))
        self.assertTrue(callable(browser_github_readme))
        self.assertTrue(callable(media_analyze_image))
        self.assertTrue(callable(browser_resolve_video))


# ---------------------------------------------------------------------------
# 功能判据
# ---------------------------------------------------------------------------

class ToolSingleTrackPhase2FunctionalTests(unittest.TestCase):
    """断言 4 个被拆工具 mock 底层模块后能正常构造/返回。"""

    def _run(self, handler: Any, args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        return asyncio.run(handler(args, context))

    def test_github_search(self) -> None:
        from core.agent_tools_web import _handle_github_search

        result = self._run(
            _handle_github_search,
            {"query": "nonebot", "language": "python"},
            _build_context(),
        )
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"github_search should be ok, got error={result.error}")

    def test_github_search_without_language(self) -> None:
        from core.agent_tools_web import _handle_github_search

        result = self._run(
            _handle_github_search, {"query": "nonebot"}, _build_context()
        )
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"github_search should be ok, got error={result.error}")

    def test_github_readme_repo_form(self) -> None:
        from core.agent_tools_web import _handle_github_readme

        result = self._run(
            _handle_github_readme, {"repo": "owner/repo"}, _build_context()
        )
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"github_readme should be ok, got error={result.error}")

    def test_github_readme_url_form(self) -> None:
        from core.agent_tools_web import _handle_github_readme

        result = self._run(
            _handle_github_readme,
            {"repo": "https://github.com/owner/repo"},
            _build_context(),
        )
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"github_readme should be ok, got error={result.error}")

    def test_analyze_image_explicit_url(self) -> None:
        from core.agent_tools_media import _handle_analyze_image

        result = self._run(
            _handle_analyze_image,
            {"url": "https://example.com/cat.png", "question": "这是什么？"},
            _build_context(),
        )
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"analyze_image should be ok, got error={result.error}")
        self.assertEqual(result.data.get("analysis"), "这是一只猫")

    def test_analyze_image_from_segments(self) -> None:
        """当前消息带图片段（无显式 url）时应走图片段目标选择路径。"""
        from core.agent_tools_media import _handle_analyze_image

        context = _build_context(raw_segments=[{"type": "image", "data": {"file": "abc.png"}}])
        result = self._run(_handle_analyze_image, {}, context)
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"analyze_image should be ok, got error={result.error}")

    def test_smart_download_uses_public_resolver(self) -> None:
        """smart_download 的媒体解析链应走公开 browser_resolve_video，不再穿墙。"""
        from core.agent_tools_napcat import _handle_smart_download

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2")
            tmp_path = fh.name
        self.addCleanup(lambda: os.path.exists(tmp_path) and os.unlink(tmp_path))

        executor = _mock_executor()
        context = _build_context(tool_executor=executor)

        resolver = AsyncMock(return_value=ToolResult(
            ok=True, tool_name="smart_download",
            payload={"video_url": "https://example.com/v.mp4", "text": "解析成功"},
            evidence=[],
        ))
        napcat_api = AsyncMock(
            return_value=ToolCallResult(ok=True, data={"file": tmp_path}, display="ok")
        )
        staged = MagicMock(return_value="/tmp/staged/v.mp4")

        with patch("core.tools_video.browser_resolve_video", resolver), \
             patch("core.agent_tools_napcat._napcat_api_call", napcat_api), \
             patch("core.agent_tools_napcat._stage_download_file", staged):
            result = asyncio.run(_handle_smart_download(
                {"url": "https://example.com/landing", "kind": "video"}, context,
            ))

        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"smart_download should be ok, got error={result.error}")
        resolver.assert_awaited_once_with(
            executor,
            url="https://example.com/landing",
            query="https://example.com/landing",
            method_name="smart_download",
        )
        self.assertFalse(
            executor._method_browser_resolve_video.called,
            "smart_download 仍穿墙调私有 _method_browser_resolve_video",
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    unittest.main(verbosity=2)
