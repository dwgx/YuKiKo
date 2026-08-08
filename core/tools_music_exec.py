"""ToolExecutor 音乐执行 mixin — 点歌、音乐搜索等。

从 core/tools.py 拆分。"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Awaitable
import logging as _logging

from utils.text import clip_text, normalize_text, normalize_matching_text
from core.tools_types import ToolResult, _tool_trace_tag, _normalize_multimodal_query
from core.music import MusicEngine, MusicSearchResult

_tool_log = _logging.getLogger("yukiko.tools")


class ToolMusicExecMixin:
    """Mixin — 从 tools.py ToolExecutor 拆分。"""

    async def _music_search(
        self, tool_args: dict[str, Any], message_text: str
    ) -> ToolResult:
        raw_keyword = str(tool_args.get("keyword", "")).strip()
        _is_url = bool(re.search(r"https?://", raw_keyword))
        keyword = raw_keyword if _is_url else normalize_matching_text(raw_keyword)
        title = normalize_matching_text(str(tool_args.get("title", "")))
        artist = normalize_matching_text(str(tool_args.get("artist", "")))
        if not keyword and title:
            keyword = f"{title} {artist}".strip()
        if not keyword:
            keyword = normalize_matching_text(message_text)
        # 尊重 tool_args 的 limit（原硬编码 5，handler 传 8 被忽略）。
        try:
            search_limit = int(tool_args.get("limit", 5) or 5)
        except (TypeError, ValueError):
            search_limit = 5
        return await search_music_with_intent(
            self._music_engine,
            keyword=keyword,
            title=title,
            artist=artist,
            limit=search_limit,
        )

    async def _music_play_by_id(
        self,
        tool_args: dict[str, Any],
        api_call: Callable[..., Awaitable[Any]] | None,
        group_id: int,
    ) -> ToolResult:
        song_id = int(tool_args.get("song_id", 0) or 0)
        if song_id <= 0:
            return ToolResult(
                ok=False, tool_name="music_play_by_id", error="invalid_song_id"
            )
        return await play_music_by_id(
            self._music_engine,
            song_id=song_id,
            song_name=str(tool_args.get("song_name", "")),
            artist=str(tool_args.get("artist", "")),
            keyword=str(tool_args.get("keyword", "")),
            api_call=api_call,
        )

    async def _music_play(
        self,
        tool_args: dict[str, Any],
        message_text: str,
        api_call: Callable[..., Awaitable[Any]] | None,
        group_id: int,
    ) -> ToolResult:
        raw_keyword = str(tool_args.get("keyword", "")).strip()
        _is_url = bool(re.search(r"https?://", raw_keyword))
        keyword = raw_keyword if _is_url else normalize_matching_text(raw_keyword)
        title = normalize_matching_text(str(tool_args.get("title", "")))
        artist = normalize_matching_text(str(tool_args.get("artist", "")))
        if not keyword and title:
            keyword = f"{title} {artist}".strip()
        if not keyword:
            keyword = normalize_matching_text(message_text)
        if not keyword:
            return ToolResult(ok=False, tool_name="music_play", error="empty_keyword")
        for prefix in (
            "点歌",
            "听歌",
            "放歌",
            "搜歌",
            "播放",
            "来首",
            "来一首",
            "唱",
            "/music",
            "/song",
        ):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix) :].strip()
        if not keyword:
            return ToolResult(ok=False, tool_name="music_play", error="empty_keyword")

        result = await self._music_engine.play(
            keyword, as_voice=True, title=title, artist=artist
        )
        if not result.ok:
            return ToolResult(
                ok=False,
                tool_name="music_play",
                payload={"text": result.message or "播放失败。"},
                error=result.error,
            )

        payload: dict[str, Any] = {"text": result.message}

        # 优先给完整音频文件，发送层可按策略决定“整段发 / 分段发 / 回退 silk”。
        if result.audio_path and api_call:
            payload["audio_file"] = result.audio_path
            if result.silk_path:
                payload["audio_file_silk"] = result.silk_path
        elif result.silk_path and api_call:
            payload["audio_file"] = result.silk_path
        elif result.silk_b64 and api_call:
            # 仅在无本地文件时才用 base64
            payload["record_b64"] = result.silk_b64

        return ToolResult(ok=True, tool_name="music_play", payload=payload)

    @staticmethod
    def _looks_like_music_request(text: str) -> bool:
        content = _normalize_multimodal_query(text).lower()
        if not content:
            return False
        plain = re.sub(r"\s+", "", content)
        explicit_tokens = (
            "/music",
            "/song",
            "action=music",
            "tool=music",
            "intent=music",
            "music=1",
        )
        if any(token in plain for token in explicit_tokens):
            return True
        explicit_patterns = (
            r"(?:^|\s)/(?:music|song)(?:\s|$)",
            r"(?:^|\s)(?:action|tool|intent)\s*=\s*music(?:\s|$)",
        )
        return any(re.search(pattern, content) for pattern in explicit_patterns)


# ── 公开接口（架构收敛 C1c）──────────────────────────────────────────
# agent 工具层不再经 tool_executor.execute(action="music_*") 路由，改走这里的
# 模块级公开函数。私有实现留在 mixin 内委托到公开函数，router 行为不变。


async def search_music_with_intent(
    engine: Any,
    *,
    keyword: str,
    title: str = "",
    artist: str = "",
    limit: int = 5,
) -> ToolResult:
    """搜索歌曲并按标题/歌手意图过滤（music_search 底层能力）。

    承接原 router 层 _music_search 的关键词归一化与点歌前缀剥离逻辑，agent
    工具层不需要再抄一份前缀表（同 _handle_music_play 复用 strip_music_command_prefix）。
    """
    raw_keyword = str(keyword or "").strip()
    is_url = bool(re.search(r"https?://", raw_keyword))
    keyword = raw_keyword if is_url else normalize_matching_text(raw_keyword)
    title = normalize_matching_text(str(title or ""))
    artist = normalize_matching_text(str(artist or ""))
    if not keyword and title:
        keyword = f"{title} {artist}".strip()
    if not keyword:
        return ToolResult(ok=False, tool_name="music_search", error="empty_keyword")
    # 去掉常见前缀
    for prefix in (
        "点歌",
        "听歌",
        "放歌",
        "搜歌",
        "播放",
        "来首",
        "来一首",
        "唱",
        "/music",
        "/song",
    ):
        if keyword.startswith(prefix):
            keyword = keyword[len(prefix) :].strip()
    if not keyword:
        return ToolResult(ok=False, tool_name="music_search", error="empty_keyword")

    # 尊重调用方传入的 limit（原硬编码 5，handler 传 8 被忽略）。
    try:
        search_limit = int(limit or 5)
    except (TypeError, ValueError):
        search_limit = 5
    search_limit = max(1, min(20, search_limit))
    results = await engine.search(
        keyword, limit=search_limit, title=title, artist=artist
    )
    if not results:
        return ToolResult(
            ok=False,
            tool_name="music_search",
            payload={"text": f"没找到「{keyword}」相关的歌曲。"},
            error="no_results",
        )

    filtered_results = results
    if title:
        intent = engine._build_keyword_intent(
            keyword=keyword, title=title, artist=artist
        )
        exact: list[MusicSearchResult] = []
        title_hits: list[MusicSearchResult] = []
        for row in results:
            if engine._title_match_level(intent.title_hint, row.name) < 2:
                continue
            if engine._should_avoid_version(row.name, intent.title_hint):
                continue
            title_hits.append(row)
            if artist and not engine._artist_matches_intent(row.artist, intent):
                continue
            exact.append(row)

        if exact:
            filtered_results = exact
        else:
            if artist and title_hits:
                return ToolResult(
                    ok=False,
                    tool_name="music_search",
                    payload={
                        "text": f"找到同名候选，但和歌手「{artist}」不一致，不能直接替你播。",
                        "results": [],
                    },
                    error="artist_mismatch",
                )
            return ToolResult(
                ok=False,
                tool_name="music_search",
                payload={
                    "text": f"没找到和《{title}》明确匹配的歌曲，近似名称不能直接当成同一首播。",
                    "results": [],
                },
                error="no_exact_match",
            )

    lines = [f"🎵 搜索「{keyword}」找到 {len(filtered_results)} 首歌："]
    for i, s in enumerate(filtered_results, 1):
        dur = (
            f" ({s.duration_ms // 1000 // 60}:{s.duration_ms // 1000 % 60:02d})"
            if s.duration_ms
            else ""
        )
        lines.append(f"{i}. {s.name} - {s.artist}{dur}")
    lines.append("\n发送「点歌 歌名」可以直接播放。")
    return ToolResult(
        ok=True,
        tool_name="music_search",
        payload={
            "text": "\n".join(lines),
            "results": [
                {"id": s.song_id, "name": s.name, "artist": s.artist}
                for s in filtered_results
            ],
        },
    )


async def play_music_by_id(
    engine: Any,
    *,
    song_id: int,
    song_name: str = "",
    artist: str = "",
    keyword: str = "",
    api_call: Callable[..., Awaitable[Any]] | None = None,
) -> ToolResult:
    """按歌曲 ID 播放（music_play_by_id 底层能力）。"""
    song_id = int(song_id or 0)
    if song_id <= 0:
        return ToolResult(
            ok=False, tool_name="music_play_by_id", error="invalid_song_id"
        )

    # 从参数获取歌曲信息
    song_name = normalize_matching_text(str(song_name or ""))
    artist = normalize_matching_text(str(artist or ""))
    keyword = normalize_matching_text(str(keyword or ""))
    require_checker = getattr(engine, "_requires_verified_original", None)
    if callable(require_checker):
        require_verified_original = bool(require_checker(keyword))
    else:
        require_verified_original = bool(
            MusicEngine._requires_verified_original(keyword)
        )

    matched_row: MusicSearchResult | None = None
    if keyword or song_name:
        query = keyword or f"{song_name} {artist}".strip()
        try:
            candidates = await engine.search(
                query, limit=12, title=song_name, artist=artist
            )
        except Exception:
            candidates = []
        for row in candidates:
            if int(getattr(row, "song_id", 0) or 0) != song_id:
                continue
            matched_row = row
            if normalize_text(getattr(row, "name", "")):
                song_name = normalize_matching_text(str(row.name))
            if normalize_text(getattr(row, "artist", "")):
                artist = normalize_matching_text(str(row.artist))
            break

    song = MusicSearchResult(
        song_id=song_id,
        name=song_name,
        artist=artist,
        album="",
        duration_ms=0,
        source="netease",
    )
    if matched_row is not None:
        song.name = (
            normalize_text(str(getattr(matched_row, "name", song.name))) or song.name
        )
        song.artist = (
            normalize_text(str(getattr(matched_row, "artist", song.artist)))
            or song.artist
        )
        song.duration_ms = int(getattr(matched_row, "duration_ms", 0) or 0)
        song.source = (
            normalize_text(str(getattr(matched_row, "source", song.source)))
            or song.source
        )
        song.source_url = normalize_text(
            str(getattr(matched_row, "source_url", ""))
        )

    result = await engine._play_song(
        song,
        as_voice=True,
        require_verified_original=require_verified_original,
    )

    if not result.ok:
        return ToolResult(
            ok=False,
            tool_name="music_play_by_id",
            payload={"text": result.message or "播放失败。"},
            error=result.error,
        )

    payload: dict[str, Any] = {"text": result.message}

    # 优先给完整音频文件
    if result.audio_path and api_call:
        payload["audio_file"] = result.audio_path
        if result.silk_path:
            payload["audio_file_silk"] = result.silk_path
    elif result.silk_path and api_call:
        payload["audio_file"] = result.silk_path
    elif result.silk_b64 and api_call:
        payload["record_b64"] = result.silk_b64

    return ToolResult(ok=True, tool_name="music_play_by_id", payload=payload)
