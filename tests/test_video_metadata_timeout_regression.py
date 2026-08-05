from __future__ import annotations

import asyncio
import unittest

import yaml

from core.config_templates import _built_in_config_defaults


class VideoMetadataTimeoutRegressionTests(unittest.TestCase):
    """字幕/元数据探测的超时曾经是 12 秒，而 yt-dlp 实测要 8.3~12.6 秒。

    擦边值 + 静默吞异常 = 间歇性失败且日志无痕。实测三连:
    8.6s 通过 / 8.3s 通过 / 12.6s 超时 —— 而 sync 层三次都成功拿到了 2089 字字幕。
    异步包装的 `except Exception: return {}` 把超时抹掉，排查时完全看不出原因。
    """

    def test_default_timeout_has_headroom_over_observed_latency(self) -> None:
        defaults = _built_in_config_defaults()
        got = defaults["search"]["video_resolver"]["metadata_timeout_seconds"]
        # 实测上界 12.6s，默认值必须显著高于它才有余量。
        self.assertGreaterEqual(got, 20, "12 秒是擦边值，实测已见 12.6 秒")

    def test_key_present_in_both_truth_sources(self) -> None:
        """`core/tools.py:149` 读它，但两处真相源此前都没有 ——
        升级安装拿不到这个键，超时无法从 WebUI 调。
        与 routing.fragment_join_enable 同一类 bug。"""

        defaults = _built_in_config_defaults()
        with open("config/templates/master.template.yml", encoding="utf-8") as fh:
            template = yaml.safe_load(fh)["config"]

        self.assertIn(
            "metadata_timeout_seconds", defaults["search"]["video_resolver"]
        )
        self.assertIn(
            "metadata_timeout_seconds", template["search"]["video_resolver"]
        )
        self.assertEqual(
            defaults["search"]["video_resolver"]["metadata_timeout_seconds"],
            template["search"]["video_resolver"]["metadata_timeout_seconds"],
        )

    def test_timeout_is_logged_not_swallowed(self) -> None:
        """静默是这个 bug 藏住的唯一原因，超时和其它异常都必须留日志。"""

        from core.tools import ToolExecutor

        executor = ToolExecutor.__new__(ToolExecutor)
        executor._video_metadata_timeout_seconds = 1

        async def _never_finishes(_url: str) -> dict:
            await asyncio.sleep(10)
            return {"unreachable": True}

        executor._inspect_platform_video_metadata = _never_finishes

        with self.assertLogs("yukiko.ytdlp", level="WARNING") as captured:
            got = asyncio.run(
                ToolExecutor._inspect_platform_video_metadata_safe(
                    executor, "https://www.youtube.com/watch?v=x"
                )
            )

        self.assertEqual(got, {})
        self.assertTrue(
            any("video_metadata_timeout" in line for line in captured.output),
            captured.output,
        )

    def test_other_errors_are_logged_with_type_and_message(self) -> None:
        from core.tools import ToolExecutor

        executor = ToolExecutor.__new__(ToolExecutor)
        executor._video_metadata_timeout_seconds = 30

        async def _raises(_url: str) -> dict:
            raise RuntimeError("upstream 412")

        executor._inspect_platform_video_metadata = _raises

        with self.assertLogs("yukiko.ytdlp", level="WARNING") as captured:
            got = asyncio.run(
                ToolExecutor._inspect_platform_video_metadata_safe(
                    executor, "https://www.bilibili.com/video/BV1x"
                )
            )

        self.assertEqual(got, {})
        joined = "\n".join(captured.output)
        self.assertIn("video_metadata_error", joined)
        self.assertIn("RuntimeError", joined)
        self.assertIn("412", joined)


if __name__ == "__main__":
    unittest.main()
