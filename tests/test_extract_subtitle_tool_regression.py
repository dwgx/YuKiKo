from __future__ import annotations

import asyncio
import unittest

import yaml

from core.agent_tools_media import _handle_extract_subtitle
from core.prompt_navigator import default_prompt_navigator_payload


class _Executor:
    """ToolExecutor 替身：只实现字幕链路要用的那个方法。"""

    def __init__(self, info: object, raises: bool = False) -> None:
        self._info = info
        self._raises = raises
        self.calls: list[str] = []

    async def _inspect_platform_video_metadata_safe(self, url: str) -> object:
        self.calls.append(url)
        if self._raises:
            raise RuntimeError("boom")
        return self._info


def _run(args: dict, executor: object | None) -> object:
    return asyncio.run(
        _handle_extract_subtitle(args, {"tool_executor": executor} if executor else {})
    )


class ExtractSubtitleToolRegressionTests(unittest.TestCase):
    """字幕提取链路早就存在，但只被 analyze_video 当内部证据用，agent 侧零出口。

    实测「提取这个视频的字幕」：模型调 analyze_video 后只能回「没拿到字幕哦」——
    字幕被抓出来、拼进证据、然后丢掉。`grep subtitle core/agent_tools_*.py` 为空。
    """

    def test_returns_subtitle_text_when_available(self) -> None:
        executor = _Executor(
            {
                "title": "测试视频",
                "subtitle_text": "第一句字幕\n第二句字幕",
                "subtitle_lang": "zh-CN",
                "subtitle_source": "subtitles",
            }
        )
        result = _run({"url": "https://www.bilibili.com/video/BV1x"}, executor)

        self.assertTrue(result.ok)
        self.assertIn("第一句字幕", result.display)
        self.assertEqual(result.data["subtitle_lang"], "zh-CN")
        self.assertFalse(result.data["truncated"])
        self.assertEqual(executor.calls, ["https://www.bilibili.com/video/BV1x"])

    def test_reports_honestly_when_no_subtitle_track(self) -> None:
        """没有字幕轨时必须明说，不能编造 —— 也不能报成成功。"""

        result = _run(
            {"url": "https://www.bilibili.com/video/BV1x"},
            _Executor({"title": "无字幕视频", "subtitle_text": ""}),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_subtitle_available")
        self.assertIn("没有可取的字幕轨", result.display)
        self.assertFalse(result.data["has_subtitle"])

    def test_truncates_at_max_chars_and_flags_it(self) -> None:
        long_text = "字" * 5000
        result = _run(
            {"url": "https://x.com/v", "max_chars": 500},
            _Executor({"subtitle_text": long_text}),
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["truncated"])
        self.assertEqual(result.data["total_chars"], 5000)
        self.assertLess(len(result.data["subtitle_text"]), 5000)

    def test_max_chars_is_clamped(self) -> None:
        for raw, _label in ((5, "过小"), (99999, "过大"), ("abc", "非数字"), (None, "缺省")):
            with self.subTest(repr(raw)):
                result = _run(
                    {"url": "https://x.com/v", "max_chars": raw},
                    _Executor({"subtitle_text": "字" * 20000}),
                )
                self.assertTrue(result.ok)
                self.assertLessEqual(len(result.data["subtitle_text"]), 12000 + 40)
                self.assertGreaterEqual(len(result.data["subtitle_text"]), 200)

    def test_signed_source_url_never_reaches_the_reply(self) -> None:
        """subtitle_source 实测是带签名的完整 timedtext URL（数百字符）。

        原样进 display 就是把签名 URL 发进群，还白烧 token。只留域名当来源标签。
        """

        signed = (
            "https://www.youtube.com/api/timedtext?v=dQw4w9WgXcQ"
            "&signature=D7CDC7695E8EA0925B21D9A39531BD38C7B1B844" + "A" * 200
        )
        result = _run(
            {"url": "https://x.com/v"},
            _Executor({"subtitle_text": "字幕内容", "subtitle_lang": "en", "subtitle_source": signed}),
        )
        self.assertTrue(result.ok)
        self.assertNotIn("signature", result.display)
        self.assertNotIn("timedtext", result.display)
        self.assertIn("www.youtube.com", result.display)
        self.assertEqual(result.data["subtitle_source"], "www.youtube.com")
        self.assertLess(len(result.display), 120)

    def test_non_url_source_label_passes_through(self) -> None:
        result = _run(
            {"url": "https://x.com/v"},
            _Executor({"subtitle_text": "x", "subtitle_source": "subtitles"}),
        )
        self.assertEqual(result.data["subtitle_source"], "subtitles")

    def test_missing_url_rejected(self) -> None:
        result = _run({}, _Executor({"subtitle_text": "x"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing url")

    def test_missing_executor_reports_unavailable(self) -> None:
        result = _run({"url": "https://x.com/v"}, None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "video_parser_unavailable")

    def test_extractor_exception_is_reported_not_swallowed(self) -> None:
        result = _run({"url": "https://x.com/v"}, _Executor(None, raises=True))
        self.assertFalse(result.ok)
        self.assertIn("extract_subtitle_error", result.error)

    def test_empty_metadata_reports_unavailable(self) -> None:
        for info in ({}, None, "not-a-dict"):
            with self.subTest(repr(info)):
                result = _run({"url": "https://x.com/v"}, _Executor(info))
                self.assertFalse(result.ok)
                self.assertEqual(result.error, "metadata_unavailable")


class SubtitleToolReachabilityTests(unittest.TestCase):
    """工具注册了还不够 —— 不在 navigator 分区里模型就够不着（scoped_tools 求交集）。"""

    def test_tool_is_listed_in_video_url_section(self) -> None:
        section = default_prompt_navigator_payload()["sections"]["video_url"]
        self.assertIn("extract_subtitle", section["tools"])

    def test_section_distinguishes_subtitle_text_from_summary(self) -> None:
        section = default_prompt_navigator_payload()["sections"]["video_url"]
        self.assertIn("extract_subtitle", section["when_to_use"])
        self.assertIn("字幕文字本身", section["when_to_use"])

    def test_all_three_prompt_sources_stay_in_sync(self) -> None:
        payload = default_prompt_navigator_payload()
        with open("config/templates/master.template.yml", encoding="utf-8") as fh:
            template = yaml.safe_load(fh)["prompts"]["prompt_navigator"]
        with open("config/prompts.yml", encoding="utf-8") as fh:
            prompts = yaml.safe_load(fh)["prompt_navigator"]
        self.assertEqual(payload, template)
        self.assertEqual(payload, prompts)

    def test_registered_in_media_tools(self) -> None:
        from core.agent_tools_registry import AgentToolRegistry
        from core.agent_tools_media import _register_media_tools

        registry = AgentToolRegistry()
        _register_media_tools(registry, None, {})
        self.assertIn("extract_subtitle", registry._schemas)
        schema = registry.get_schema("extract_subtitle")
        self.assertEqual(schema.parameters["required"], ["url"])


if __name__ == "__main__":
    unittest.main()
