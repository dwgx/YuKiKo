"""候选评分 + 多解析器聚合回归（S4：对比 Reflection_King 补齐差距）。

覆盖：score_candidate 的 kind 基础分 / 分辨率加分 / 码率加分 / 广告扣分 / 签名标记，
pick_best_candidate 的排序与 evidence 加分，以及 tools_video 聚合层的按 URL 去重。
"""
from __future__ import annotations

import shutil
import unittest
from unittest.mock import patch

import core.tools_video as tools_video
from core.candidate_scoring import (
    EVIDENCE_BONUS,
    kind_for_url,
    parse_height_from_quality,
    pick_best_candidate,
    score_candidate,
)
from core.tools import ToolExecutor


class ScoreCandidateTests(unittest.TestCase):
    def test_kind_base_scores(self) -> None:
        self.assertEqual(score_candidate("video", "https://cdn/x.mp4")[0], 78)
        self.assertEqual(score_candidate("manifest", "https://cdn/x.m3u8")[0], 76)
        self.assertEqual(score_candidate("audio", "https://cdn/x.m4a")[0], 70)
        self.assertEqual(score_candidate("other", "https://cdn/x")[0], 60)

    def test_resolution_bonus_tiers(self) -> None:
        self.assertEqual(score_candidate("video", "u", height=2160)[0], 78 + 12)
        self.assertEqual(score_candidate("video", "u", height=1080)[0], 78 + 12)
        self.assertEqual(score_candidate("video", "u", height=720)[0], 78 + 8)
        self.assertEqual(score_candidate("video", "u", height=480)[0], 78 + 4)
        self.assertEqual(score_candidate("video", "u", height=360)[0], 78)

    def test_resolution_bonus_falls_back_to_width(self) -> None:
        # 16:9 估算：1920 宽 → 1080 高 → +12
        self.assertEqual(score_candidate("video", "u", width=1920)[0], 78 + 12)
        self.assertEqual(score_candidate("video", "u", height=None)[0], 78)

    def test_bitrate_bonus_tiers(self) -> None:
        self.assertEqual(score_candidate("video", "u", bitrate=2_500_000)[0], 78 + 6)
        self.assertEqual(score_candidate("video", "u", bitrate=1_200_000)[0], 78 + 4)
        self.assertEqual(score_candidate("video", "u", bitrate=600_000)[0], 78 + 2)
        self.assertEqual(score_candidate("video", "u", bitrate=100_000)[0], 78)

    def test_ad_keywords_penalty(self) -> None:
        for bad in ("https://cdn/x/ad_1.mp4", "https://cdn/ads/x.mp4", "https://marketing.cdn/x.mp4"):
            score, breakdown = score_candidate("video", bad)
            self.assertEqual(score, 78 - 80)
            self.assertEqual(breakdown["ad_penalty"], -80)
        score, breakdown = score_candidate("video", "https://cdn/x.mp4")
        self.assertEqual(score, 78)
        self.assertEqual(breakdown["ad_penalty"], 0)

    def test_url_lower_override(self) -> None:
        # url 本身不含广告词，但 url_lower 命中 → 照常扣分（调用方可注入归一化小写）
        score, _ = score_candidate("video", "https://cdn/x.mp4", url_lower="https://cdn/ads/x.mp4")
        self.assertEqual(score, 78 - 80)

    def test_signed_url_marker(self) -> None:
        for signed in ("https://cdn/x.mp4?sign=abc", "https://cdn/x.mp4?auth_key=1&expires=2"):
            score, breakdown = score_candidate("video", signed)
            self.assertEqual(score, 78)  # 只标记不加分
            self.assertTrue(breakdown["signed"])
        _, breakdown = score_candidate("video", "https://cdn/x.mp4")
        self.assertFalse(breakdown["signed"])

    def test_breakdown_contains_all_parts(self) -> None:
        _, breakdown = score_candidate("video", "u", height=1080, bitrate=2_500_000)
        self.assertEqual(
            breakdown,
            {
                "kind": "video",
                "base": 78,
                "resolution": 12,
                "bitrate": 6,
                "ad_penalty": 0,
                "signed": False,
                "total": 96,
            },
        )


class PickBestCandidateTests(unittest.TestCase):
    def test_picks_highest_score_and_attaches_breakdown(self) -> None:
        candidates = [
            {"url": "https://cdn/a.mp4", "kind": "video", "height": 480},
            {"url": "https://cdn/b.mp4", "kind": "video", "height": 1080},
        ]
        best = pick_best_candidate(candidates)
        self.assertEqual(best["url"], "https://cdn/b.mp4")
        self.assertEqual(best["score"], 78 + 12)
        self.assertEqual(best["score_breakdown"]["kind"], "video")
        self.assertEqual(best["score_breakdown"]["total"], best["score"])

    def test_manifest_loses_to_video_at_same_resolution(self) -> None:
        # 同分辨率下 video 基础分 78 > manifest 76，直链优先于 HLS 流
        candidates = [
            {"url": "https://cdn/s.m3u8", "kind": "manifest", "height": 1080},
            {"url": "https://cdn/v.mp4", "kind": "video", "height": 1080},
        ]
        best = pick_best_candidate(candidates)
        self.assertEqual(best["url"], "https://cdn/v.mp4")  # 78+12 > 76+12

    def test_evidence_bonus_for_multi_source(self) -> None:
        candidates = [
            {
                "url": "https://cdn/a.mp4",
                "kind": "video",
                "height": 720,
                "evidence_count": 2,
            },
            {"url": "https://cdn/b.mp4", "kind": "video", "height": 1080},
        ]
        best = pick_best_candidate(candidates)
        self.assertEqual(best["url"], "https://cdn/a.mp4")  # 78+8+5 > 78+12
        self.assertEqual(best["score_breakdown"]["evidence_bonus"], EVIDENCE_BONUS)
        self.assertEqual(best["score"], 78 + 8 + EVIDENCE_BONUS)

    def test_empty_and_invalid_candidates(self) -> None:
        self.assertEqual(pick_best_candidate([]), {})
        self.assertEqual(pick_best_candidate([{"kind": "video"}, "junk"]), {})

    def test_stable_order_on_ties(self) -> None:
        candidates = [
            {"url": "https://cdn/first.mp4", "kind": "video"},
            {"url": "https://cdn/second.mp4", "kind": "video"},
        ]
        best = pick_best_candidate(candidates)
        self.assertEqual(best["url"], "https://cdn/first.mp4")


class UrlKindHelpersTests(unittest.TestCase):
    def test_kind_for_url(self) -> None:
        self.assertEqual(kind_for_url("https://cdn/x.mp4"), "video")
        self.assertEqual(kind_for_url("https://cdn/x.flv?token=1"), "video")
        self.assertEqual(kind_for_url("https://cdn/x.m3u8"), "manifest")
        self.assertEqual(kind_for_url("https://cdn/x.m4a"), "audio")
        self.assertEqual(kind_for_url("https://cdn/x.mp3"), "audio")

    def test_parse_height_from_quality(self) -> None:
        self.assertEqual(parse_height_from_quality("720P"), 720)
        self.assertEqual(parse_height_from_quality("1080p"), 1080)
        self.assertEqual(parse_height_from_quality("4K"), 2160)
        self.assertIsNone(parse_height_from_quality("best"))
        self.assertIsNone(parse_height_from_quality(""))


class AggregationDedupeTests(unittest.TestCase):
    def _make_executor(self) -> ToolExecutor:
        return ToolExecutor(None, None, lambda *args, **kwargs: None, {})

    def test_same_url_from_two_resolvers_gets_evidence_count_and_bonus(self) -> None:
        executor = self._make_executor()

        def fake_probe(executable: str, extra_args: list[str], source_url: str):
            _ = extra_args, source_url
            if executable.endswith("you-get"):
                return {
                    "streams": {
                        "mp4": {
                            "quality": "720P",
                            "url": "https://cdn.example.com/a.mp4",
                        },
                        "flv": {
                            "quality": "480P",
                            "url": ["https://cdn.example.com/b.flv"],
                        },
                    }
                }
            if executable.endswith("streamlink"):
                return {
                    "plugin": "generic",
                    "streams": {
                        "720p": {
                            "url": "https://cdn.example.com/a.mp4",
                            "quality": "720p",
                        },
                        "best": {
                            "url": "https://cdn.example.com/a.m3u8",
                            "quality": "best",
                        },
                    },
                }
            return {}

        with (
            patch.object(tools_video, "YoutubeDL", None),
            patch.object(
                shutil,
                "which",
                side_effect=lambda name: (
                    f"/usr/bin/{name}" if name in ("you-get", "streamlink") else None
                ),
            ),
            patch.object(executor, "_probe_resolver_json", side_effect=fake_probe),
        ):
            candidates = executor._collect_aggregated_video_candidates(
                "https://example.com/video/123"
            )

        by_url = {c["url"]: c for c in candidates}
        self.assertEqual(len(candidates), 3)  # a.mp4 / b.flv / a.m3u8 去重后
        shared = by_url["https://cdn.example.com/a.mp4"]
        self.assertEqual(shared["evidence_count"], 2)
        self.assertEqual(shared["sources"], ["you-get", "streamlink"])

        best = pick_best_candidate(candidates)
        # 720p 双来源 91 分 > 480p 单来源 82 分 > manifest 单来源 76 分
        self.assertEqual(best["url"], "https://cdn.example.com/a.mp4")
        self.assertEqual(best["score_breakdown"]["evidence_bonus"], EVIDENCE_BONUS)
        self.assertEqual(best["score"], 78 + 8 + EVIDENCE_BONUS)

    def test_ytdlp_info_rows_are_aggregated_with_kinds(self) -> None:
        collected: list[dict] = []

        def add(cand: dict, source: str) -> None:
            _ = source
            collected.append(cand)

        info = {
            "url": "https://cdn.example.com/root.mp4",
            "height": 1080,
            "formats": [
                {"url": "https://cdn.example.com/root.mp4", "height": 1080},
                {"url": "https://cdn.example.com/hls.m3u8", "height": 720},
                {"url": "https://cdn.example.com/audio.m4a"},
            ],
        }
        tools_video.ToolVideoMixin._add_ytdlp_info_candidates(add, info)
        self.assertEqual(len(collected), 4)
        self.assertEqual(
            {c["kind"] for c in collected}, {"video", "manifest", "audio"}
        )
        m4a = next(c for c in collected if c["url"].endswith("audio.m4a"))
        self.assertEqual(m4a["kind"], "audio")


if __name__ == "__main__":
    unittest.main()
