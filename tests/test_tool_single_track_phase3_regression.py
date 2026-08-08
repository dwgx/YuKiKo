"""C1c 工具单轨回归测试 — 拆除剩余 agent 工具经 tool_executor.execute 的 call-through。

架构收敛 C1c：C1/C1b 已拆 9 个工具（fetch_webpage / douyin_search / parse_video /
analyze_video / music_play / github_search / github_readme / analyze_image /
smart_download）。剩余 5 个仍经 tool_executor.execute(action=...) 路由到 router
层，本测试锁定这批工具改为直接调用底层能力：

- music_search           → core.tools_music_exec.search_music_with_intent
- music_play_by_id       → core.tools_music_exec.play_music_by_id
- bilibili_audio_extract → core.tools_video.bilibili_audio_extract_video
- search_media           → core.tools_search.search_media
- analyze_image          → core.tools_vision.media_analyze_image（C1b 已拆，本批锁定）

判据：
1. 源码判据（AST 提取 handler 函数体内的属性调用，不匹配注释/字符串）：
   handler 内不再出现 `tool_executor.execute`。
2. 功能判据：mock 底层公开函数后，5 个 handler 均能正常返回 ToolCallResult。
3. search_music_with_intent 行为判据：前缀剥离与标题/歌手意图过滤与 router 层一致。
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent_tools_types import ToolCallResult
from core.music import MusicEngine, MusicSearchResult
from core.tools_types import ToolResult

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 5 个被拆工具 → (agent_tools 文件相对路径, handler 函数名)
REFACTORED_TOOLS: dict[str, tuple[str, str]] = {
    "music_search": ("core/agent_tools_admin.py", "_handle_music_search"),
    "music_play_by_id": ("core/agent_tools_admin.py", "_handle_music_play_by_id"),
    "bilibili_audio_extract": (
        "core/agent_tools_admin.py",
        "_handle_bilibili_audio_extract",
    ),
    "search_media": ("core/agent_tools_search.py", "_make_search_media_handler"),
    "analyze_image": ("core/agent_tools_media.py", "_handle_analyze_image"),
}


def _handler_attribute_calls(file_path: str, handler_name: str) -> set[str]:
    """提取 handler 函数体内出现的所有属性调用名（AST，忽略注释/字符串）。

    search_media 的 handler 是工厂函数 _make_search_media_handler 内的闭包，
    ast.walk 会递归进嵌套函数，所以对工厂函数同样适用。
    """
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
    """模拟 ToolExecutor：不自动生成 _music_engine（handler 会回退到 context）。"""
    te = MagicMock()
    te._music_engine = None
    return te


def _build_context(**overrides: Any) -> dict[str, Any]:
    base = {
        "tool_executor": _mock_executor(),
        "music_engine": MagicMock(),
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


class ToolSingleTrackPhase3SourceTests(unittest.TestCase):
    """断言 5 个被拆工具的 handler 不再经 tool_executor.execute 路由。"""

    def test_no_tool_executor_execute_in_refactored_handlers(self) -> None:
        for tool, (file_path, handler) in REFACTORED_TOOLS.items():
            attrs = _handler_attribute_calls(file_path, handler)
            self.assertNotIn(
                "execute",
                attrs,
                f"{tool} handler 仍经 tool_executor.execute 路由，未直接调底层公开函数",
            )

    def test_public_interface_functions_exist(self) -> None:
        """公开接口函数应存在于目标能力模块。"""
        from core.tools_music_exec import play_music_by_id, search_music_with_intent
        from core.tools_search import search_media
        from core.tools_video import bilibili_audio_extract_video
        from core.tools_vision import media_analyze_image

        self.assertTrue(callable(search_music_with_intent))
        self.assertTrue(callable(play_music_by_id))
        self.assertTrue(callable(bilibili_audio_extract_video))
        self.assertTrue(callable(search_media))
        self.assertTrue(callable(media_analyze_image))


# ---------------------------------------------------------------------------
# 功能判据
# ---------------------------------------------------------------------------


class ToolSingleTrackPhase3FunctionalTests(unittest.TestCase):
    """断言 5 个被拆工具 mock 底层公开函数后能正常构造/返回。"""

    def _run(self, handler: Any, args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        return asyncio.run(handler(args, context))

    def test_music_search(self) -> None:
        from core.agent_tools_admin import _handle_music_search

        context = _build_context()
        engine = context["music_engine"]
        search_fn = AsyncMock(
            return_value=ToolResult(
                ok=True,
                tool_name="music_search",
                payload={
                    "text": "🎵 搜索「稻香」找到 1 首歌：",
                    "results": [{"id": 123, "name": "稻香", "artist": "周杰伦"}],
                },
            )
        )
        with patch("core.tools_music_exec.search_music_with_intent", search_fn):
            result = self._run(_handle_music_search, {"keyword": "点歌 稻香"}, context)

        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"music_search should be ok, got error={result.error}")
        self.assertEqual(result.data.get("results"), [{"id": 123, "name": "稻香", "artist": "周杰伦"}])
        self.assertIn("稻香", result.display)
        search_fn.assert_awaited_once_with(engine, keyword="点歌 稻香", title="", artist="", limit=8)

    def test_music_search_failure_keeps_message(self) -> None:
        """失败时 payload.text 要进 display（与 router 层行为一致）。"""
        from core.agent_tools_admin import _handle_music_search

        search_fn = AsyncMock(
            return_value=ToolResult(
                ok=False,
                tool_name="music_search",
                error="no_results",
                payload={"text": "没找到「稻香」相关的歌曲。"},
            )
        )
        with patch("core.tools_music_exec.search_music_with_intent", search_fn):
            result = self._run(_handle_music_search, {"keyword": "稻香"}, _build_context())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_results")
        self.assertIn("没找到", result.display)

    def test_music_play_by_id(self) -> None:
        from core.agent_tools_admin import _handle_music_play_by_id

        context = _build_context()
        engine = context["music_engine"]
        play_fn = AsyncMock(
            return_value=ToolResult(
                ok=True,
                tool_name="music_play_by_id",
                payload={"text": "稻香 - 周杰伦", "audio_file": "/tmp/test.mp3"},
            )
        )
        with patch("core.tools_music_exec.play_music_by_id", play_fn):
            result = self._run(
                _handle_music_play_by_id,
                {"song_id": 123, "song_name": "稻香", "artist": "周杰伦"},
                context,
            )

        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"music_play_by_id should be ok, got error={result.error}")
        self.assertEqual(result.data.get("audio_file"), "/tmp/test.mp3")
        self.assertEqual(result.data.get("audio_file_silk"), None)
        play_fn.assert_awaited_once_with(
            engine,
            song_id=123,
            song_name="稻香",
            artist="周杰伦",
            keyword="",
            api_call=context["api_call"],
        )

    def test_music_play_by_id_failure_keeps_message(self) -> None:
        """失败时 payload.text 要进 display（模型据此决定换关键词还是放弃）。"""
        from core.agent_tools_admin import _handle_music_play_by_id

        play_fn = AsyncMock(
            return_value=ToolResult(
                ok=False,
                tool_name="music_play_by_id",
                error="play_failed",
                payload={"text": "没找到与歌手「宋岳庭」匹配的可播版本，请换个关键词或指定歌曲ID。"},
            )
        )
        with patch("core.tools_music_exec.play_music_by_id", play_fn):
            result = self._run(_handle_music_play_by_id, {"song_id": 1}, _build_context())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "play_failed")
        self.assertIn("宋岳庭", result.display)

    def test_bilibili_audio_extract(self) -> None:
        from core.agent_tools_admin import _handle_bilibili_audio_extract

        context = _build_context()
        extract_fn = AsyncMock(
            return_value=ToolResult(
                ok=True,
                tool_name="bilibili_audio_extract",
                payload={"audio_file": "/tmp/b.mp3", "text": "已从 B 站提取音频：测试"},
            )
        )
        with patch("core.tools_video.bilibili_audio_extract_video", extract_fn):
            result = self._run(_handle_bilibili_audio_extract, {"keyword": "测试"}, context)

        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"bilibili_audio_extract should be ok, got error={result.error}")
        self.assertEqual(result.data.get("audio_file"), "/tmp/b.mp3")
        extract_fn.assert_awaited_once_with(
            context["tool_executor"],
            keyword="测试",
            message_text="测试",
            api_call=context["api_call"],
            group_id=999,
        )

    def test_search_media_image(self) -> None:
        from core.agent_tools_search import _make_search_media_handler

        handler = _make_search_media_handler(MagicMock())
        context = _build_context()
        search_fn = AsyncMock(
            return_value=ToolResult(
                ok=True,
                tool_name="search_image",
                payload={"text": "先给你一张图", "image_url": "https://example.com/a.png"},
            )
        )
        with patch("core.tools_search.search_media", search_fn):
            result = self._run(handler, {"query": "猫", "media_type": "image"}, context)

        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"search_media should be ok, got error={result.error}")
        self.assertEqual(result.data.get("media_type"), "image")
        self.assertEqual(result.data.get("query"), "猫")
        self.assertEqual(result.data.get("mode"), "image")
        # 有 text 时直接透传底层文案（与 router 层行为一致）
        self.assertIn("先给你一张图", result.display)
        search_fn.assert_awaited_once()
        self.assertEqual(search_fn.await_args.kwargs["query"], "猫")
        self.assertEqual(search_fn.await_args.kwargs["mode"], "image")
        self.assertEqual(search_fn.await_args.kwargs["group_id"], 999)

    def test_search_media_video_mode(self) -> None:
        """video 类型应把 media_type 映射为 video 模式透传。"""
        from core.agent_tools_search import _make_search_media_handler

        handler = _make_search_media_handler(MagicMock())
        search_fn = AsyncMock(
            return_value=ToolResult(
                ok=True,
                tool_name="search_video",
                payload={"text": "找到视频", "video_url": "https://example.com/v.mp4"},
            )
        )
        with patch("core.tools_search.search_media", search_fn):
            result = self._run(handler, {"query": "猫咪", "media_type": "video"}, _build_context())

        self.assertTrue(result.ok, f"search_media should be ok, got error={result.error}")
        self.assertEqual(result.data.get("media_type"), "video")
        self.assertEqual(search_fn.await_args.kwargs["mode"], "video")

    def test_search_media_failure_display(self) -> None:
        """失败时 display 不能是成功话术，否则模型会误导回复。"""
        from core.agent_tools_search import _make_search_media_handler

        handler = _make_search_media_handler(MagicMock())
        search_fn = AsyncMock(
            return_value=ToolResult(ok=False, tool_name="search_image", error="blocked_image_request", payload={})
        )
        with patch("core.tools_search.search_media", search_fn):
            result = self._run(handler, {"query": "猫", "media_type": "image"}, _build_context())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "blocked_image_request")
        self.assertIn("媒体搜索失败", result.display)

    def test_analyze_image_explicit_url(self) -> None:
        from core.agent_tools_media import _handle_analyze_image

        executor = MagicMock()
        executor._method_media_analyze_image = AsyncMock(
            return_value=ToolResult(
                ok=True,
                tool_name="analyze_image",
                payload={"text": "这是一只猫", "analysis": "这是一只猫"},
                evidence=[],
            )
        )
        context = _build_context(tool_executor=executor)
        result = self._run(
            _handle_analyze_image,
            {"url": "https://example.com/cat.png", "question": "这是什么？"},
            context,
        )
        self.assertIsInstance(result, ToolCallResult)
        self.assertTrue(result.ok, f"analyze_image should be ok, got error={result.error}")
        self.assertEqual(result.data.get("analysis"), "这是一只猫")


# ---------------------------------------------------------------------------
# 公开函数行为判据（search_music_with_intent 承载了 router 层前缀/意图逻辑）
# ---------------------------------------------------------------------------


class SearchMusicWithIntentBehaviorTests(unittest.TestCase):
    """公开函数本身：点歌前缀剥离 + 标题/歌手意图过滤（与原 router 层一致）。

    用真实 MusicEngine 的类方法做意图判定，engine 只 stub 掉 search 与播放。
    """

    class _FakeMusicEngine:
        _build_keyword_intent = MusicEngine._build_keyword_intent
        _title_match_level = MusicEngine._title_match_level
        _should_avoid_version = MusicEngine._should_avoid_version
        _artist_matches_intent = MusicEngine._artist_matches_intent

        def __init__(self, results: list[MusicSearchResult]) -> None:
            self._results = results

        async def search(self, keyword: str, limit: int = 5, **kwargs: Any) -> list[MusicSearchResult]:
            return self._results

    def _run(self, fn: Any, *args: Any, **kwargs: Any) -> ToolResult:
        return asyncio.run(fn(*args, **kwargs))

    def test_strips_command_prefix(self) -> None:
        from core.tools_music_exec import search_music_with_intent

        engine = self._FakeMusicEngine([MusicSearchResult(song_id=1, name="稻香", artist="周杰伦")])
        result = self._run(search_music_with_intent, engine, keyword="点歌 稻香", limit=8)
        self.assertTrue(result.ok, f"expected ok, got {result.error}")
        self.assertEqual(result.payload["results"], [{"id": 1, "name": "稻香", "artist": "周杰伦"}])

    def test_artist_mismatch_rejected(self) -> None:
        from core.tools_music_exec import search_music_with_intent

        engine = self._FakeMusicEngine([MusicSearchResult(song_id=1, name="稻香", artist="林俊杰")])
        result = self._run(search_music_with_intent, engine, keyword="稻香", title="稻香", artist="周杰伦")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "artist_mismatch")

    def test_no_title_match_rejected(self) -> None:
        from core.tools_music_exec import search_music_with_intent

        engine = self._FakeMusicEngine([MusicSearchResult(song_id=1, name="晴天", artist="周杰伦")])
        result = self._run(search_music_with_intent, engine, keyword="稻香", title="稻香", artist="周杰伦")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_exact_match")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    unittest.main(verbosity=2)
