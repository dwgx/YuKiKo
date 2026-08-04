"""core/tools_video.py A9：语义猜测 → 显式工具参数 的契约回归。

原来这些行为由本地词表猜（「发我」放宽时长上限、「总结一下」偷偷改成深度分析、
「只要总结」把视频吞掉）。现在全部由模型选完工具后传进来的显式参数决定。
这里逐条钉住新契约，并钉住「自然语言不再触发任何东西」。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.tools import ToolExecutor
from core.tools_video import _optional_flag_arg
from core.video_analyzer import VideoAnalysisResult


class _DummyExecutor(ToolExecutor):
    def __init__(self) -> None:
        super().__init__(None, None, lambda *args, **kwargs: None, {})


class VideoDurationSceneIsExplicitTests(unittest.TestCase):
    """时长上限只认显式 duration_scene，不再从 query 猜。"""

    def setUp(self) -> None:
        self.executor = _DummyExecutor()

    def test_should_use_conservative_default_limit_when_scene_not_given(self) -> None:
        self.assertEqual(
            self.executor._pick_video_duration_limit(),
            (self.executor._video_search_max_duration_seconds, "default"),
        )
        self.assertEqual(
            self.executor._pick_video_duration_limit(""),
            (self.executor._video_search_max_duration_seconds, "default"),
        )

    def test_should_widen_limit_only_for_explicit_send_scene(self) -> None:
        self.assertEqual(
            self.executor._pick_video_duration_limit("send"),
            (self.executor._video_search_send_max_duration_seconds, "send"),
        )
        self.assertEqual(
            self.executor._pick_video_duration_limit("SEND"),
            (self.executor._video_search_send_max_duration_seconds, "send"),
        )

    def test_should_widen_limit_only_for_explicit_analysis_scene(self) -> None:
        self.assertEqual(
            self.executor._pick_video_duration_limit("analysis"),
            (self.executor._video_search_analysis_max_duration_seconds, "analysis"),
        )

    def test_should_not_widen_limit_from_natural_language_send_phrases(self) -> None:
        # 迁移前：这三句都会被词表判成 send 档（1800s）。现在只是无法识别的 scene 值。
        for phrase in ("把这个视频发我", "下载这个视频给我", "转发这个视频到群里"):
            self.assertEqual(
                self.executor._pick_video_duration_limit(phrase),
                (self.executor._video_search_max_duration_seconds, "default"),
                msg=phrase,
            )

    def test_should_reject_overlong_video_against_the_scene_limit(self) -> None:
        # 1200s：default 档（600s）拒收，send 档（1800s）放行 —— 上限由显式 scene 决定。
        meta = {"duration": 1200}
        self.assertEqual(
            self.executor._is_video_duration_acceptable_for_search(meta),
            (False, 600, 1200, "default"),
        )
        self.assertEqual(
            self.executor._is_video_duration_acceptable_for_search(
                meta, duration_scene="send"
            ),
            (True, 1800, 1200, "send"),
        )
        # 2000s 超过 send 档上限，仍应拒收；只有显式 analysis 档（2400s）才放行。
        self.assertFalse(
            self.executor._is_video_duration_acceptable_for_search(
                {"duration": 2000}, duration_scene="send"
            )[0]
        )
        self.assertTrue(
            self.executor._is_video_duration_acceptable_for_search(
                {"duration": 2000}, duration_scene="analysis"
            )[0]
        )

    def test_should_accept_unknown_duration_regardless_of_scene(self) -> None:
        ok, limit, duration, scene = (
            self.executor._is_video_duration_acceptable_for_search({"duration": 0})
        )
        self.assertTrue(ok)
        self.assertEqual((limit, duration, scene), (600, 0, "default"))


class VideoAnalysisIntentIsExplicitTests(unittest.TestCase):
    """是否做深度分析 / 是否只回文字，由显式参数决定；自然语言不再触发。"""

    def setUp(self) -> None:
        self.executor = _DummyExecutor()

    def test_should_not_treat_natural_language_as_analysis_request(self) -> None:
        for phrase in (
            "总结一下这个视频",
            "这个视频讲了什么",
            "评价一下",
            "怎么看这个视频",
            "解析 https://www.bilibili.com/video/BV1xx411c7mD/",
        ):
            self.assertFalse(
                self.executor._looks_like_video_analysis_request(phrase), msg=phrase
            )

    def test_should_still_honour_explicit_analyze_control_token(self) -> None:
        self.assertTrue(
            self.executor._looks_like_video_analysis_request(
                "/analyze https://example.com/demo.mp4"
            )
        )
        self.assertTrue(
            self.executor._looks_like_video_analysis_request("output=text")
        )

    def test_explicit_output_mode_should_beat_typed_token(self) -> None:
        # output_mode=video 时即使用户打了 output=text 也不降级成纯文字。
        self.assertFalse(
            self.executor._wants_text_only_output(
                output_mode="video", fallback_text="output=text"
            )
        )
        self.assertTrue(
            self.executor._wants_text_only_output(
                output_mode="text", fallback_text="随便什么"
            )
        )

    def test_missing_output_mode_should_fall_back_to_typed_token_only(self) -> None:
        self.assertTrue(
            self.executor._wants_text_only_output(
                output_mode="", fallback_text="output=text"
            )
        )
        self.assertTrue(
            self.executor._wants_text_only_output(
                output_mode="auto", fallback_text="/summary"
            )
        )
        # 自然语言「只要总结」迁移前会吞掉视频，现在不会。
        self.assertFalse(
            self.executor._wants_text_only_output(
                output_mode="", fallback_text="只要总结不用发视频"
            )
        )


class BuildVideoResultRespectsExplicitArgsTests(unittest.TestCase):
    """搜索结果组装：深度分析与纯文字输出都只听显式参数。"""

    def setUp(self) -> None:
        self.executor = _DummyExecutor()
        analysis = VideoAnalysisResult(
            source_url="https://www.bilibili.com/video/BV1xx411c7mD/",
            platform="bilibili",
            title="标题",
            uploader="作者",
            duration=10,
            analysis_depth="rich_metadata",
        )
        self.executor._video_analyzer = SimpleNamespace(
            analyze=AsyncMock(return_value=analysis)
        )

    def _build(self, **kwargs: object) -> object:
        return asyncio.run(
            self.executor._build_video_result_with_analysis(
                source_url="https://www.bilibili.com/video/BV1xx411c7mD/",
                resolved="https://cdn.example.com/x.mp4",
                # 迁移前这句自然语言会把结果偷偷改成深度分析
                query="总结一下这个视频",
                meta={"duration": 10},
                **kwargs,
            )
        )

    def test_should_not_analyze_when_analyze_content_absent(self) -> None:
        payload = self._build().payload or {}
        self.assertEqual(payload.get("mode"), "video")
        self.assertIsNone(payload.get("video_analysis"))
        self.assertTrue(payload.get("video_url"))

    def test_should_not_analyze_when_analyze_content_is_false(self) -> None:
        payload = (self._build(analyze_content=False).payload) or {}
        self.assertIsNone(payload.get("video_analysis"))

    def test_should_analyze_when_analyze_content_is_true(self) -> None:
        payload = (self._build(analyze_content=True).payload) or {}
        self.assertTrue(payload.get("video_analysis"))
        self.assertEqual(payload.get("mode"), "video")
        self.assertTrue(payload.get("video_url"))

    def test_text_output_mode_should_drop_the_video(self) -> None:
        payload = (
            self._build(analyze_content=True, output_mode="text").payload
        ) or {}
        self.assertEqual(payload.get("mode"), "text")
        self.assertEqual(payload.get("video_url"), "")

    def test_video_output_mode_should_keep_the_video(self) -> None:
        payload = (
            self._build(analyze_content=True, output_mode="video").payload
        ) or {}
        self.assertEqual(payload.get("mode"), "video")
        self.assertTrue(payload.get("video_url"))


class VideoRequestDetectionIsStructuralTests(unittest.TestCase):
    """_looks_like_video_request 只剩结构信号（扩展名直链 / 控制令牌）。"""

    def setUp(self) -> None:
        self.executor = _DummyExecutor()

    def test_should_detect_direct_video_url_by_extension(self) -> None:
        self.assertTrue(
            self.executor._looks_like_video_request("https://example.com/demo.mp4")
        )
        self.assertTrue(
            self.executor._looks_like_video_request(
                "看看 https://example.com/a.m3u8"
            )
        )

    def test_should_detect_explicit_video_control_token(self) -> None:
        self.assertTrue(self.executor._looks_like_video_request("/video 猫猫"))

    def test_should_not_detect_video_intent_from_natural_language(self) -> None:
        for phrase in (
            "发个抖音视频",
            "把这个视频发我",
            "抖音 找点好玩的视频",
        ):
            self.assertFalse(
                self.executor._looks_like_video_request(phrase), msg=phrase
            )


class RemovedKeywordSymbolsTests(unittest.TestCase):
    """双重死亡的两个符号已删除，不允许悄悄回来。"""

    def test_video_send_and_douyin_search_keyword_helpers_are_gone(self) -> None:
        executor = _DummyExecutor()
        self.assertFalse(hasattr(executor, "_looks_like_video_send_request"))
        self.assertFalse(hasattr(executor, "_looks_like_douyin_search_request"))

    def test_module_no_longer_imports_keyword_cue_helpers(self) -> None:
        import core.tools_video as tools_video

        self.assertFalse(hasattr(tools_video, "_prompt_cues"))
        self.assertFalse(hasattr(tools_video, "_shared_video_request"))
        self.assertFalse(hasattr(tools_video, "_pl"))


class OptionalFlagArgTests(unittest.TestCase):
    """三态布尔归一：None 表示模型没给，不要替它猜。"""

    def test_should_return_none_for_absent_or_unparseable_values(self) -> None:
        for value in (None, "", "maybe", "大概吧"):
            self.assertIsNone(_optional_flag_arg(value), msg=repr(value))

    def test_should_normalize_truthy_and_falsy_spellings(self) -> None:
        for value in (True, "true", "TRUE", "yes", "on", "1", 1):
            self.assertTrue(_optional_flag_arg(value), msg=repr(value))
        for value in (False, "false", "no", "off", "0", 0):
            self.assertFalse(_optional_flag_arg(value), msg=repr(value))


if __name__ == "__main__":
    unittest.main()
