"""Music search and playback engine.

Capabilities:
- Search via Alger API (NeteaseCloudMusicApi wrapper), fallback to Netease official API
- Respect the query keyword from upper-layer agent without local version preference reranking
- Fetch playable URL and optional lyrics
- Download mp3 and convert to QQ-compatible SILK
- Local alternative source matching (QQ Music, Kuwo, Kugou, Migu)
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from core.soundcloud import SoundCloudClient
from utils.media import sniff_audio_container
from utils.text import has_unrequested_title_qualifier, normalize_matching_text, normalize_text

_log = logging.getLogger("yukiko.music")


@dataclass(slots=True)
class MusicSearchResult:
    """Single music search item."""

    song_id: int = 0
    name: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    source: str = "netease"
    source_url: str = ""


@dataclass(slots=True)
class MusicPlayResult:
    """Music play result."""

    ok: bool = False
    song: MusicSearchResult | None = None
    audio_path: str = ""
    silk_path: str = ""
    silk_b64: str = ""
    message: str = ""
    error: str = ""


@dataclass(slots=True)
class MusicPlayUrl:
    """Resolved playable URL metadata."""

    url: str = ""
    duration_ms: int = 0
    is_trial: bool = False
    source: str = ""
    level: str = ""


@dataclass(slots=True)
class MusicKeywordIntent:
    title_hint: str = ""
    artist_hint: str = ""
    artist_tokens: tuple[str, ...] = ()


class MusicEngine:
    """Search + play + silk conversion."""

    # 默认不预设任何第三方聚合 API：旧默认 http://mc.alger.fun/api 实测已失效（503），
    # 且是明文 HTTP。业主想用聚合源就自己在 music.api_bases 里填 HTTPS 候选。
    # 留空时搜索直接走 netease 官方 HTTPS + core/music_sources.py 本地音源。
    _DEFAULT_API_BASE = ""
    _NETEASE_SEARCH_URL = "https://music.163.com/api/search/get"
    _NETEASE_PLAYER_URL = "https://music.163.com/api/song/enhance/player/url"
    _ALGER_PLAYER_URL_V1 = "/song/url/v1"
    _ALGER_PLAYER_URL = "/song/url"
    _ALGER_SEARCH_URL = "/search"
    _COMMON_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    _DEFAULT_MAX_VOICE_DURATION_S = 0  # 0 means no truncation
    _DEFAULT_TRIAL_MAX_DURATION_MS = 35_000
    _BREAK_LIMIT_MIN_FULL_MS = 90_000
    # 聚合源整体墙钟预算：实测挂掉的上游一次 503 就要 5.1s，旧逻辑一个 query variant
    # 能烧 7 次 ≈ 39s，直接吃满 agent 的 tool_timeout_seconds=28，可用的 netease 腿
    # （实测 0.9s 出 10 条）永远轮不到。预算到点就放弃聚合源，交给后面的兜底。
    _DEFAULT_UPSTREAM_BUDGET_S = 8.0
    # 同一 host 判定不可达后的静默期：期内跳过，不再发请求也不再烧预算。
    _DEFAULT_UNREACHABLE_COOLDOWN_S = 300.0
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        music_cfg = cfg.get("music", cfg) if isinstance(cfg, dict) else {}

        self._enable = bool(music_cfg.get("enable", True))
        self._allow_insecure_api_base = bool(music_cfg.get("allow_insecure_api_base", False))
        self._api_bases = self._resolve_api_bases(music_cfg)
        # 兼容仍按单个基址读的调用方（get_lyrics 等）。
        self._api_base = self._api_bases[0] if self._api_bases else ""
        self._cache_dir = Path(music_cfg.get("cache_dir", "storage/cache/music"))
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._timeout = float(music_cfg.get("timeout_seconds", 10))
        self._upstream_budget_s = max(
            2.0, float(music_cfg.get("upstream_budget_seconds", self._DEFAULT_UPSTREAM_BUDGET_S))
        )
        self._unreachable_cooldown_s = max(
            0.0,
            float(music_cfg.get("unreachable_cooldown_seconds", self._DEFAULT_UNREACHABLE_COOLDOWN_S)),
        )
        # host → 静默期截止时间戳（熔断器）。
        self._unreachable_until: dict[str, float] = {}
        # 「本次工具调用」的聚合源总预算截止点。None = 不在一次操作里。
        # 必须是每次 search()/play() 一个，而不是每次 _search_alger()/_get_alger_url() 一个：
        # search() 对每个 query variant 各调一次 _search_alger（实测
        # _build_search_queries('蛋堡 热水澡') 产出 4 个 variant），
        # play() 对最多 8 个候选各调一次 _get_alger_url ——
        # 预算写 8 秒、实际能烧 32~64 秒，「秒级放弃」的契约就是假的。
        self._operation_deadline: float | None = None
        self._ffmpeg = shutil.which("ffmpeg") or self._find_bundled_ffmpeg() or ""
        self._pilk_available = self._check_pilk()

        self._max_voice_duration_s = max(
            0,
            int(music_cfg.get("max_voice_duration_seconds", self._DEFAULT_MAX_VOICE_DURATION_S)),
        )
        self._silk_encode_timeout_s = max(30, int(music_cfg.get("silk_encode_timeout_seconds", 180)))
        self._silk_bit_rate = max(8000, int(music_cfg.get("silk_bit_rate", 32000)))
        self._max_cache_files = int(music_cfg.get("cache_keep_files", 50))
        self._break_limit_enable = bool(music_cfg.get("break_limit_enable", True))
        trial_raw = music_cfg.get("trial_max_duration_ms", self._DEFAULT_TRIAL_MAX_DURATION_MS)
        try:
            trial_cfg_raw = int(trial_raw or 0)
        except Exception:
            trial_cfg_raw = self._DEFAULT_TRIAL_MAX_DURATION_MS
        self._trial_max_duration_ms = max(0, trial_cfg_raw)
        self._artist_guard_enable = bool(music_cfg.get("artist_guard_enable", True))
        self._artist_guard_allow_mismatch_fallback = bool(
            music_cfg.get("artist_guard_allow_mismatch_fallback", False)
        )

        # UnblockNeteaseMusic 配置
        self._unblock_enable = bool(music_cfg.get("unblock_enable", False))
        self._unblock_api_base = str(music_cfg.get("unblock_api_base", "")).rstrip("/")
        self._unblock_sources = str(
            music_cfg.get("unblock_sources", "qq,kuwo,kugou,migu,soundcloud")
        ).strip()

        # 本地音源匹配器
        self._local_source_enable = bool(music_cfg.get("local_source_enable", True))
        self._source_matcher = None
        if self._local_source_enable:
            try:
                from core.music_sources import MusicSourceMatcher
                self._source_matcher = MusicSourceMatcher(timeout=self._timeout)
            except Exception as exc:
                _log.warning("music_source_matcher_init_fail | %s", exc)
                self._source_matcher = None

        self._alger_discovered_api_bases: list[str] = []
        self._soundcloud_client = SoundCloudClient(timeout=max(8.0, self._timeout))

    @staticmethod
    def _find_bundled_ffmpeg() -> str:
        """Try to find ffmpeg bundled by imageio-ffmpeg."""
        try:
            import imageio_ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path:
                return str(path)
        except Exception:
            pass
        return ""

    @staticmethod
    def _check_pilk() -> bool:
        try:
            import pilk  # noqa: F401

            return True
        except Exception:
            pass
        try:
            import pysilk  # noqa: F401

            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        """Compatibility hook."""
        return None

    # ── URL 直链检测 ──────────────────────────────────────────────
    _URL_PLATFORM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        ("soundcloud", re.compile(r"https?://(?:www\.)?soundcloud\.com/", re.I)),
        ("youtube", re.compile(r"https?://(?:www\.|m\.|music\.)?youtu(?:be\.com|\.be)/", re.I)),
        ("spotify", re.compile(r"https?://(?:open\.)?spotify\.com/", re.I)),
        ("bandcamp", re.compile(r"https?://[a-z0-9\-]+\.bandcamp\.com/", re.I)),
    ]

    @staticmethod
    def _detect_url_platform(text: str) -> tuple[str, str]:
        """如果 text 含外链，返回 (platform, url)；否则 ("", "")。"""
        url_match = re.search(r"(https?://[^\s<>\"']+)", text)
        if not url_match:
            return "", ""
        url = url_match.group(1)
        for platform, pattern in MusicEngine._URL_PLATFORM_PATTERNS:
            if pattern.search(url):
                return platform, url
        return "", ""

    async def _resolve_url_as_search_result(self, platform: str, url: str) -> list[MusicSearchResult]:
        """将外链直接解析为 MusicSearchResult，跳过关键词搜索。"""
        if platform == "soundcloud":
            try:
                audio = await self._soundcloud_client.resolve_page_audio(url)
                if audio and (audio.audio_url or audio.page_url):
                    return [MusicSearchResult(
                        song_id=audio.track_id or 0,
                        name=audio.title or "SoundCloud Track",
                        artist=audio.artist or "",
                        duration_ms=0,
                        source="soundcloud",
                        source_url=audio.page_url or url,
                    )]
            except Exception as exc:
                _log.warning("url_resolve_soundcloud_fail | url=%s | %s", url[:160], exc)
        # YouTube / Spotify / Bandcamp / 其他：构造占位结果，让 _play_song 走 yt-dlp
        return [MusicSearchResult(
            song_id=0,
            name=f"{platform} track",
            artist="",
            duration_ms=0,
            source=platform,
            source_url=url,
        )]

    async def search(
        self,
        keyword: str,
        limit: int = 5,
        *,
        title: str = "",
        artist: str = "",
    ) -> list[MusicSearchResult]:
        """Search songs with exact-title preference and Alger-first query variants."""
        # ── URL 短路：必须在 normalize 之前检测（normalize 会破坏 URL 结构） ──
        raw_keyword = str(keyword or "").strip()
        platform, url = self._detect_url_platform(raw_keyword)
        if platform:
            _log.info("music_search_url_shortcut | platform=%s | url=%s", platform, url[:160])
            results = await self._resolve_url_as_search_result(platform, url)
            if results:
                return results[:limit]

        keyword = normalize_matching_text(keyword)
        title = normalize_matching_text(title)
        artist = normalize_matching_text(artist)
        query = build_music_keyword(keyword, title, artist)
        if not self._enable or not (query or keyword or title or artist):
            return []

        intent = self._build_keyword_intent(keyword=keyword, title=title, artist=artist)
        query_variants = self._build_search_queries(keyword=keyword, title=title, artist=artist, intent=intent)
        fetch_limit = max(limit * 3, 20)
        merge_limit = fetch_limit * 10

        results: list[MusicSearchResult] = []
        found_precise = False

        for query_variant in query_variants:
            rows = await self._search_alger(query_variant, fetch_limit)
            results = self._merge_unique_results(results, rows, merge_limit)
            if self._contains_precise_match(rows, intent=intent):
                found_precise = True
                break

        if not results or not found_precise:
            for query_variant in query_variants:
                rows = await self._search_netease(query_variant, fetch_limit)
                results = self._merge_unique_results(results, rows, merge_limit)
                if self._contains_precise_match(rows, intent=intent):
                    break

        if intent.title_hint and not self._contains_precise_match(results, intent=intent):
            for query_variant in query_variants:
                rows = await self._search_soundcloud(query_variant, fetch_limit)
                results = self._merge_unique_results(results, rows, merge_limit)
                if self._contains_precise_match(rows, intent=intent):
                    break

        results = self._rank_search_results(results, query or keyword or title or artist, intent=intent)
        return results[:limit]

    @staticmethod
    def _split_artist_song(keyword: str) -> tuple[str, str]:
        kw = normalize_matching_text(keyword)
        if not kw:
            return "", ""
        if "的" in kw:
            left, right = kw.split("的", 1)
            left = normalize_matching_text(left)
            right = normalize_matching_text(right)
            if len(left) >= 2 and right:
                return left, right
        parts = [part for part in re.split(r"\s+", kw) if part]
        if len(parts) >= 2:
            # 中文点歌惯例「歌名 歌手」：「晴天 周杰伦」→ title=晴天, artist=周杰伦。
            # 末段当歌手、前面当歌名（原实现首段当歌手，把「晴天 周杰伦」解析成
            # 查《周杰伦》这首歌，实测点歌必失败）。
            artist = parts[-1]
            title = " ".join(parts[:-1])
            if len(artist) >= 2 and title:
                return artist, title
        return "", kw

    @classmethod
    def _build_keyword_intent(
        cls,
        *,
        keyword: str,
        title: str = "",
        artist: str = "",
    ) -> MusicKeywordIntent:
        title_hint = normalize_matching_text(title)
        artist_hint = normalize_matching_text(artist)
        if not title_hint:
            parsed_artist, parsed_title = cls._split_artist_song(keyword)
            if parsed_title:
                title_hint = parsed_title
            if parsed_artist and not artist_hint:
                artist_hint = parsed_artist
        artist_tokens = tuple(
            dict.fromkeys(token for token in re.split(r"[\s,/|]+", artist_hint.lower()) if token)
        )
        return MusicKeywordIntent(
            title_hint=title_hint,
            artist_hint=artist_hint,
            artist_tokens=artist_tokens,
        )

    @classmethod
    def _build_search_queries(
        cls,
        *,
        keyword: str,
        title: str,
        artist: str,
        intent: MusicKeywordIntent,
    ) -> list[str]:
        queries: list[str] = []

        def _add(value: str) -> None:
            normalized = normalize_matching_text(value)
            if normalized and normalized not in queries:
                queries.append(normalized)

        if title and artist:
            _add(f"{title} {artist}")
            _add(f"{artist} {title}")
            _add(title)
        elif title:
            _add(title)
        elif intent.artist_hint and intent.title_hint:
            _add(f"{intent.artist_hint} {intent.title_hint}")
            _add(f"{intent.title_hint} {intent.artist_hint}")
            _add(intent.title_hint)
        elif keyword:
            _add(keyword)

        expanded = build_music_keyword(keyword, title, artist)
        _add(expanded)
        _add(keyword)
        if title and not artist:
            _add(title)
        if intent.title_hint and intent.title_hint != title:
            _add(intent.title_hint)
        return queries

    @classmethod
    def _contains_precise_match(cls, rows: list[MusicSearchResult], *, intent: MusicKeywordIntent) -> bool:
        if not rows:
            return False
        if not intent.title_hint and not intent.artist_hint:
            return True
        for row in rows[:8]:
            if intent.title_hint:
                if cls._title_match_level(intent.title_hint, row.name) < 2:
                    continue
                if cls._should_avoid_version(row.name, intent.title_hint):
                    continue
            if intent.artist_hint and not cls._artist_matches_intent(row.artist, intent):
                continue
            return True
        return False

    @staticmethod
    def _compact_text(text: str) -> str:
        return re.sub(r"[\s\-\_·•./|\\,，;；:&()（）\[\]{}]+", "", normalize_matching_text(text).lower())

    @classmethod
    def _title_match_level(cls, expected_title: str, actual_title: str) -> int:
        expected_raw = normalize_matching_text(expected_title).lower()
        actual_raw = normalize_matching_text(actual_title).lower()
        expected = cls._compact_text(expected_raw)
        actual = cls._compact_text(actual_raw)
        if not expected or not actual:
            return 0
        if expected == actual:
            return 3
        if len(expected) < 2 or len(actual) < 2:
            boundary = re.compile(rf"^{re.escape(expected_raw)}(?:\s|$)")
            return 2 if expected_raw and boundary.search(actual_raw) else 0
        if expected in actual or actual in expected:
            return 2
        return 0

    @classmethod
    def _should_avoid_version(cls, actual_title: str, requested_text: str = "") -> bool:
        return has_unrequested_title_qualifier(actual_title, requested_text)

    @staticmethod
    def _requires_verified_original(keyword: str) -> bool:
        content = normalize_matching_text(keyword).lower()
        if not content:
            return False
        return any(token in content for token in ("原声", "原版", "原曲", "官方"))

    @classmethod
    def _artist_matches_intent(cls, artist: str, intent: MusicKeywordIntent) -> bool:
        if not intent.artist_hint:
            return True
        artist_norm = normalize_matching_text(artist).lower()
        artist_compact = cls._compact_text(artist_norm)
        hint_compact = cls._compact_text(intent.artist_hint)
        if hint_compact and (hint_compact in artist_compact or artist_compact in hint_compact):
            return True
        if intent.artist_tokens:
            token_hits = 0
            for token in intent.artist_tokens:
                token_norm = normalize_matching_text(token).lower()
                token_compact = cls._compact_text(token_norm)
                if not token_compact:
                    continue
                if token_compact in artist_compact:
                    token_hits += 1
            if token_hits > 0:
                return True
        return False

    @staticmethod
    def _rank_search_results(
        results: list[MusicSearchResult],
        keyword: str,
        *,
        intent: MusicKeywordIntent | None = None,
    ) -> list[MusicSearchResult]:
        """对搜索结果进行智能排序，优先关键词命中且非改编版本。"""
        keyword_lower = normalize_matching_text(keyword).lower()
        raw_tokens = [x for x in re.split(r"[\s,，;；/|]+", keyword_lower) if x]
        if not raw_tokens and keyword_lower:
            raw_tokens = [keyword_lower]
        # 去重但保持顺序，避免重复词影响评分。
        tokens = list(dict.fromkeys(raw_tokens))
        intent_obj = intent or MusicKeywordIntent(title_hint=keyword_lower, artist_hint="", artist_tokens=())

        compact_keyword = re.sub(r"\s+", "", keyword_lower)
        compact_title_hint = re.sub(r"\s+", "", normalize_matching_text(intent_obj.title_hint).lower())

        def score_result(item: MusicSearchResult) -> tuple[int, int, int, int, int, int, int, int, int, int, int, int, int]:
            """关键词命中优先，其次标题/歌手约束，再其次时长。"""
            name_lower = normalize_matching_text(item.name).lower()
            artist_lower = normalize_matching_text(item.artist).lower()
            compact_name = re.sub(r"\s+", "", name_lower)

            exact_name_match = 1 if keyword_lower and keyword_lower in name_lower else 0
            exact_compact_match = 1 if compact_keyword and compact_name == compact_keyword else 0
            starts_with_keyword = 1 if compact_keyword and compact_name.startswith(compact_keyword) else 0
            title_match_level = MusicEngine._title_match_level(intent_obj.title_hint, item.name) if intent_obj.title_hint else 0
            artist_hint_match = 1 if MusicEngine._artist_matches_intent(artist_lower, intent_obj) else 0
            strict_match = 1 if title_match_level >= 2 and artist_hint_match else 0
            version_ok = 0 if (intent_obj.title_hint and MusicEngine._should_avoid_version(item.name, intent_obj.title_hint)) else 1
            keyword_pos_score = 0
            if compact_keyword:
                pos = compact_name.find(compact_keyword)
                if pos >= 0:
                    keyword_pos_score = max(1, 1000 - pos)
            length_gap_score = 0
            if compact_keyword:
                length_gap_score = max(1, 1000 - abs(len(compact_name) - len(compact_keyword)) * 10)

            name_token_hits = 0
            artist_token_hits = 0
            all_token_hits = 0
            for token in tokens:
                if token in name_lower:
                    name_token_hits += 1
                if token in artist_lower:
                    artist_token_hits += 1
                if token in name_lower or token in artist_lower:
                    all_token_hits += 1

            duration_score = int(item.duration_ms or 0)

            return (
                strict_match,
                title_match_level,
                artist_hint_match,
                version_ok,
                exact_compact_match,
                exact_name_match,
                starts_with_keyword,
                keyword_pos_score,
                length_gap_score,
                name_token_hits,
                all_token_hits,
                artist_token_hits,
                duration_score,
            )

        # 按得分排序（降序）
        return sorted(results, key=score_result, reverse=True)

    @staticmethod
    def _merge_unique_results(
        first: list[MusicSearchResult],
        second: list[MusicSearchResult],
        max_size: int,
    ) -> list[MusicSearchResult]:
        seen: set[str] = set()
        merged: list[MusicSearchResult] = []
        for row in [*first, *second]:
            key = f"{row.song_id}|{row.name.strip().lower()}|{row.artist.strip().lower()}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
            if len(merged) >= max_size:
                break
        return merged

    # ── 聚合 API 基址：多候选 + HTTPS 强制 ──────────────────────────
    def _resolve_api_bases(self, music_cfg: dict[str, Any]) -> list[str]:
        """合并 music.api_bases（列表）与 music.api_base（单值，向后兼容），按序去重。

        默认要求 HTTPS。公网明文 HTTP 候选被丢弃并留 WARNING，除非
        music.allow_insecure_api_base 为真；环回/内网地址一律放行（自建服务不出网）。
        """
        raw_candidates: list[Any] = []
        configured_list = music_cfg.get("api_bases")
        if isinstance(configured_list, str):
            raw_candidates.extend(re.split(r"[,\s]+", configured_list))
        elif isinstance(configured_list, (list, tuple)):
            raw_candidates.extend(configured_list)
        raw_candidates.append(music_cfg.get("api_base", self._DEFAULT_API_BASE))

        out: list[str] = []
        for item in raw_candidates:
            base = normalize_text(str(item or "")).rstrip("/")
            if not base or base in out:
                continue
            if self._is_insecure_public_base(base):
                if not self._allow_insecure_api_base:
                    _log.warning(
                        "music_api_base_insecure_skipped | base=%s | "
                        "出网调用必须 HTTPS；要放行请开 music.allow_insecure_api_base",
                        base,
                    )
                    continue
                _log.warning("music_api_base_insecure_allowed | base=%s", base)
            out.append(base)
        return out

    @staticmethod
    def _is_insecure_public_base(base: str) -> bool:
        """明文 HTTP 且指向公网 host 时为真（环回/内网自建服务不算）。"""
        parsed = urlparse(base)
        if normalize_text(parsed.scheme).lower() != "http":
            return False
        host = normalize_text(parsed.hostname or "").lower()
        if not host or host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (addr.is_loopback or addr.is_private or addr.is_link_local)

    @staticmethod
    def _base_host(url: str) -> str:
        return normalize_text(urlparse(normalize_text(url)).netloc).lower()

    def _is_host_unreachable(self, url: str) -> bool:
        """该 host 是否处于熔断静默期。"""
        host = self._base_host(url)
        if not host:
            return False
        until = self._unreachable_until.get(host, 0.0)
        if until <= 0:
            return False
        if until > time.monotonic():
            return True
        self._unreachable_until.pop(host, None)
        return False

    def _mark_host_unreachable(self, url: str, reason: str) -> None:
        host = self._base_host(url)
        if not host or self._unreachable_cooldown_s <= 0:
            return
        already_open = self._is_host_unreachable(url)
        self._unreachable_until[host] = time.monotonic() + self._unreachable_cooldown_s
        if not already_open:
            _log.warning(
                "music_api_host_circuit_open | host=%s | cooldown=%.0fs | reason=%s",
                host,
                self._unreachable_cooldown_s,
                reason[:160],
            )

    def _resolve_upstream_deadline(self) -> float:
        """取聚合源预算截止点：优先用本次操作的，没有才现算一个。

        `_operation_deadline` 由 `_upstream_operation()` 在 search()/play() 入口设置，
        所以同一次工具调用里的多次 variant / 候选请求**共享**一个预算。
        独立调用（比如单独取歌词）拿不到操作上下文，就退回单次预算 ——
        行为与改动前一致，不会变得更松。
        """

        pending = self._operation_deadline
        if pending is not None:
            return pending
        return time.monotonic() + self._upstream_budget_s

    @contextlib.contextmanager
    def _upstream_operation(self):
        """把一次工具调用内的所有聚合源请求圈进同一个预算。

        嵌套调用（play() 内部又调 search()）沿用外层截止点，不重置 ——
        否则内层一重置，外层的「秒级放弃」就又失效了。
        """

        if self._operation_deadline is not None:
            yield
            return
        self._operation_deadline = time.monotonic() + self._upstream_budget_s
        try:
            yield
        finally:
            self._operation_deadline = None

    async def _fetch_alger_json(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        source: str,
        deadline: float,
    ) -> dict[str, Any] | None:
        """请求一个聚合源端点。快速失败：预算耗尽或 host 熔断时直接放弃。

        返回 None 表示这次没拿到可用 JSON，失败原因一定落一条 WARNING。
        """
        if self._is_host_unreachable(endpoint):
            _log.warning(
                "music_api_skip_circuit_open | source=%s | host=%s",
                source,
                self._base_host(endpoint),
            )
            return None

        attempt = 0
        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log.warning(
                    "music_api_budget_exhausted | source=%s | endpoint=%s | attempt=%d",
                    source,
                    endpoint,
                    attempt,
                )
                return None
            request_timeout = min(self._timeout, remaining)
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=request_timeout, headers=self._COMMON_HEADERS) as client:
                    resp = await client.get(endpoint, params=params)
                    status = resp.status_code
                    if status in self._RETRYABLE_STATUS:
                        cost = time.monotonic() - started
                        # 只在还够再跑一次同等开销的请求时才重试，避免把预算烧在死上游。
                        if attempt < 2 and (deadline - time.monotonic()) > cost:
                            continue
                        self._mark_host_unreachable(endpoint, f"http_{status}")
                        _log.warning(
                            "music_api_fail | source=%s | endpoint=%s | status=%d | attempts=%d",
                            source,
                            endpoint,
                            status,
                            attempt,
                        )
                        return None
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.TransportError as exc:
                self._mark_host_unreachable(endpoint, type(exc).__name__)
                _log.warning(
                    "music_api_fail | source=%s | endpoint=%s | transport=%s | %s",
                    source,
                    endpoint,
                    type(exc).__name__,
                    exc,
                )
                return None
            except Exception as exc:
                # 这条原来只记 WARNING 就 return，**不标记 host 不可达** ——
                # 于是「熔断期内零请求」只覆盖了 TransportError 和可重试 5xx，
                # 而最常见的形态（稳定 7s 回 404、HTTP 200 但 body 不是 JSON）
                # 一律漏过去。实测后果：上游稳定回 404 时跑完 4 个 variant / 8 次 HTTP，
                # _unreachable_until 仍是空的，下一回合从头再烧一遍
                # （ToolExecutor 长驻，熔断状态本该跨回合复用）。
                #
                # 慢/坏响应与连不上是同一类事实：这个 host 现在给不出可用 JSON。
                # 所以一并熔断。判定只看「有没有拿到可用响应」这个结构事实，
                # 不去猜错误语义。
                self._mark_host_unreachable(endpoint, f"bad_response:{type(exc).__name__}")
                _log.warning(
                    "music_api_fail | source=%s | endpoint=%s | exc=%s | %s | circuit=opened",
                    source,
                    endpoint,
                    type(exc).__name__,
                    exc,
                )
                return None
            if not isinstance(data, dict):
                # 200 + 非 JSON 对象：同样是「这个 host 给不出可用 JSON」。
                self._mark_host_unreachable(endpoint, "non_dict_payload")
                _log.warning(
                    "music_api_fail | source=%s | endpoint=%s | reason=non_dict_payload"
                    " | got=%s | circuit=opened",
                    source,
                    endpoint,
                    type(data).__name__,
                )
                return None
            return data

    @staticmethod
    def _derive_alger_web_base(api_base: str) -> str:
        base = normalize_text(api_base).rstrip("/")
        if not base:
            return ""
        parsed = urlparse(base)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        lowered = base.lower()
        if lowered.endswith("/api"):
            return base[:-4]
        return base

    @staticmethod
    def _normalize_alger_api_base(candidate: str) -> str:
        raw = normalize_text(candidate).rstrip("/")
        if not raw:
            return ""
        if raw.lower().endswith("/api"):
            return raw
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/api"
        return ""

    async def _discover_alger_api_bases_via_crawl(self, *, deadline: float | None = None) -> list[str]:
        """从各候选基址的前端页面/JS 中提取 API 基址，作为 API 不可用时的爬虫兜底。

        熔断中的 host 直接跳过 —— 上游整台挂掉时再去爬它的首页只是白烧预算。
        """
        out: list[str] = []
        for api_base in self._api_bases:
            if deadline is not None and time.monotonic() >= deadline:
                _log.warning("music_crawl_budget_exhausted | base=%s", api_base)
                break
            if self._is_host_unreachable(api_base):
                continue
            for found in await self._crawl_one_web_base(
                self._derive_alger_web_base(api_base), api_base, deadline=deadline
            ):
                if found not in out:
                    out.append(found)
        return out

    async def _crawl_one_web_base(
        self, web_base: str, api_base: str, *, deadline: float | None = None
    ) -> list[str]:
        """爬一个前端站点找 API 基址。**每个请求都要夹 deadline。**

        原实现只在外层 `_discover_alger_api_bases_via_crawl` 的循环顶检查一次 deadline，
        进来之后用 `self._timeout`（模板 15s）发最多 5 个请求（1 个首页 + 4 个 JS），
        一个都不夹 remaining —— 单次爬取最坏 5×15=75 秒，而 upstream_budget_seconds 写的是 8。
        「秒级放弃聚合源」的契约在这条腿上完全不成立。
        """
        web_base = normalize_text(web_base).rstrip("/")
        if not web_base:
            return []

        def _budget_left() -> float:
            """剩余预算。没有 deadline 时退回单次 timeout，行为与改动前一致。"""

            if deadline is None:
                return float(self._timeout)
            return deadline - time.monotonic()

        def _request_timeout() -> float | None:
            """本次请求该用多长超时。None 表示预算已耗尽，别发了。"""

            left = _budget_left()
            if left <= 0:
                return None
            return max(0.5, min(float(self._timeout), left))

        script_urls: list[str] = []
        discovered: list[str] = []
        seen: set[str] = set()
        web_host = normalize_text(urlparse(web_base).netloc).lower()

        def _add_api_base(raw_base: str) -> None:
            normalized = self._normalize_alger_api_base(raw_base)
            if not normalized:
                return
            host = normalize_text(urlparse(normalized).netloc).lower()
            # 只保留 Alger 同域（或显式含 alger 标识）的 API，避免拉入无关第三方接口。
            if web_host and host and host != web_host and "alger" not in host:
                return
            if normalized in seen:
                return
            seen.add(normalized)
            discovered.append(normalized)

        _add_api_base(api_base)

        index_timeout = _request_timeout()
        if index_timeout is None:
            _log.warning("music_crawl_budget_exhausted | stage=index | web=%s", web_base)
            return discovered
        try:
            async with httpx.AsyncClient(timeout=index_timeout, headers=self._COMMON_HEADERS) as client:
                index_resp = await client.get(web_base)
                index_resp.raise_for_status()
                html = normalize_text(index_resp.text)
                if not html:
                    return discovered

                for raw in re.findall(r"""<(?:script|link)[^>]+(?:src|href)=["']([^"']+assets/[^"']+\.js[^"']*)["']""", html, flags=re.IGNORECASE):
                    full = normalize_text(urljoin(f"{web_base}/", raw))
                    if full and full not in script_urls:
                        script_urls.append(full)
                    if len(script_urls) >= 4:
                        break

                for js_url in script_urls:
                    js_timeout = _request_timeout()
                    if js_timeout is None:
                        _log.warning(
                            "music_crawl_budget_exhausted | stage=js | web=%s | fetched=%d/%d",
                            web_base,
                            script_urls.index(js_url),
                            len(script_urls),
                        )
                        break
                    try:
                        js_resp = await client.get(js_url, timeout=js_timeout)
                        js_resp.raise_for_status()
                        js_text = normalize_text(js_resp.text)
                    except Exception:
                        continue
                    if not js_text:
                        continue
                    for match in re.findall(r"""https?://[^"'\s]+?/api""", js_text):
                        _add_api_base(match)
                    # 前端常见 request.get("/song/url/v1")，至少可反推出同域 /api。
                    if ("/song/url/v1" in js_text) or ("/song/url" in js_text and "/search" in js_text):
                        _add_api_base(f"{web_base}/api")
        except httpx.TransportError as exc:
            self._mark_host_unreachable(web_base, type(exc).__name__)
            _log.warning("alger_crawl_discover_fail | web=%s | %s", web_base, exc)
            return discovered
        except Exception as exc:
            _log.warning("alger_crawl_discover_fail | web=%s | %s", web_base, exc)
            return discovered

        if len(discovered) > 1:
            _log.info(
                "alger_crawl_discover_ok | web=%s | candidates=%s",
                web_base,
                ",".join(discovered[:5]),
            )
        return discovered

    def _candidate_alger_api_bases(self) -> list[str]:
        """全部聚合源候选（配置顺序 + 爬虫发现），已剔除熔断中的 host。"""
        out: list[str] = []
        skipped = 0
        for item in [*self._api_bases, *self._alger_discovered_api_bases]:
            norm = self._normalize_alger_api_base(item)
            if not norm or norm in out:
                continue
            if self._is_host_unreachable(norm):
                skipped += 1
                continue
            out.append(norm)
        if skipped and not out:
            _log.warning("music_api_all_candidates_circuit_open | skipped=%d", skipped)
        return out

    async def _search_alger(self, keyword: str, limit: int) -> list[MusicSearchResult]:
        params = {"keywords": keyword, "limit": limit}
        tried: set[str] = set()
        deadline = self._resolve_upstream_deadline()

        async def _try_search(base: str, source: str) -> list[MusicSearchResult]:
            endpoint = f"{base}{self._ALGER_SEARCH_URL}"
            if endpoint in tried:
                return []
            tried.add(endpoint)
            data = await self._fetch_alger_json(endpoint, params, source=source, deadline=deadline)
            if data is None:
                return []
            rows = self._parse_search_songs(data)
            if not rows:
                _log.warning("alger_search_empty | source=%s | endpoint=%s", source, endpoint)
            return rows

        candidates = self._candidate_alger_api_bases()
        for idx, api_base in enumerate(candidates):
            rows = await _try_search(api_base, source="api")
            if rows:
                return rows
            if idx + 1 < len(candidates):
                _log.warning(
                    "music_api_failover | from=%s | to=%s | stage=search",
                    self._base_host(api_base),
                    self._base_host(candidates[idx + 1]),
                )

        discovered = await self._discover_alger_api_bases_via_crawl(deadline=deadline)
        for api_base in discovered:
            if api_base not in self._alger_discovered_api_bases:
                self._alger_discovered_api_bases.append(api_base)
            rows = await _try_search(api_base, source="crawler")
            if rows:
                return rows
        return []

    async def _search_netease(self, keyword: str, limit: int) -> list[MusicSearchResult]:
        params = {"s": keyword, "type": 1, "limit": limit, "offset": 0}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.post(self._NETEASE_SEARCH_URL, data=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("netease_search_fail | %s", exc)
            return []
        return self._parse_search_songs(data)

    async def _search_soundcloud(self, keyword: str, limit: int) -> list[MusicSearchResult]:
        query = normalize_matching_text(keyword)
        if not query:
            return []
        try:
            rows = await self._soundcloud_client.search_tracks(query, limit=max(5, min(limit, 20)))
        except Exception as exc:
            _log.warning("soundcloud_search_fail | keyword=%s | %s", query, exc)
            return []

        results: list[MusicSearchResult] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            title = normalize_text(str(item.get("title", "")))
            page_url = normalize_text(str(item.get("permalink_url", "")))
            if not title or not page_url:
                continue
            artist = self._extract_soundcloud_artist(item)
            try:
                song_id = int(item.get("id", 0) or 0)
            except Exception:
                song_id = 0
            try:
                duration_ms = int(item.get("duration", 0) or 0)
            except Exception:
                duration_ms = 0
            results.append(
                MusicSearchResult(
                    song_id=song_id,
                    name=title,
                    artist=artist,
                    duration_ms=duration_ms,
                    source="soundcloud",
                    source_url=page_url,
                )
            )
        return results

    @staticmethod
    def _extract_soundcloud_artist(item: dict[str, Any]) -> str:
        publisher = item.get("publisher_metadata", {})
        if isinstance(publisher, dict):
            artist = normalize_text(str(publisher.get("artist", "")))
            if artist:
                return artist
        user = item.get("user", {})
        if isinstance(user, dict):
            artist = normalize_text(str(user.get("username", "")))
            if artist:
                return artist
        return ""

    @staticmethod
    def _parse_search_songs(data: dict[str, Any]) -> list[MusicSearchResult]:
        songs = data.get("result", {}).get("songs", [])
        if not isinstance(songs, list):
            return []

        results: list[MusicSearchResult] = []
        for item in songs:
            if not isinstance(item, dict):
                continue

            ar_list = item.get("artists") or item.get("ar") or []
            if isinstance(ar_list, list):
                artists = "/".join(
                    str(x.get("name", "")).strip()
                    for x in ar_list
                    if isinstance(x, dict) and str(x.get("name", "")).strip()
                )
            else:
                artists = ""

            al = item.get("album") or item.get("al") or {}
            album = str(al.get("name", "")).strip() if isinstance(al, dict) else ""

            dur = item.get("duration") or item.get("dt") or 0
            try:
                duration_ms = int(dur)
            except Exception:
                duration_ms = 0

            try:
                song_id = int(item.get("id", 0) or 0)
            except Exception:
                song_id = 0

            name = str(item.get("name", "")).strip()
            if not name:
                continue

            results.append(
                MusicSearchResult(
                    song_id=song_id,
                    name=name,
                    artist=artists,
                    album=album,
                    duration_ms=duration_ms,
                    source="netease",
                    source_url="",
                )
            )

        return results

    @classmethod
    def _pick_better_url(cls, current: MusicPlayUrl, incoming: MusicPlayUrl) -> MusicPlayUrl:
        if not incoming.url:
            return current
        if not current.url:
            return incoming
        level_rank = {
            "jymaster": 5,
            "sky": 4,
            "lossless": 3,
            "hires": 3,
            "exhigh": 2,
            "higher": 1,
            "standard": 0,
        }

        def _score(info: MusicPlayUrl) -> tuple[int, int, int]:
            quality = level_rank.get(normalize_text(info.level).lower(), 0)
            return (0 if info.is_trial else 1, int(info.duration_ms or 0), quality)

        return incoming if _score(incoming) > _score(current) else current

    def _extract_play_url_meta(self, rows: Any, *, source: str) -> MusicPlayUrl:
        best = MusicPlayUrl()
        if not isinstance(rows, list):
            return best

        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_url = row.get("url")
            if not isinstance(raw_url, str):
                continue
            url = normalize_text(raw_url)
            if not url or not re.match(r"^https?://", url, flags=re.IGNORECASE):
                continue
            try:
                duration_ms = int(row.get("time", 0) or 0)
            except Exception:
                duration_ms = 0
            free_trial_info = row.get("freeTrialInfo")
            is_trial = bool(free_trial_info)
            if not is_trial:
                free_trial_priv = row.get("freeTrialPrivilege")
                if isinstance(free_trial_priv, dict):
                    try:
                        cannot_reason = int(free_trial_priv.get("cannotListenReason") or 0)
                    except Exception:
                        cannot_reason = 0
                    if cannot_reason == 1:
                        # 版权受限时经常下发试听片段；再结合时长阈值减少误判。
                        if self._break_limit_enable:
                            # 破限模式：只把“明显短片段”判为试听，避免把完整音源误判。
                            is_trial = duration_ms <= 0 or duration_ms < self._BREAK_LIMIT_MIN_FULL_MS
                        else:
                            threshold = self._trial_max_duration_ms
                            is_trial = duration_ms <= 0 or (threshold > 0 and duration_ms <= threshold)
            level = normalize_text(str(row.get("level", "")))
            candidate = MusicPlayUrl(
                url=url,
                duration_ms=duration_ms,
                is_trial=is_trial,
                source=source,
                level=level,
            )
            best = self._pick_better_url(best, candidate)
        return best

    async def _get_play_url(self, song_id: int) -> MusicPlayUrl:
        alger = await self._get_alger_url(song_id)
        if alger.url and not alger.is_trial:
            return alger

        # 尝试 UnblockNeteaseMusic 服务
        if self._unblock_enable and self._unblock_api_base:
            unblock = await self._get_unblock_url(song_id)
            if unblock.url and not unblock.is_trial:
                _log.info(
                    "unblock_success | id=%d | src=%s | level=%s",
                    song_id,
                    unblock.source or "-",
                    unblock.level or "-",
                )
                return unblock
            # 如果 unblock 也是试听，选择更好的
            if unblock.url:
                alger = self._pick_better_url(alger, unblock)

        if not alger.url:
            _log.info("alger_url_empty, fallback to netease | id=%d", song_id)
        elif alger.is_trial:
            _log.info(
                "alger_url_trial_only | id=%d | src=%s | level=%s | time=%dms",
                song_id,
                alger.source or "-",
                alger.level or "-",
                alger.duration_ms,
            )

        netease = await self._get_netease_url(song_id)
        chosen = self._pick_better_url(alger, netease)
        return chosen

    async def _get_play_url_with_alternative(
        self,
        song: MusicSearchResult,
        *,
        require_verified_original: bool = False,
    ) -> MusicPlayUrl:
        """获取播放链接，支持本地音源替换。"""
        # 先尝试网易云音源
        netease_url = await self._get_play_url(song.song_id)
        if netease_url.url and not netease_url.is_trial:
            return netease_url

        if require_verified_original:
            return netease_url

        # 如果网易云失败，尝试本地音源匹配
        if self._source_matcher and song.name and song.artist:
            _log.info(
                "trying_alternative_source | id=%d | song=%s | artist=%s",
                song.song_id, song.name, song.artist,
            )
            sources = self._unblock_sources.split(",") if self._unblock_sources else None
            alternative = await self._source_matcher.find_alternative(
                song.name,
                song.artist,
                song.duration_ms,
                sources,
            )
            if alternative and alternative.url:
                return MusicPlayUrl(
                    url=alternative.url,
                    duration_ms=alternative.duration_ms,
                    is_trial=False,
                    source=alternative.source,
                    level=alternative.quality,
                )

        return netease_url

    async def _get_soundcloud_play_url(self, song: MusicSearchResult) -> MusicPlayUrl:
        page_url = normalize_text(song.source_url)
        if not page_url:
            return MusicPlayUrl()
        try:
            audio = await self._soundcloud_client.resolve_page_audio(page_url)
        except Exception as exc:
            _log.warning("soundcloud_resolve_fail | url=%s | %s", page_url[:160], exc)
            return MusicPlayUrl()
        if not audio or not audio.audio_url:
            return MusicPlayUrl()
        return MusicPlayUrl(
            url=audio.audio_url,
            duration_ms=int(song.duration_ms or 0),
            is_trial=False,
            source="soundcloud",
            level=audio.protocol or audio.mime_type or "soundcloud",
        )

    async def _get_alger_url(self, song_id: int) -> MusicPlayUrl:
        best = MusicPlayUrl()
        tried: set[str] = set()
        deadline = self._resolve_upstream_deadline()

        async def _try_base(api_base: str, source_tag: str) -> MusicPlayUrl:
            local_best = MusicPlayUrl()
            endpoint_rows = [
                (f"{api_base}{self._ALGER_PLAYER_URL_V1}", {"id": song_id, "level": "exhigh"}, "alger_v1"),
                (f"{api_base}{self._ALGER_PLAYER_URL}", {"id": song_id, "br": 320000}, "alger"),
            ]
            for endpoint, params, source in endpoint_rows:
                if endpoint in tried:
                    continue
                tried.add(endpoint)
                data = await self._fetch_alger_json(
                    endpoint, params, source=f"{source_tag}:{source}", deadline=deadline
                )
                if data is None:
                    continue
                rows = data.get("data", [])
                candidate = self._extract_play_url_meta(rows, source=f"{source_tag}:{source}")
                local_best = self._pick_better_url(local_best, candidate)
            return local_best

        candidates = self._candidate_alger_api_bases()
        for idx, api_base in enumerate(candidates):
            candidate = await _try_base(api_base, source_tag="api")
            best = self._pick_better_url(best, candidate)
            if best.url and not best.is_trial:
                return best
            if idx + 1 < len(candidates):
                _log.warning(
                    "music_api_failover | from=%s | to=%s | stage=play_url",
                    self._base_host(api_base),
                    self._base_host(candidates[idx + 1]),
                )

        discovered = await self._discover_alger_api_bases_via_crawl(deadline=deadline)
        for api_base in discovered:
            if api_base not in self._alger_discovered_api_bases:
                self._alger_discovered_api_bases.append(api_base)
            candidate = await _try_base(api_base, source_tag="crawler")
            best = self._pick_better_url(best, candidate)
            if best.url and not best.is_trial:
                return best
        return best

    async def _get_netease_url(self, song_id: int) -> MusicPlayUrl:
        params = {"id": song_id, "ids": f"[{song_id}]", "br": 320000}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(self._NETEASE_PLAYER_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("netease_url_fail | id=%d | %s", song_id, exc)
            return MusicPlayUrl()

        rows = data.get("data", [])
        return self._extract_play_url_meta(rows, source="netease")

    async def _get_unblock_url(self, song_id: int) -> MusicPlayUrl:
        """通过 UnblockNeteaseMusic 服务获取音源。"""
        if not self._unblock_api_base:
            return MusicPlayUrl()

        # UnblockNeteaseMusic 的 API 格式：GET /song/url?id=xxx&source=qq,kuwo
        url = f"{self._unblock_api_base}/song/url"
        params = {"id": song_id}
        if self._unblock_sources:
            params["source"] = self._unblock_sources

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._COMMON_HEADERS) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            _log.warning("unblock_url_fail | id=%d | %s", song_id, exc)
            return MusicPlayUrl()

        # UnblockNeteaseMusic 返回格式类似 Netease API
        rows = data.get("data", [])
        return self._extract_play_url_meta(rows, source="unblock")

    @staticmethod
    def _order_play_candidates(results: list[MusicSearchResult], *, intent: MusicKeywordIntent) -> list[MusicSearchResult]:
        if not results:
            return []
        title_hint = normalize_matching_text(intent.title_hint)

        strict_title: list[MusicSearchResult] = []
        title_only: list[MusicSearchResult] = []
        artist_only: list[MusicSearchResult] = []
        rest: list[MusicSearchResult] = []

        for row in results:
            title_hit = bool(
                title_hint
                and MusicEngine._title_match_level(title_hint, row.name) >= 2
                and not MusicEngine._should_avoid_version(row.name, title_hint)
            )
            artist_hit = bool(intent.artist_hint and MusicEngine._artist_matches_intent(row.artist, intent))
            if title_hit and artist_hit:
                strict_title.append(row)
            elif title_hit:
                title_only.append(row)
            elif artist_hit:
                artist_only.append(row)
            else:
                rest.append(row)
        merged = [*strict_title, *title_only, *artist_only, *rest]

        seen_ids: set[int] = set()
        out: list[MusicSearchResult] = []
        for row in merged:
            if row.song_id in seen_ids:
                continue
            seen_ids.add(row.song_id)
            out.append(row)
        return out

    @staticmethod
    def _format_song_choices(results: list[MusicSearchResult], requested_text: str = "") -> str:
        visible: list[str] = []
        for idx, row in enumerate(results[:5], start=1):
            if requested_text and MusicEngine._should_avoid_version(row.name, requested_text):
                continue
            visible.append(f"{idx}. {row.name} - {row.artist} (ID: {row.song_id})")
        return "\n".join(visible)

    async def get_lyrics(self, song_id: int) -> str:
        candidates = self._candidate_alger_api_bases()
        if not candidates:
            _log.warning("lyric_skip_no_api_base | id=%d | 未配置可用的 music.api_bases", song_id)
            return ""
        params = {"id": song_id}
        deadline = self._resolve_upstream_deadline()
        for api_base in candidates:
            data = await self._fetch_alger_json(
                f"{api_base}/lyric", params, source="lyric", deadline=deadline
            )
            if data is None:
                continue
            lrc = data.get("lrc")
            lyric = str(lrc.get("lyric", "") or "") if isinstance(lrc, dict) else ""
            if lyric:
                return lyric
        return ""

    async def play(
        self,
        keyword: str,
        as_voice: bool = True,
        *,
        title: str = "",
        artist: str = "",
    ) -> MusicPlayResult:
        raw_keyword = str(keyword or "").strip()
        keyword = normalize_matching_text(keyword)
        title = normalize_matching_text(title)
        artist = normalize_matching_text(artist)
        query = build_music_keyword(keyword, title, artist)
        intent = self._build_keyword_intent(keyword=keyword, title=title, artist=artist)
        # 传原始 keyword 给 search，确保 URL 短路检测能在 normalize 前生效
        search_keyword = raw_keyword if self._detect_url_platform(raw_keyword)[0] else query
        results = await self.search(search_keyword, limit=12, title=title, artist=artist)
        if not results:
            return MusicPlayResult(ok=False, message="没找到相关歌曲", error="no_results")

        ordered = self._order_play_candidates(results, intent=intent)
        requested_title = title or intent.title_hint
        if requested_title:
            exact_title_results = [
                row
                for row in ordered
                if self._title_match_level(requested_title, row.name) >= 2
                and not self._should_avoid_version(row.name, requested_title)
            ]
            if not exact_title_results:
                suggestions = self._format_song_choices(results, requested_text=requested_title)
                text = f"没找到和《{requested_title}》明确匹配的可播歌曲。"
                if suggestions:
                    text += f"\n可参考这些近似结果，但我不能直接替你播：\n{suggestions}"
                return MusicPlayResult(
                    ok=False,
                    song=results[0],
                    message=text,
                    error="no_exact_match",
                )
            ordered = exact_title_results

        has_artist_hint = bool(intent.artist_hint)
        artist_matched: list[MusicSearchResult] = []
        artist_mismatched: list[MusicSearchResult] = []
        if has_artist_hint:
            for row in ordered:
                if self._artist_matches_intent(row.artist, intent):
                    artist_matched.append(row)
                else:
                    artist_mismatched.append(row)
            if artist_matched:
                ordered = [*artist_matched, *artist_mismatched]
            elif self._artist_guard_enable and not self._artist_guard_allow_mismatch_fallback:
                top = results[0]
                return MusicPlayResult(
                    ok=False,
                    song=top,
                    message=f"没找到与歌手「{intent.artist_hint}」匹配的可播版本，请换个关键词或指定歌曲ID。",
                    error="artist_mismatch",
                )
        first = results[0]
        first_preview: MusicPlayResult | None = None
        last_error: MusicPlayResult | None = None
        strict_last_error: MusicPlayResult | None = None
        strict_attempted = 0
        require_verified_original = self._requires_verified_original(query)

        # 依次尝试多个候选，尽量拿到可下载完整音频。
        for idx, song in enumerate(ordered[:8], start=1):
            strict_mode_for_song = bool(has_artist_hint and self._artist_matches_intent(song.artist, intent))
            if has_artist_hint and strict_mode_for_song:
                strict_attempted += 1
            one = await self._play_song(
                song,
                as_voice=as_voice,
                require_verified_original=require_verified_original,
            )
            if one.ok:
                if idx > 1:
                    _log.info(
                        "music_play_fallback_hit | keyword=%s | picked=%d/%d | id=%d | song=%s - %s",
                        normalize_matching_text(query)[:80],
                        idx,
                        len(ordered[:8]),
                        song.song_id,
                        song.name,
                        song.artist,
                    )
                return one
            if one.error == "preview_only" and first_preview is None:
                first_preview = one
            if has_artist_hint and strict_mode_for_song:
                strict_last_error = one
            last_error = one
            if (
                has_artist_hint
                and self._artist_guard_enable
                and not self._artist_guard_allow_mismatch_fallback
                and strict_attempted > 0
                and idx >= len(artist_matched)
            ):
                break

        if (
            has_artist_hint
            and self._artist_guard_enable
            and not self._artist_guard_allow_mismatch_fallback
            and strict_attempted > 0
        ):
            if strict_last_error is not None and strict_last_error.error == "preview_only":
                strict_song = strict_last_error.song or first
                return MusicPlayResult(
                    ok=False,
                    song=strict_song,
                    message=strict_last_error.message or "命中了仅试听音源，没有可用完整音源。",
                    error="preview_only",
                )
            strict_song = (strict_last_error.song if strict_last_error else first) or first
            return MusicPlayResult(
                ok=False,
                song=strict_song,
                message=f"找到歌手「{intent.artist_hint}」的候选但都不可播，请换源或改为指定歌曲ID。",
                error=(strict_last_error.error if strict_last_error else "artist_play_failed") or "artist_play_failed",
            )

        if first_preview is not None:
            song = first_preview.song or first
            return MusicPlayResult(
                ok=False,
                song=song,
                message=first_preview.message or "命中了仅试听音源（约 20~30 秒），没有可用完整音源。",
                error="preview_only",
            )

        return MusicPlayResult(
            ok=False,
            song=first,
            message="歌曲暂时无法播放 可能是区域或版权限制",
            error=(last_error.error if last_error else "") or "play_failed",
        )

    async def _play_via_ytdlp(self, song: MusicSearchResult, *, as_voice: bool = True) -> MusicPlayResult:
        """使用 yt-dlp 从外链直接下载音频（YouTube / Spotify / Bandcamp 等）。"""
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            return MusicPlayResult(ok=False, song=song, error="ytdlp_missing",
                                   message="当前环境缺少 yt-dlp 依赖，无法解析外链音乐。")

        import hashlib
        url = song.source_url
        digest = hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()[:12]
        mp3_path = self._cache_dir / f"ytdlp_{digest}.mp3"

        if not mp3_path.exists() or mp3_path.stat().st_size < 64 * 1024:
            mp3_path.unlink(missing_ok=True)
            opts = {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl": str(mp3_path.with_suffix(".%(ext)s")),
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "socket_timeout": 20,
                "retries": 1,
            }
            if self._ffmpeg:
                opts["ffmpeg_location"] = self._ffmpeg
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]

            def _download() -> dict[str, Any] | None:
                with YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=True)

            try:
                meta = await asyncio.to_thread(_download)
            except Exception as exc:
                _log.warning("ytdlp_music_download_fail | url=%s | %s", url[:160], exc)
                return MusicPlayResult(ok=False, song=song, error="ytdlp_download_failed",
                                       message=f"从 {song.source} 下载音频失败: {str(exc)[:120]}")

            # yt-dlp 可能输出不同扩展名，找到实际文件
            if not mp3_path.exists():
                for ext in ("mp3", "m4a", "opus", "webm", "ogg", "wav"):
                    candidate = mp3_path.with_suffix(f".{ext}")
                    if candidate.exists() and candidate.stat().st_size > 1024:
                        mp3_path = candidate
                        break

            # 更新 song 元数据
            if meta and isinstance(meta, dict):
                song.name = normalize_text(str(meta.get("title", ""))) or song.name
                song.artist = normalize_text(str(meta.get("uploader", "") or meta.get("artist", ""))) or song.artist
                song.duration_ms = int(float(meta.get("duration", 0) or 0) * 1000)

        if not mp3_path.exists() or mp3_path.stat().st_size < 1024:
            return MusicPlayResult(ok=False, song=song, error="ytdlp_no_audio",
                                   message="yt-dlp 下载完成但未获取到有效音频文件。")

        _log.info("ytdlp_music_ok | source=%s | song=%s - %s | path=%s",
                   song.source, song.name, song.artist, mp3_path.name)

        result = MusicPlayResult(
            ok=True, song=song,
            audio_path=str(mp3_path),
            message=f"{song.name} - {song.artist}",
        )

        if as_voice and self._ffmpeg and self._pilk_available:
            silk_path = await self._convert_to_silk(mp3_path)
            if silk_path:
                try:
                    silk_bytes = silk_path.read_bytes()
                except Exception:
                    silk_bytes = b""
                if len(silk_bytes) >= 256:
                    result.silk_path = str(silk_path)
                    result.silk_b64 = base64.b64encode(silk_bytes).decode("ascii")

        self._evict_cache()
        return result

    async def _play_song(
        self,
        song: MusicSearchResult,
        as_voice: bool,
        *,
        require_verified_original: bool = False,
    ) -> MusicPlayResult:
        source_lower = normalize_text(song.source).lower()

        # ── 外链平台：用 yt-dlp 直接下载音频 ──
        if source_lower in {"youtube", "spotify", "bandcamp"} and song.source_url:
            return await self._play_via_ytdlp(song, as_voice=as_voice)

        if source_lower == "soundcloud":
            play_url = await self._get_soundcloud_play_url(song)
        else:
            play_url = await self._get_play_url_with_alternative(
                song,
                require_verified_original=require_verified_original,
            )
        if not play_url.url:
            return MusicPlayResult(ok=False, song=song, error="no_url")
        if play_url.is_trial:
            preview_s = max(20, int(round(play_url.duration_ms / 1000.0))) if play_url.duration_ms > 0 else 30
            _log.info(
                "music_play_preview_only | id=%d | song=%s - %s | source=%s | level=%s | time=%dms",
                song.song_id,
                song.name,
                song.artist,
                play_url.source or "-",
                play_url.level or "-",
                play_url.duration_ms,
            )
            return MusicPlayResult(
                ok=False,
                song=song,
                message=f"「{song.name} - {song.artist}」当前只能拿到试听片段（约 {preview_s} 秒），没有可用完整音源。",
                error="preview_only",
            )

        mp3_path = self._cache_dir / f"netease_{song.song_id}.mp3"
        if mp3_path.exists():
            try:
                if mp3_path.stat().st_size < 64 * 1024:
                    mp3_path.unlink(missing_ok=True)
            except Exception:
                pass

        if not mp3_path.exists():
            if not await self._download_audio_to_cache(play_url.url, mp3_path):
                _log.warning("music_download_fail | id=%d | url=%s", song.song_id, play_url.url[:160])
                return MusicPlayResult(ok=False, song=song, error="download_failed")

        result = MusicPlayResult(
            ok=True,
            song=song,
            audio_path=str(mp3_path),
            message=f"{song.name} - {song.artist}",
        )

        if as_voice and self._ffmpeg and self._pilk_available:
            silk_path = await self._convert_to_silk(mp3_path)
            if silk_path:
                try:
                    silk_bytes = silk_path.read_bytes()
                except Exception:
                    silk_bytes = b""
                if len(silk_bytes) >= 256:
                    result.silk_path = str(silk_path)
                    result.silk_b64 = base64.b64encode(silk_bytes).decode("ascii")
                else:
                    _log.warning("silk_file_too_small | path=%s | bytes=%d", silk_path.name, len(silk_bytes))

        self._evict_cache()
        return result

    async def _download_audio_to_cache(self, source_url: str, target_path: Path) -> bool:
        url = normalize_text(source_url)
        if not url:
            return False
        try:
            target_path.unlink(missing_ok=True)
        except Exception:
            pass

        lower_url = url.lower()
        if self._should_transcode_remote_audio(lower_url, "") and self._ffmpeg:
            return await self._transcode_remote_audio_to_mp3(url, target_path)

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                content_type = normalize_text(resp.headers.get("content-type", "")).lower()
                if self._should_transcode_remote_audio(lower_url, content_type):
                    if self._ffmpeg:
                        return await self._transcode_remote_audio_to_mp3(url, target_path)
                    return False
                target_path.write_bytes(resp.content)
        except Exception as exc:
            _log.warning("music_download_http_fail | url=%s | %s", url[:160], exc)
            if self._ffmpeg:
                return await self._transcode_remote_audio_to_mp3(url, target_path)
            return False

        try:
            if not target_path.exists() or target_path.stat().st_size <= 1024:
                return False
        except Exception:
            return False

        # 内容必须真的是音频。
        #
        # 原来这里只检查 `size > 1024`，而上游返回的 HTML 错误页轻松超过 1024 字节，
        # 于是错误页被存成 .mp3 发出去。业主报「他发的语音听歌也听不了」，
        # 实测日志里对应的是 ffmpeg 的
        #   extract_audio_failed | *.mp3 | Format mp3 detected only with low score of 1
        # —— ffmpeg 在说这个文件根本不是 mp3。
        #
        # 判定复用 utils/media.py::sniff_audio_container（napcat 的下载工具路径
        # 早就在做同类校验，music.py 这条路没接 —— 又一处「防护存在但另一条路没接」）。
        container = sniff_audio_container(target_path)
        if not container:
            head = b""
            try:
                head = target_path.read_bytes()[:64]
            except Exception:
                pass
            _log.warning(
                "music_download_not_audio | url=%s | bytes=%d | head=%r | "
                "上游大概返回了错误页而不是音频",
                url[:120],
                target_path.stat().st_size,
                head[:32],
            )
            # 还能转码就交给 ffmpeg 再试一次（有些源给的是容器不带魔数的流）
            if self._ffmpeg and await self._transcode_remote_audio_to_mp3(url, target_path):
                return bool(sniff_audio_container(target_path))
            target_path.unlink(missing_ok=True)
            return False
        return True

    @staticmethod
    def _should_transcode_remote_audio(url: str, content_type: str) -> bool:
        lowered_content_type = normalize_text(content_type).lower()
        if ".m3u8" in url or "mpegurl" in lowered_content_type:
            return True
        if any(url.endswith(ext) for ext in (".m4a", ".aac", ".ogg", ".opus", ".webm")):
            return True
        return lowered_content_type.startswith("audio/") and "mpeg" not in lowered_content_type

    async def _transcode_remote_audio_to_mp3(self, source_url: str, target_path: Path) -> bool:
        if not self._ffmpeg:
            return False
        cmd = [
            self._ffmpeg,
            "-y",
            "-i",
            str(source_url),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "192k",
            str(target_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=max(60, min(self._silk_encode_timeout_s, 240)))
            if proc.returncode != 0:
                target_path.unlink(missing_ok=True)
                return False
            return target_path.exists() and target_path.stat().st_size > 1024
        except Exception as exc:
            _log.warning("music_transcode_remote_fail | url=%s | %s", source_url[:160], exc)
            target_path.unlink(missing_ok=True)
            return False

    @staticmethod
    def _encode_silk_with_fallback(
        encoder_mod: Any,
        pcm_path: Path,
        silk_path: Path,
        *,
        prefer_keywords: bool,
        bit_rate: int,
        prefer_file_io: bool = False,
    ) -> tuple[Any, str]:
        """Try multiple signatures so pilk/pysilk version differences won't break encoding."""

        def _path_kw_with_bitrate() -> Any:
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                sample_rate=24000,
                bit_rate=int(bit_rate),
                tencent=True,
            )

        def _path_kw_no_bitrate() -> Any:
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                sample_rate=24000,
                tencent=True,
            )

        def _path_pos_with_bitrate() -> Any:
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                24000,
                int(bit_rate),
            )

        def _path_pos_legacy() -> Any:
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                24000,
                True,
            )

        def _path_pos_tencent() -> Any:
            # pilk 0.2.4: encode(pcm, silk, pcm_rate, silk_rate, tencent)。
            # silk_rate 也传 24000 且 tencent=True 才产出 QQ 可用的 tencent 头 silk。
            return encoder_mod.encode(
                str(pcm_path),
                str(silk_path),
                24000,
                24000,
                True,
            )

        def _file_kw_with_bitrate() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    sample_rate=24000,
                    bit_rate=int(bit_rate),
                    tencent=True,
                )

        def _file_kw_no_bitrate() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    sample_rate=24000,
                    tencent=True,
                )

        def _file_pos_with_bitrate() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    24000,
                    int(bit_rate),
                )

        def _file_pos_legacy() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    24000,
                    True,
                )

        def _file_pos_tencent() -> Any:
            with pcm_path.open("rb") as src, silk_path.open("wb") as dst:
                return encoder_mod.encode(
                    src,
                    dst,
                    24000,
                    24000,
                    True,
                )

        attempts: list[tuple[str, Any]] = []
        if prefer_keywords:
            ordered = [
                ("kw_with_bitrate", _path_kw_with_bitrate, _file_kw_with_bitrate),
                ("kw_no_bitrate", _path_kw_no_bitrate, _file_kw_no_bitrate),
                ("pos_tencent", _path_pos_tencent, _file_pos_tencent),
                ("pos_with_bitrate", _path_pos_with_bitrate, _file_pos_with_bitrate),
                ("pos_legacy", _path_pos_legacy, _file_pos_legacy),
            ]
        else:
            ordered = [
                ("pos_tencent", _path_pos_tencent, _file_pos_tencent),
                ("pos_with_bitrate", _path_pos_with_bitrate, _file_pos_with_bitrate),
                ("pos_legacy", _path_pos_legacy, _file_pos_legacy),
                ("kw_with_bitrate", _path_kw_with_bitrate, _file_kw_with_bitrate),
                ("kw_no_bitrate", _path_kw_no_bitrate, _file_kw_no_bitrate),
            ]
        if prefer_file_io:
            for name, path_fn, file_fn in ordered:
                attempts.append((f"file_{name}", file_fn))
                attempts.append((f"path_{name}", path_fn))
        else:
            for name, path_fn, file_fn in ordered:
                attempts.append((f"path_{name}", path_fn))
                attempts.append((f"file_{name}", file_fn))

        last_exc: Exception | None = None
        for name, fn in attempts:
            try:
                return fn(), name
            except TypeError as exc:
                last_exc = exc
                continue

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no_silk_encode_attempt")

    async def _convert_to_silk(self, audio_path: Path) -> Path | None:
        """Convert MP3 to QQ-compatible SILK."""
        silk_path = audio_path.with_suffix(".silk")
        pcm_path = audio_path.with_suffix(".pcm")

        try:
            if silk_path.exists():
                silk_path.unlink(missing_ok=True)

            cmd = [
                self._ffmpeg,
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-f",
                "s16le",
            ]
            if self._max_voice_duration_s > 0:
                cmd.extend(["-t", str(self._max_voice_duration_s)])
            cmd.append(str(pcm_path))

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=self._silk_encode_timeout_s)
            if proc.returncode != 0 or not pcm_path.exists():
                return None

            try:
                import pilk
                _silk_mod = "pilk"
                encoder_mod = pilk
                encode_ret, encode_sig = self._encode_silk_with_fallback(
                    encoder_mod,
                    pcm_path,
                    silk_path,
                    prefer_keywords=True,
                    bit_rate=self._silk_bit_rate,
                )
            except ImportError:
                import pysilk as pilk  # type: ignore[no-redef]
                _silk_mod = "pysilk"
                encoder_mod = pilk
                encode_ret, encode_sig = self._encode_silk_with_fallback(
                    encoder_mod,
                    pcm_path,
                    silk_path,
                    prefer_keywords=False,
                    bit_rate=self._silk_bit_rate,
                    prefer_file_io=True,
                )
            if not silk_path.exists():
                return None

            size = silk_path.stat().st_size
            duration_ms = -1  # unknown
            try:
                duration_ms = int(encoder_mod.get_duration(str(silk_path)))
            except Exception:
                pass

            _log.info(
                "silk_encode_ok | mod=%s | sig=%s | ret=%s | dur=%dms | size=%d | path=%s",
                _silk_mod,
                encode_sig,
                encode_ret,
                duration_ms,
                size,
                silk_path.name,
            )
            if size < 256 or (duration_ms == 0):
                # duration_ms == -1 means get_duration unavailable (pysilk), allow it
                _log.warning(
                    "silk_invalid | dur=%dms | size=%d | path=%s",
                    duration_ms,
                    size,
                    silk_path.name,
                )
                silk_path.unlink(missing_ok=True)
                return None

            return silk_path
        except Exception as exc:
            _log.warning("silk_convert_fail | %s", exc)
            return None
        finally:
            if pcm_path.exists():
                pcm_path.unlink(missing_ok=True)

    def _evict_cache(self) -> None:
        """Keep only latest cache files."""
        try:
            files = sorted(self._cache_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
            for file_path in files[self._max_cache_files :]:
                file_path.unlink(missing_ok=True)
        except Exception:
            pass


def build_music_keyword(keyword: str, title: str = "", artist: str = "") -> str:
    """构建音乐搜索关键词。

    Args:
        keyword: 基础关键词
        title: 歌曲标题
        artist: 歌手名

    Returns:
        组合后的搜索关键词
    """
    keyword = normalize_matching_text(keyword)
    title = normalize_matching_text(title)
    artist = normalize_matching_text(artist)

    def _compact(text: str) -> str:
        return re.sub(r"\s+", "", text)

    def _token_is_covered(token: str, target: str) -> bool:
        compact_token = _compact(token)
        compact_target = _compact(target)
        return bool(compact_token and compact_target and compact_token in compact_target)

    extra_tokens = [
        token
        for token in re.split(r"\s+", keyword)
        if token and not _token_is_covered(token, title) and not _token_is_covered(token, artist)
    ]

    parts: list[str] = []
    keyword_contains_title = bool(title and _compact(title) and _compact(title) in _compact(keyword))
    if title or artist:
        if keyword_contains_title and artist:
            parts.extend([artist, title])
        else:
            parts.extend([title, artist])
    elif keyword:
        parts.append(keyword)

    for token in extra_tokens:
        if token and token not in parts:
            parts.append(token)

    cleaned = [part for part in parts if part]
    return " ".join(cleaned)


# 点歌命令常见前缀。tools_music_exec.py 的 _music_search/_music_play 各内联了
# 一份相同表；架构收敛 C1 后 agent 工具层直接调 MusicEngine，这里提供公开的
# 去前缀函数，避免在 agent 工具层再抄第三份。
MUSIC_COMMAND_PREFIXES: tuple[str, ...] = (
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
)


def strip_music_command_prefix(keyword: str) -> str:
    """去掉点歌命令常见前缀，返回清洗后的关键词。

    与 tools_music_exec.py 内联前缀表保持一致：空输入返回空串，
    命中前缀则剥离后去首尾空白。
    """
    kw = str(keyword or "").strip()
    for prefix in MUSIC_COMMAND_PREFIXES:
        if kw.startswith(prefix):
            kw = kw[len(prefix) :].strip()
    return kw
