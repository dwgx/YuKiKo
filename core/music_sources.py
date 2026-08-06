"""本地音源替换引擎 - 参考 UnblockNeteaseMusic 实现。

支持的平台:
- QQ 音乐 (QQ Music)
- 酷狗音乐 (KuGou)
- 酷我音乐 (KuWo)
- 咪咕音乐 (Migu)
- SoundCloud

参考项目:
- https://github.com/UnblockNeteaseMusic/server
- https://github.com/Binaryify/NeteaseCloudMusicApi
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from core.soundcloud import SoundCloudClient
from utils.text import normalize_matching_text

_log = logging.getLogger("yukiko.music_sources")


@dataclass(slots=True)
class AlternativeSource:
    """替代音源信息。"""
    url: str = ""
    source: str = ""  # qq / kuwo / kugou / migu / soundcloud
    quality: str = ""
    duration_ms: int = 0
    size: int = 0
    br: int = 0  # 比特率


class MusicSourceMatcher:
    """音源匹配器 - 从多个平台搜索并匹配歌曲。

    实现参考 UnblockNeteaseMusic 的匹配逻辑。
    """

    def __init__(self, timeout: float = 8):
        self._timeout = timeout
        # QQ 音乐 vkey 请求要求一个十位数字 guid；固定用同一个而不是每次随机，
        # 免得上游把变动的 guid 当异常流量。
        self._qq_guid = f"{random.randint(10 ** 9, 10 ** 10 - 1)}"
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self._preferred_sources = ("kuwo", "kugou", "migu", "soundcloud", "qq")
        self._secondary_sources = {"qq"}
        self._soundcloud = SoundCloudClient(timeout=max(8.0, timeout))

    # kuwo 搜索窗口。变体噪音很多，窗口太窄正片会被挤出去。
    _KUWO_SEARCH_ROWS = 20

    # 歌手已知时，候选歌手低于这个相似度就判定为「另一个人唱的」。
    # 只在双方都有歌手信息、且没有包含关系时才用到（feat 串靠包含关系放行）。
    _MIN_ARTIST_SCORE = 0.5
    # 带括号修饰的变体相对干净标题的罚分，只用于同分时的取舍，不足以改变过线与否。
    _BRACKETED_VARIANT_PENALTY = 0.02

    _BLOCKED_VERSION_MARKERS = (
        "伴奏",
        "纯音乐",
        "instrumental",
        "inst",
        "karaoke",
        "卡拉ok",
        "原版伴奏",
    )

    def _normalize_sources(self, sources: list[str] | None) -> list[str]:
        requested = [self._normalize_text(x) for x in (sources or []) if self._normalize_text(x)]
        if not requested:
            requested = list(self._preferred_sources)
        deduped = list(dict.fromkeys(requested))
        ordered = [src for src in self._preferred_sources if src in deduped]
        ordered.extend(src for src in deduped if src not in ordered)
        return ordered

    async def _search_one_source(
        self,
        source: str,
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        try:
            _log.info("searching_source | source=%s | song=%s | artist=%s", source, song_name, artist)
            if source == "qq":
                timeout = min(self._timeout, 6.0)
            elif source == "soundcloud":
                timeout = min(14.0, max(self._timeout, 12.0))
            else:
                timeout = min(self._timeout, 8.0)
            result = await asyncio.wait_for(
                self._search_source(source, song_name, artist, duration_ms),
                timeout=timeout,
            )
            if result and result.url:
                _log.info(
                    "alternative_found | source=%s | song=%s | artist=%s | br=%dk",
                    source, song_name, artist, result.br // 1000 if result.br else 0,
                )
                return result
            _log.info("source_no_result | source=%s | song=%s", source, song_name)
            return None
        except asyncio.TimeoutError:
            _log.warning("source_search_timeout | source=%s | song=%s", source, song_name)
            return None
        except Exception as exc:
            _log.warning("source_search_fail | source=%s | song=%s | error=%s", source, song_name, exc)
            return None

    async def _search_source_batch(
        self,
        batch_sources: list[str],
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        if not batch_sources:
            return None

        async def _runner(source: str) -> tuple[str, AlternativeSource | None]:
            return source, await self._search_one_source(source, song_name, artist, duration_ms)

        tasks = [asyncio.create_task(_runner(source)) for source in batch_sources]
        successes: dict[str, AlternativeSource] = {}
        try:
            for task in asyncio.as_completed(tasks):
                source, result = await task
                if result and result.url:
                    successes[source] = result
                    best = self._pick_best_source(successes, batch_sources)
                    if best is not None:
                        return best
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        return self._pick_best_source(successes, batch_sources)

    @staticmethod
    def _pick_best_source(
        successes: dict[str, AlternativeSource],
        source_order: list[str],
    ) -> AlternativeSource | None:
        for source in source_order:
            result = successes.get(source)
            if result and result.url:
                return result
        return None

    @classmethod
    def _should_avoid_candidate_name(cls, candidate_name: str, requested_name: str) -> bool:
        candidate_norm = normalize_matching_text(candidate_name).lower()
        requested_norm = normalize_matching_text(requested_name).lower()
        if not candidate_norm:
            return False
        for marker in cls._BLOCKED_VERSION_MARKERS:
            if marker in candidate_norm and marker not in requested_norm:
                return True
        return False

    async def find_alternative(
        self,
        song_name: str,
        artist: str,
        duration_ms: int = 0,
        sources: list[str] | None = None,
    ) -> AlternativeSource | None:
        """搜索替代音源。

        Args:
            song_name: 歌曲名
            artist: 歌手名
            duration_ms: 歌曲时长（用于匹配验证）
            sources: 音源优先级列表，如 ["qq", "kuwo", "kugou", "migu"]
        """
        sources = self._normalize_sources(sources)

        # 清理歌曲名和歌手名 —— 用「查询形态」保留词边界。
        # 这里曾经用 _normalize_text（比较形态），把 "Life's a Struggle" 压成
        # "lifesastruggle" 再发给上游，英文歌名因此几乎必然搜不到。
        # 下游 _find_best_match 会自己再做比较形态归一化，所以传查询形态是安全的。
        song_name = self._normalize_query_text(song_name)
        artist = self._normalize_query_text(artist)

        if not song_name:
            return None

        primary_batch = [src for src in sources if src not in self._secondary_sources]
        secondary_batch = [src for src in sources if src in self._secondary_sources]

        result = await self._search_source_batch(primary_batch, song_name, artist, duration_ms)
        if result and result.url:
            return result
        return await self._search_source_batch(secondary_batch, song_name, artist, duration_ms)

    async def _search_source(
        self,
        source: str,
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        """从指定平台搜索歌曲。"""
        if source == "kugou":
            return await self._search_kugou(song_name, artist, duration_ms)
        elif source == "migu":
            return await self._search_migu(song_name, artist, duration_ms)
        elif source == "kuwo":
            return await self._search_kuwo(song_name, artist, duration_ms)
        elif source == "qq":
            return await self._search_qq(song_name, artist, duration_ms)
        elif source == "soundcloud":
            return await self._search_soundcloud(song_name, artist, duration_ms)
        return None

    async def _search_soundcloud(
        self,
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        """从 SoundCloud 搜索歌曲并解析真实播放地址。"""
        keyword = f"{song_name} {artist}".strip()

        try:
            songs = await self._soundcloud.search_tracks(keyword, limit=8)
            if not songs:
                return None

            best_match = self._find_best_match(songs, song_name, artist, duration_ms, "soundcloud")
            if not best_match:
                return None

            ordered_candidates: list[dict[str, Any]] = [best_match]
            for row in songs[:5]:
                if row is best_match:
                    continue
                ordered_candidates.append(row)

            audio = None
            resolved_match = best_match
            for row in ordered_candidates:
                audio = await self._soundcloud.get_track_audio(row)
                if audio and audio.audio_url:
                    resolved_match = row
                    break
            if not audio or not audio.audio_url:
                return None

            preset = ""
            transcodings = ((resolved_match.get("media") or {}).get("transcodings") or [])
            if isinstance(transcodings, list):
                for item in transcodings:
                    if not isinstance(item, dict):
                        continue
                    fmt = item.get("format", {})
                    protocol = ""
                    if isinstance(fmt, dict):
                            protocol = self._normalize_text(str(fmt.get("protocol", ""))).lower()
                    if protocol == audio.protocol:
                        preset = self._normalize_text(str(item.get("preset", "")))
                        break

            br = 128000
            if "160" in preset:
                br = 160000
            elif "256" in preset:
                br = 256000
            elif "320" in preset:
                br = 320000

            duration_value = resolved_match.get("duration", 0) or resolved_match.get("full_duration", 0)
            try:
                resolved_duration_ms = int(duration_value)
            except (ValueError, TypeError):
                resolved_duration_ms = 0

            return AlternativeSource(
                url=audio.audio_url,
                source="soundcloud",
                quality=audio.protocol or "stream",
                duration_ms=resolved_duration_ms,
                size=0,
                br=br,
            )
        except Exception as exc:
            _log.warning("soundcloud_search_fail | %s", exc)
            return None

    async def _search_kugou(
        self,
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        """从酷狗音乐搜索 - 参考 UnblockNeteaseMusic 的实现。"""
        keyword = f"{song_name} {artist}".strip()

        # 酷狗搜索 API（HTTPS + HTTP 双重回退）
        search_urls = [
            "https://mobilecdn.kugou.com/api/v3/search/song",
            "http://mobilecdn.kugou.com/api/v3/search/song",
        ]
        params = {
            "format": "json",
            "keyword": keyword,
            "page": 1,
            "pagesize": 5,
            "showtype": 1,
        }

        data = None
        for search_url in search_urls:
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    headers=self._headers,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(search_url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    break
            except Exception:
                continue

        if not data:
            return None

        try:
            songs = data.get("data", {}).get("info", [])
            if not songs:
                return None

            # 匹配最相似的歌曲
            best_match = self._find_best_match(songs, song_name, artist, duration_ms, "kugou")
            if not best_match:
                return None

            # 获取播放链接
            hash_val = best_match.get("hash", "") or best_match.get("320hash", "")
            album_id = best_match.get("album_id", "")

            if not hash_val:
                return None

            play_url = await self._get_kugou_play_url(hash_val, album_id)
            if not play_url:
                return None

            return AlternativeSource(
                url=play_url,
                source="kugou",
                quality="hq",
                duration_ms=best_match.get("duration", 0) * 1000,
                size=best_match.get("filesize", 0),
                br=320000,
            )
        except Exception as exc:
            _log.warning("kugou_search_fail | %s", exc)
            return None

    async def _get_kugou_play_url(self, hash_val: str, album_id: str = "") -> str:
        """获取酷狗音乐播放链接。"""
        url = "http://www.kugou.com/yy/index.php"
        params = {
            "r": "play/getdata",
            "hash": hash_val,
            "album_id": album_id,
            "dfid": "-",
            "mid": hashlib.md5(hash_val.encode()).hexdigest(),
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers, follow_redirects=True) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            play_url = data.get("data", {}).get("play_url", "")
            if play_url and play_url.startswith("http"):
                _log.info("kugou_play_url_ok | hash=%s | url=%s", hash_val, play_url)
                return play_url

            # 尝试备用字段
            play_backup_url = data.get("data", {}).get("play_backup_url", "")
            if play_backup_url and play_backup_url.startswith("http"):
                _log.info("kugou_play_url_ok_backup | hash=%s | url=%s", hash_val, play_backup_url)
                return play_backup_url

            _log.warning("kugou_no_play_url | hash=%s | data=%s", hash_val, data.get("data", {}))
            return ""
        except Exception as exc:
            _log.warning("kugou_play_url_fail | hash=%s | error=%s", hash_val, exc)
            return ""

    async def _search_migu(
        self,
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        """从咪咕音乐搜索 - 使用 app API。

        2026-08-06 实测的上游现状：

        * 参数名是 ``text``，不是 ``keyword``。用 ``keyword`` 会拿到 HTTP 200 +
          ``code=299999 info='参数校验失败 text:不能为空'`` —— 不抛异常，只是 0 条结果。
        * 只带 ``text`` 会返回 ``code=000000 info='成功'`` 但依然 0 条歌曲，
          必须同时带 ``searchSwitch`` 打开 song 维度。
        * 返回体里**没有** ``listenUrl``/``mp3``/``hqUrl`` 这类可播字段，只有
          ``lyricUrl``/``mrcurl``，且 ``rateFormats`` 每档都是 ``price:"200"`` +
          ``showTag:["vip"]``。所以这个源现在只能搜到、放不出 —— 修好参数后
          它会快速走到 ``_extract_migu_play_url`` 返回空并干净失败，
          而不是像以前那样绕一圈 301 再报一条像 bug 的 WARNING。
        """

        keyword = f"{song_name} {artist}".strip()

        # 咪咕 App API（更稳定）
        search_url = "https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do"
        params = {
            "text": keyword,
            "searchSwitch": json.dumps(
                {
                    "song": 1,
                    "album": 0,
                    "singer": 0,
                    "tagSong": 0,
                    "mvSong": 0,
                    "songlist": 0,
                    "bestShow": 1,
                }
            ),
            "type": 2,
            "pgc": 1,
            "rows": 5,
            "ua": "Android_migu",
            "version": "5.0.1",
        }

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; MI 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
                "Referer": "https://m.music.migu.cn/",
                "Channel": "0146921",
            }

            async with httpx.AsyncClient(timeout=self._timeout, headers=headers, follow_redirects=True) as client:
                resp = await client.get(search_url, params=params)
                resp.raise_for_status()
                data = resp.json()

            # 新版 API 返回格式
            song_result = data.get("songResultData", {})
            songs = song_result.get("result", []) if isinstance(song_result, dict) else []

            if not songs:
                _log.info("migu_no_songs | code=%s | info=%s", data.get("code"), data.get("info"))
                return None

            best_match = self._find_best_match(songs, song_name, artist, duration_ms, "migu")
            if not best_match:
                return None

            # 从结果中提取播放链接
            play_url = self._extract_migu_play_url(best_match)
            if not play_url:
                _log.info(
                    "migu_no_playable_url | song=%s | 上游返回体只有 lyricUrl/mrcurl，音质档位均为 vip",
                    song_name,
                )
                return None

            return AlternativeSource(
                url=play_url,
                source="migu",
                quality="hq",
                duration_ms=0,
                size=0,
                br=128000,
            )
        except Exception as exc:
            _log.warning("migu_search_fail | %s", exc)
            return None

    # 已删除 _search_migu_legacy：端点 https://m.music.migu.cn/migu/remoting/scr_search_tag
    # 2026-08-06 实测 301 -> https://m.music.migu.cn/v5，那是个 HTML SPA，跟不跟重定向
    # 都拿不到 JSON。它只在主端点 0 结果时被调用，而当时主端点是因为参数名过期才 0 结果，
    # 于是每次 migu 查询都白跑一次请求并留下一条像 bug 的 migu_legacy_search_fail。

    @staticmethod
    def _extract_migu_play_url(song: dict) -> str:
        """从咪咕搜索结果中提取最佳播放链接。"""
        # 新版 API 的字段
        for key in ("listenUrl", "mp3", "hqUrl", "sqUrl", "bqUrl"):
            url = song.get(key, "")
            if url and isinstance(url, str) and url.startswith("http"):
                return url

        # 尝试从 rateFormats 中提取
        rate_formats = song.get("rateFormats", [])
        if isinstance(rate_formats, list):
            for fmt in rate_formats:
                if not isinstance(fmt, dict):
                    continue
                url = fmt.get("url", "") or fmt.get("androidUrl", "") or fmt.get("iosUrl", "")
                if url and isinstance(url, str):
                    # 咪咕 FTP 地址转 HTTP
                    if url.startswith("ftp://"):
                        url = url.replace("ftp://218.200.160.122:21", "http://freetyst.nf.migu.cn")
                    if url.startswith("http"):
                        return url

        # 尝试 newRateFormats
        new_formats = song.get("newRateFormats", [])
        if isinstance(new_formats, list):
            for fmt in new_formats:
                if not isinstance(fmt, dict):
                    continue
                url = fmt.get("url", "") or fmt.get("androidUrl", "")
                if url and isinstance(url, str):
                    if url.startswith("ftp://"):
                        url = url.replace("ftp://218.200.160.122:21", "http://freetyst.nf.migu.cn")
                    if url.startswith("http"):
                        return url

        return ""

    async def _search_kuwo(
        self,
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        """从酷我音乐搜索 - 使用 search.kuwo.cn 移动端 API。"""
        keyword = f"{song_name} {artist}".strip()

        # 优先使用稳定的移动端搜索 API
        search_url = "http://search.kuwo.cn/r.s"
        params = {
            "all": keyword,
            "ft": "music",
            "itemset": "web_2013",
            "client": "kt",
            "pn": 0,
            # 2026-08-06 实测：rn=5 时窗口会被伴奏/片段/DJ 版/演唱会串烧占满，
            # 正片被挤出去，同一个查询两次跑出的 5 条还不一样 —— 命中纯看运气。
            "rn": self._KUWO_SEARCH_ROWS,
            "rformat": "json",
            "encoding": "utf8",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers, follow_redirects=True) as client:
                resp = await client.get(search_url, params=params)
                resp.raise_for_status()
                # search.kuwo.cn 返回的 JSON 可能不标准（单引号），需要特殊处理
                text = resp.text.strip()
                try:
                    data = resp.json()
                except Exception:
                    # kuwo 有时返回单引号 JSON，尝试替换为双引号后解析
                    try:
                        data = json.loads(text.replace("'", '"'))
                    except Exception:
                        data = {}

            songs = data.get("abslist", [])
            if not songs:
                return None

            # 匹配最相似的歌曲
            best_match = self._find_best_match(songs, song_name, artist, duration_ms, "kuwo")
            if not best_match:
                return None

            # 获取 rid
            rid = (
                best_match.get("DC_TARGETID", "")
                or best_match.get("MUSICRID", "")
                or best_match.get("rid", "")
                or best_match.get("musicrid", "")
            )
            if not rid:
                return None

            # 移除 "MUSIC_" 前缀
            rid = str(rid)
            if rid.startswith("MUSIC_"):
                rid = rid[6:]

            play_url = await self._get_kuwo_play_url(rid)
            if not play_url:
                return None

            dur_raw = best_match.get("DURATION", 0) or best_match.get("duration", 0)
            try:
                dur_ms = int(dur_raw) * 1000
            except (ValueError, TypeError):
                dur_ms = 0

            return AlternativeSource(
                url=play_url,
                source="kuwo",
                quality="hq",
                duration_ms=dur_ms,
                size=0,
                br=128000,
            )
        except Exception as exc:
            _log.warning("kuwo_search_fail | %s", exc)
            return None

    async def _get_kuwo_play_url(self, rid: str) -> str:
        """获取酷我音乐播放链接 - 使用 antiserver CDN 接口。"""
        # antiserver 接口稳定，不需要 token/csrf
        url = "http://antiserver.kuwo.cn/anti.s"
        params = {
            "type": "convert_url",
            "rid": rid,
            "format": "mp3",
            "response": "url",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers, follow_redirects=True) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                play_url = resp.text.strip()

            if play_url and play_url.startswith("http"):
                _log.info("kuwo_play_url_ok | rid=%s | url=%s", rid, play_url[:80])
                return play_url
            _log.warning("kuwo_no_play_url | rid=%s | resp=%s", rid, play_url[:100])
            return ""
        except Exception as exc:
            _log.warning("kuwo_play_url_fail | rid=%s | error=%s", rid, exc)
            return ""

    async def _search_qq(
        self,
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        """从 QQ 音乐搜索 - 使用 musicu.fcg 统一接口。"""
        keyword = f"{song_name} {artist}".strip()

        # 新版 QQ 音乐统一搜索接口
        req_data = {
            "comm": {"ct": 11, "cv": "12080008"},
            "req_1": {
                "method": "DoSearchForQQMusicDesktop",
                "module": "music.search.SearchCgiService",
                "param": {
                    "query": keyword,
                    "num_per_page": 5,
                    "page_num": 1,
                    "search_type": 0,
                },
            },
        }

        try:
            headers = dict(self._headers)
            headers["Referer"] = "https://y.qq.com/"

            async with httpx.AsyncClient(timeout=self._timeout, headers=headers, follow_redirects=True) as client:
                resp = await client.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=req_data)
                resp.raise_for_status()
                data = resp.json()

            songs = data.get("req_1", {}).get("data", {}).get("body", {}).get("song", {}).get("list", [])
            if not songs:
                # 回退到旧接口
                return await self._search_qq_legacy(song_name, artist, duration_ms)

            # 匹配最相似的歌曲
            best_match = self._find_best_match(songs, song_name, artist, duration_ms, "qq")
            if not best_match:
                return None

            # 获取播放链接
            song_mid = best_match.get("mid", "") or best_match.get("songmid", "")
            if not song_mid:
                return None

            play_url = await self._get_qq_play_url(song_mid)
            if not play_url:
                return None

            return AlternativeSource(
                url=play_url,
                source="qq",
                quality="hq",
                duration_ms=best_match.get("interval", 0) * 1000,
                size=best_match.get("size128", 0),
                br=128000,
            )
        except Exception as exc:
            _log.warning("qq_search_fail | %s", exc)
            # 回退到旧接口
            return await self._search_qq_legacy(song_name, artist, duration_ms)

    async def _search_qq_legacy(
        self,
        song_name: str,
        artist: str,
        duration_ms: int,
    ) -> AlternativeSource | None:
        """QQ 音乐旧版搜索接口（回退用）。"""
        keyword = f"{song_name} {artist}".strip()
        search_url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {
            "w": keyword,
            "format": "json",
            "n": 5,
            "p": 1,
            "cr": 1,
            "g_tk": 5381,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers, follow_redirects=True) as client:
                resp = await client.get(search_url, params=params)
                resp.raise_for_status()
                data = resp.json()

            songs = data.get("data", {}).get("song", {}).get("list", [])
            if not songs:
                return None

            best_match = self._find_best_match(songs, song_name, artist, duration_ms, "qq")
            if not best_match:
                return None

            song_mid = best_match.get("songmid", "") or best_match.get("mid", "")
            if not song_mid:
                return None

            play_url = await self._get_qq_play_url(song_mid)
            if not play_url:
                return None

            return AlternativeSource(
                url=play_url,
                source="qq",
                quality="hq",
                duration_ms=best_match.get("interval", 0) * 1000,
                size=best_match.get("size128", 0),
                br=128000,
            )
        except Exception as exc:
            _log.warning("qq_legacy_search_fail | %s", exc)
            return None

    async def _get_qq_play_url(self, song_mid: str) -> str:
        """获取 QQ 音乐播放链接。

        2026-08-06 实测：匿名（无登录 cookie）**拿不到任何 purl**。
        试过 4 种 module/platform 组合（``vkey.GetVkeyServer`` / ``music.vkey.GetVkey``、
        ``platform=20`` / ``yqq.json``、带/不带 ``filename``、带/不带 Referer）
        以及老端点 ``fcg_music_express_mobile3``，全部返回空 purl，
        响应里带 ``msg='<本机IP>;invalidq;'``。连《晴天》这种最大众的曲目也一样。

        所以 ``qq_vkey_no_purl`` 不是请求构造 bug，是**权限墙** —— 要能播必须提供
        QQ 音乐登录凭证（uin + qm_keyst），和 QZone cookie 同一类问题。
        本仓目前没有 QQ 音乐 cookie 的接线（``core/cookie_auth.py`` 只服务
        QZone / bilibili / douyin）。
        """

        # QQ 音乐 vkey 获取 API
        url = "https://u.y.qq.com/cgi-bin/musicu.fcg"

        req_data = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    # guid="0" 不符合上游要求（要十位数字）。首次干净探测时
                    # guid="0" 回 result=104003、换成十位数字回 result=0；
                    # 但连续请求几次后本机 IP 被限流，两者都回 104003。
                    # 无论哪种情况 purl 都是空的（那要凭证），改这里只是让请求本身合法。
                    "guid": self._qq_guid,
                    "songmid": [song_mid],
                    "songtype": [0],
                    "uin": "0",
                    "loginflag": 1,
                    "platform": "20",
                },
            },
        }

        try:
            params = {
                "format": "json",
                "data": json.dumps(req_data),
            }

            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers, follow_redirects=True) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            midurlinfo = data.get("req_0", {}).get("data", {}).get("midurlinfo", [])
            if not midurlinfo:
                _log.warning("qq_vkey_no_midurlinfo | song_mid=%s | response=%s", song_mid, data)
                return ""

            purl = midurlinfo[0].get("purl", "")
            if not purl:
                # 实测这条恒定发生在匿名访问下（见方法 docstring），不是偶发故障，
                # 所以降到 INFO 并只留判定所需的三个字段 —— 原来整个 40 键 dict
                # 刷进 WARNING，读起来像 bug，实际是「没登录」。
                info = midurlinfo[0]
                _log.info(
                    "qq_vkey_no_purl | song_mid=%s | result=%s | subcode=%s | "
                    "匿名无播放权限，需要 QQ 音乐登录凭证",
                    song_mid,
                    info.get("result"),
                    info.get("subcode"),
                )
                return ""

            # 拼接完整 URL
            play_url = f"http://dl.stream.qqmusic.qq.com/{purl}"
            _log.info("qq_play_url_ok | song_mid=%s | url=%s", song_mid, play_url)
            return play_url
        except Exception as exc:
            _log.warning("qq_vkey_fail | song_mid=%s | error=%s", song_mid, exc)
            return ""

    def _find_best_match(
        self,
        songs: list[dict[str, Any]],
        target_name: str,
        target_artist: str,
        target_duration_ms: int,
        source: str,
    ) -> dict[str, Any] | None:
        """从搜索结果中找到最匹配的歌曲 - 参考 UnblockNeteaseMusic 的匹配算法。"""
        if not songs:
            return None

        target_name_norm = self._normalize_text(target_name)
        target_artist_norm = self._normalize_text(target_artist)

        best_score = 0.0
        best_match = None

        for song in songs:
            # 根据不同平台提取字段
            if source == "qq":
                name = song.get("name", "") or song.get("songname", "")
                singers = song.get("singer", [])
                if isinstance(singers, list) and singers:
                    artist = "/".join(s.get("name", "") for s in singers if isinstance(s, dict) and s.get("name"))
                    if not artist and singers:
                        artist = singers[0].get("name", "") if isinstance(singers[0], dict) else ""
                else:
                    artist = ""
                duration = song.get("interval", 0) * 1000
            elif source == "kuwo":
                name = song.get("name", "") or song.get("SONGNAME", "") or song.get("NAME", "")
                artist = song.get("artist", "") or song.get("ARTIST", "")
                dur_raw = song.get("duration", 0) or song.get("DURATION", 0)
                try:
                    duration = int(dur_raw) * 1000
                except (ValueError, TypeError):
                    duration = 0
            elif source == "kugou":
                name = song.get("songname", "") or song.get("filename", "")
                artist = song.get("singername", "")
                duration = song.get("duration", 0) * 1000
            elif source == "migu":
                name = song.get("title", "") or song.get("songName", "") or song.get("name", "")
                # 新版 API 歌手在 singers 列表中
                singers = song.get("singers", [])
                if isinstance(singers, list) and singers:
                    artist = "/".join(
                        s.get("name", "") for s in singers if isinstance(s, dict) and s.get("name")
                    )
                if not artist:
                    artist = song.get("singerName", "") or song.get("singer", "") or song.get("artist", "")
                duration = 0
            elif source == "soundcloud":
                if not bool(song.get("streamable", True)):
                    continue
                name = song.get("title", "")
                publisher = song.get("publisher_metadata", {})
                if isinstance(publisher, dict):
                    artist = publisher.get("artist", "")
                else:
                    artist = ""
                if not artist:
                    user = song.get("user", {})
                    if isinstance(user, dict):
                        artist = user.get("username", "")
                duration = song.get("duration", 0) or song.get("full_duration", 0)
            else:
                continue

            if self._should_avoid_candidate_name(str(name), target_name):
                continue

            name_norm = self._normalize_text(name)
            artist_norm = self._normalize_text(artist)

            # 计算相似度 - 参考 UnblockNeteaseMusic 的算法
            name_score = self._similarity(target_name_norm, name_norm)
            artist_score = self._similarity(target_artist_norm, artist_norm)
            direct_name_hit = bool(
                target_name_norm and name_norm
                and (target_name_norm in name_norm or name_norm in target_name_norm)
            )
            min_name_score = 0.85 if len(target_name_norm) <= 4 else 0.72
            if target_name_norm and not direct_name_hit and name_score < min_name_score:
                continue

            # 歌手门：歌名对上但歌手完全是另一个人，说明这是翻唱/他人版本，
            # 不是同一段录音。剥掉括号修饰后歌名往往拿满分（`孤勇者 (cover: 陈奕迅)`
            # 归一化成 `孤勇者`），只靠总分阈值挡不住 —— 0.7+0+0.1 = 0.8 会通过。
            # 只在双方都有歌手信息时生效：候选缺字段不等于歌手不符。
            direct_artist_hit = bool(
                target_artist_norm and artist_norm
                and (target_artist_norm in artist_norm or artist_norm in target_artist_norm)
            )
            if (
                target_artist_norm
                and artist_norm
                and not direct_artist_hit
                and artist_score < self._MIN_ARTIST_SCORE
            ):
                continue

            # 时长匹配（允许 ±10 秒误差）
            duration_score = 1.0
            if target_duration_ms > 0 and duration > 0:
                duration_diff = abs(target_duration_ms - duration)
                if duration_diff > 10000:  # 超过 10 秒
                    duration_score = 0.7
                elif duration_diff > 5000:  # 超过 5 秒
                    duration_score = 0.85

            # 综合评分 - 歌名权重最高
            score = (name_score * 0.7 + artist_score * 0.2 + duration_score * 0.1)

            # 正片与带修饰的变体归一化后同分，靠这个微小罚分让干净标题胜出
            # （同时存在 `晴天` 和 `晴天 (Live)` 时要挑前者）。
            if self._strip_bracketed_qualifiers(html.unescape(str(name))) != html.unescape(str(name)):
                score -= self._BRACKETED_VARIANT_PENALTY

            if score > best_score:
                best_score = score
                best_match = song

        # 只返回相似度 > 0.5 的结果
        if best_score > 0.5:
            _log.info(
                "match_found | source=%s | score=%.2f | name=%s | artist=%s",
                source, best_score, target_name, target_artist,
            )
            return best_match
        return None

    @staticmethod
    def _strip_bracketed_qualifiers(text: str) -> str:
        """剥掉括号修饰（(DJ版)、(Live)、(cover: X) 等），保留主标题。

        必须在 ``normalize_matching_text`` **之前**调用 —— 后者把所有标点
        （含括号）替换成空格，跑完之后这两条正则永远匹配不到任何东西。
        """

        stripped = re.sub(r"\([^)]*\)", " ", text)
        stripped = re.sub(r"（[^）]*）", " ", stripped)
        # 整个名字都在括号里时（如 "(instrumental)"）不要剥成空串，
        # 否则候选会因为归一化后为空而静默消失。
        if not re.search(r"\w", stripped, flags=re.UNICODE):
            return text
        return stripped

    @classmethod
    def _normalize_query_text(cls, text: str) -> str:
        """归一化「发给上游的搜索词」—— 保留词边界。

        与 ``_normalize_text`` 的分工：这个用于**查询**，那个用于**比较**。
        把比较用的形态当查询词发出去会把 "Life's a Struggle" 压成
        "lifesastruggle"，上游搜不到任何东西。
        """

        if not text:
            return ""
        unescaped = html.unescape(text)
        # 撇号直接删（"Life's" -> "Lifes"），其余标点转空格以保住词边界。
        collapsed = re.sub(r"['’]", "", unescaped)
        collapsed = re.sub(r"[^\w\s一-鿿]", " ", collapsed)
        return re.sub(r"\s+", " ", collapsed).strip()

    @classmethod
    def _normalize_text(cls, text: str) -> str:
        """标准化文本 - 去除特殊字符、空格、转小写。用于**相似度比较**。"""
        if not text:
            return ""
        # 处理 HTML 实体，并在激进清理之前剥掉括号内容
        text = normalize_matching_text(cls._strip_bracketed_qualifiers(html.unescape(text)))
        # 移除特殊字符
        text = re.sub(r'[^\w\s]', '', text)
        # 移除空格并转小写
        return text.strip().lower().replace(" ", "")

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """计算两个字符串的相似度 - 使用 Levenshtein 距离的简化版本。"""
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0

        # 完全包含关系
        if a in b or b in a:
            shorter = min(len(a), len(b))
            longer = max(len(a), len(b))
            return shorter / longer * 0.95

        # 计算公共字符数
        common = sum(1 for c in a if c in b)
        max_len = max(len(a), len(b))

        if max_len == 0:
            return 0.0

        return common / max_len
