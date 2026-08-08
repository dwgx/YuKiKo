"""C1 工具单轨回归测试 — agent 工具不再穿墙调 router 私有 _method_* / execute。

架构收敛 C1：agent 工具直接调底层能力模块（MusicEngine / 浏览器 / 视频解析模块）
的公开接口，不再经 ToolExecutor.execute 或私有 _method_* 穿墙。

被拆的 5 个工具与目标公开接口：
- fetch_webpage  → core.tools_search.browser_fetch_url
- douyin_search  → core.tools_video.douyin_search_video
- parse_video    → core.tools_video.browser_resolve_video
- analyze_video  → core.tools_video.video_analyze
- music_play     → core.music.MusicEngine.play（直接调）

判据：
1. 源码判据（AST 提取 handler 函数体内的属性调用，不匹配注释/字符串）：
   handler 内不再出现 `_method_*` 属性调用，music_play 不再出现
   `tool_executor.execute(...)`。
2. 功能判据：mock 底层模块后，5 个 handler 均能正常构造并返回 ToolCallResult。
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent_tools_types import ToolCallResult
from core.music import MusicPlayResult, MusicSearchResult
from core.tools_types import ToolResult

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 5 个被拆工具 → (agent_tools 文件相对路径, handler 函数名)
REFACTORED_TOOLS: dict[str, tuple[str, str]] = {
    "fetch_webpage": ("core/agent_tools_web.py", "_handle_fetch_webpage"),
    "douyin_search": ("core/agent_tools_web.py", "_handle_douyin_search"),
    "parse_video": ("core/agent_tools_media.py", "_handle_parse_video"),
    "analyze_video": ("core/agent_tools_media.py", "_handle_analyze_video"),
    "music_play": ("core/agent_tools_admin.py", "_handle_music_play"),
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
    """模拟 ToolExecutor：私有 _method_* 返回 ToolResult，_music_engine 返回 MusicPlayResult。"""
    te = MagicMock()
    te._method_browser_fetch_url = AsyncMock(
        return_value=ToolResult(ok=True, tool_name="browser.fetch_url",
                                payload={"text": "页面摘要：测试网页"}, evidence=[])
    )
    te._method_douyin_search_video = AsyncMock(
        return_value=ToolResult(ok=True, tool_name="douyin.search_video",
                                payload={"text": "抖音搜索结果"}, evidence=[])
    )
    te._method_browser_resolve_video = AsyncMock(
        return_value=ToolResult(ok=True, tool_name="parse_video",
                                payload={"text": "解析成功", "video_url": "https://example.com/v.mp4"},
                                evidence=[])
    )
    te._method_video_analyze = AsyncMock(
        return_value=ToolResult(ok=True, tool_name="analyze_video",
                                payload={
                                    "text": "分析完成",
                                    "analysis_context": "时长: 00:00:30\n标题: 测试视频\n",
                                    "video_url": "https://example.com/v.mp4",
                                },
                                evidence=[])
    )
    te._music_engine = MagicMock()
    te._music_engine.play = AsyncMock(
        return_value=MusicPlayResult(
            ok=True,
            song=MusicSearchResult(song_id=1, name="测试", artist="歌手"),
            audio_path="/tmp/test.mp3",
            message="测试 - 歌手",
        )
    )
    return te


def _build_context(**overrides: Any) -> dict[str, Any]:
    base = {
        "tool_executor": _mock_executor(),
        "api_call": lambda **_: {"status": "ok"},
        "message_text": "测试消息",
        "conversation_id": "group:999:user:10001",
        "raw_segments": [],
        "reply_media_segments": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 源码判据
# ---------------------------------------------------------------------------

class ToolSingleTrackSourceTests(unittest.TestCase):
    """断言 5 个被拆工具的 handler 不再穿墙调私有方法。"""

    def test_no_private_method_calls_in_refactored_handlers(self) -> None:
        for tool, (file_path, handler) in REFACTORED_TOOLS.items():
            attrs = _handler_attribute_calls(file_path, handler)
            private_calls = sorted(a for a in attrs if a.startswith("_method_"))
            self.assertEqual(
                private_calls,
                [],
                f"{tool} handler 仍直接调私有 _method_*: {private_calls}",
            )

    def test_music_play_no_tool_executor_execute(self) -> None:
        attrs = _handler_attribute_calls(
            "core/agent_tools_admin.py", "_handle_music_play"
        )
        self.assertNotIn(
            "execute", attrs,
            "music_play handler 仍经 tool_executor.execute 路由，未直接调 MusicEngine",
        )

    def test_public_interface_functions_exist(self) -> None:
        """公开接口函数应存在于目标能力模块。"""
        from core.music import strip_music_command_prefix
        from core.tools_search import browser_fetch_url
        from core.tools_video import browser_resolve_video, douyin_search_video, video_analyze

        self.assertTrue(callable(browser_fetch_url))
        self.assertTrue(callable(douyin_search_video))
        self.assertTrue(callable(browser_resolve_video))
        self.assertTrue(callable(video_analyze))
        self.assertTrue(callable(strip_music_command_prefix))


# ---------------------------------------------------------------------------
# 功能判据
# ---------------------------------------------------------------------------

class ToolSingleTrackFunctionalTests(unittest.TestCase):
    """断言 5 个被拆工具 mock 底层模块后能正常构造/返回。"""

    def _run(self, handler: Any, args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        return asyncio.run(handler(args, context))

    def test_fetch_webpage(self) -> None:
        from core.agent_tools_web import _handle_fetch_webpage

        result = self._run(_handle_fetch_webpage, {"url": "https://example.com"}, _build_context())
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"fetch_webpage should be ok, got error={result.error}")

    def test_douyin_search(self) -> None:
        from core.agent_tools_web import _handle_douyin_search

        result = self._run(_handle_douyin_search, {"query": "搞笑"}, _build_context())
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"douyin_search should be ok, got error={result.error}")

    def test_parse_video(self) -> None:
        from core.agent_tools_media import _handle_parse_video

        result = self._run(
            _handle_parse_video, {"url": "https://example.com/v.mp4"}, _build_context()
        )
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"parse_video should be ok, got error={result.error}")
        self.assertEqual(result.data.get("video_url"), "https://example.com/v.mp4")

    def test_analyze_video(self) -> None:
        from core.agent_tools_media import _handle_analyze_video

        result = self._run(
            _handle_analyze_video, {"url": "https://example.com/v.mp4"}, _build_context()
        )
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"analyze_video should be ok, got error={result.error}")

    def test_music_play(self) -> None:
        from core.agent_tools_admin import _handle_music_play

        result = self._run(_handle_music_play, {"keyword": "点歌 测试"}, _build_context())
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"music_play should be ok, got error={result.error}")
        self.assertEqual(result.data.get("audio_file"), "/tmp/test.mp3")

    def test_music_play_strips_command_prefix(self) -> None:
        """点歌前缀应被剥离后交给 MusicEngine（与原 router 层行为一致）。"""
        from core.agent_tools_admin import _handle_music_play

        context = _build_context()
        executor = context["tool_executor"]
        result = self._run(
            _handle_music_play, {"keyword": "点歌 稻香"}, context
        )
        self.assertIsInstance(result, ToolCallResult)
        executor._music_engine.play.assert_awaited_once_with(
            "稻香", as_voice=True, title="", artist=""
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    unittest.main(verbosity=2)
