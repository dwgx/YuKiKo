"""小红书（xiaohongshu.com / xhslink.com）解析器回归测试。

S3：平台判定（详情页各形态 / 短链 / 非详情页）+ mock 详情页 HTML 提取
（图文多图 → image_urls、视频 → video_url、无数据/登录墙 → 明确错误）。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from core.tools import ToolExecutor
from core.xhs import XhsResult, fetch_xhs_post, is_xhs_detail_url, is_xhs_url, parse_xhs_html


class _DummyExecutor(ToolExecutor):
    def __init__(self, config: dict | None = None) -> None:
        super().__init__(None, None, lambda *args, **kwargs: None, config or {})


class XhsUrlDetectionTests(unittest.TestCase):
    """平台判定：xiaohongshu.com / xhslink.com 各形态。"""

    def setUp(self) -> None:
        self.executor = _DummyExecutor()

    def test_should_detect_xhs_hosts(self) -> None:
        for url in (
            "https://www.xiaohongshu.com/explore/64a1b2c3d4e5f6a7b8c9d0e1",
            "https://www.xiaohongshu.com/discovery/item/64a1b2c3d4e5f6a7b8c9d0e1",
            "https://www.xiaohongshu.com/user/profile/5f4e3d2c1b0a",
            "https://xhslink.com/a/AbCdEf",
            "http://xhslink.com/AbCdEf",
        ):
            self.assertTrue(self.executor._is_supported_platform_video_url(url), msg=url)
            self.assertTrue(is_xhs_url(url), msg=url)

    def test_should_not_detect_non_xhs_hosts(self) -> None:
        for url in (
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://www.douyin.com/video/123456",
            "https://www.baidu.com/",
            "https://xhs.example.com/a/1",
        ):
            self.assertFalse(is_xhs_url(url), msg=url)
            # 原有平台判定不受影响
            if "bilibili.com" in url:
                self.assertTrue(self.executor._is_supported_platform_video_url(url), msg=url)

    def test_should_accept_xhs_detail_urls(self) -> None:
        for url in (
            "https://www.xiaohongshu.com/explore/64a1b2c3d4e5f6a7b8c9d0e1",
            "https://www.xiaohongshu.com/explore/64a1b2c3d4e5f6a7b8c9d0e1?xsec_token=abc",
            "https://www.xiaohongshu.com/discovery/item/64a1b2c3d4e5f6a7b8c9d0e1",
            "https://www.xiaohongshu.com/user/profile/5f4e3d2c1b0a",
            "https://xhslink.com/a/AbCdEf",
        ):
            self.assertTrue(self.executor._is_platform_video_detail_url(url), msg=url)
            self.assertTrue(is_xhs_detail_url(url), msg=url)

    def test_should_reject_xhs_non_detail_urls(self) -> None:
        for url in (
            "https://www.xiaohongshu.com/explore",
            "https://www.xiaohongshu.com/",
            "https://www.xiaohongshu.com/search_result?keyword=猫",
            "https://www.xiaohongshu.com/explore/",
        ):
            self.assertFalse(self.executor._is_platform_video_detail_url(url), msg=url)
            self.assertFalse(is_xhs_detail_url(url), msg=url)

    def test_existing_platforms_should_still_work(self) -> None:
        # 加小红书判定不能破坏既有平台
        self.assertTrue(
            self.executor._is_platform_video_detail_url(
                "https://www.bilibili.com/video/BV1xx411c7mD"
            )
        )
        self.assertTrue(
            self.executor._is_platform_video_detail_url(
                "https://www.douyin.com/video/7456800000000000000"
            )
        )
        self.assertFalse(
            self.executor._is_platform_video_detail_url("https://www.bilibili.com/")
        )


class XhsParseHtmlTests(unittest.TestCase):
    """mock 详情页 HTML：INITIAL_STATE 图文多图 / 视频 / og 兜底 / 无数据 / 登录墙。"""

    # 图文：noteDetailMap 带 imageList 3 张（json.dumps 保证合法 JSON）
    IMAGE_HTML = """<html><head><title>周末探店记录</title></head><body>
<script>window.__INITIAL_STATE__={}</script>
</body></html>""".format(
        json.dumps(
            {
                "note": {
                    "noteDetailMap": {
                        "64abc": {
                            "note": {
                                "type": "normal",
                                "title": "周末探店记录",
                                "desc": "好吃",
                                "user": {"nickname": "吃货小分队"},
                                "imageList": [
                                    {
                                        "urlDefault": "https://sns-webpic-qc.xhscdn.com/photo/1.jpg!nd_dft_wlteh_webp_3",
                                        "url": "https://sns-webpic-qc.xhscdn.com/photo/1.jpg!nd_dft_wlteh_webp_3",
                                    },
                                    {
                                        "urlDefault": "https://sns-webpic-qc.xhscdn.com/photo/2.jpg!nd_dft_wlteh_webp_3",
                                    },
                                    {
                                        # 无 urlDefault：infoList 兜底，取最后（最大）一张
                                        "infoList": [
                                            {"url": "https://sns-webpic-qc.xhscdn.com/photo/3_small.jpg"},
                                            {"url": "https://sns-webpic-qc.xhscdn.com/photo/3_orig.jpg!x"},
                                        ],
                                    },
                                ],
                            }
                        }
                    }
                }
            },
            ensure_ascii=False,
        )
    )

    # 视频：type=video + h264 masterUrl
    VIDEO_HTML = """<html><head><title>教程视频</title></head><body>
<script>window.__INITIAL_STATE__={}</script>
</body></html>""".format(
        json.dumps(
            {
                "note": {
                    "noteDetailMap": {
                        "64abc": {
                            "note": {
                                "type": "video",
                                "title": "三分钟教程",
                                "desc": "",
                                "user": {"nickname": "up主"},
                                "imageList": [
                                    {
                                        "urlDefault": "https://sns-webpic-qc.xhscdn.com/cover.jpg!nd_dft_wlteh_webp_3",
                                    }
                                ],
                                "video": {
                                    "media": {
                                        "stream": {
                                            "h264": [
                                                {
                                                    "masterUrl": "https://sns-video-qc.xhscdn.com/v/stream.m3u8",
                                                    "backupUrls": [
                                                        "https://sns-video-qc.xhscdn.com/v/backup.m3u8"
                                                    ],
                                                }
                                            ]
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            },
            ensure_ascii=False,
        )
    )

    # 无 INITIAL_STATE：只有 og 元数据
    OG_ONLY_HTML = """
    <html><head>
    <meta property="og:title" content="纯 og 图文" />
    <meta property="og:image" content="https://sns-webpic-qc.xhscdn.com/og/cover.jpg!nd_dft_wlteh_webp_3" />
    </head><body></body></html>
    """

    # content 在 property 前面的 og 顺序
    OG_REVERSED_HTML = """
    <html><head>
    <meta content="https://sns-webpic-qc.xhscdn.com/og/reversed.jpg!nd_dft_wlteh_webp_3" property="og:image" />
    </head><body></body></html>
    """

    def test_should_extract_multi_image_note(self) -> None:
        result = parse_xhs_html(self.IMAGE_HTML, "https://www.xiaohongshu.com/explore/64abc")
        self.assertEqual(result.error, "")
        self.assertEqual(result.kind, "image")
        self.assertEqual(result.title, "周末探店记录")
        self.assertEqual(result.uploader, "吃货小分队")
        # 3 张图，且 `!` 后缀被截掉拿原图
        self.assertEqual(len(result.image_urls), 3)
        self.assertEqual(
            result.image_urls[0],
            "https://sns-webpic-qc.xhscdn.com/photo/1.jpg",
        )
        # infoList 兜底取最大图
        self.assertEqual(
            result.image_urls[2],
            "https://sns-webpic-qc.xhscdn.com/photo/3_orig.jpg",
        )
        self.assertEqual(result.video_url, "")

    def test_should_extract_video_note(self) -> None:
        result = parse_xhs_html(self.VIDEO_HTML, "https://www.xiaohongshu.com/explore/64abc")
        self.assertEqual(result.error, "")
        self.assertEqual(result.kind, "video")
        self.assertEqual(result.title, "三分钟教程")
        self.assertEqual(result.uploader, "up主")
        self.assertEqual(
            result.video_url,
            "https://sns-video-qc.xhscdn.com/v/stream.m3u8",
        )
        # 视频封面也保留在 image_urls
        self.assertEqual(len(result.image_urls), 1)

    def test_should_fall_back_to_og_metadata(self) -> None:
        result = parse_xhs_html(self.OG_ONLY_HTML, "https://xhslink.com/a/AbCdEf")
        self.assertEqual(result.error, "")
        self.assertEqual(result.kind, "image")
        self.assertEqual(result.title, "纯 og 图文")
        self.assertEqual(
            result.image_urls,
            ["https://sns-webpic-qc.xhscdn.com/og/cover.jpg"],
        )

    def test_should_parse_og_with_content_before_property(self) -> None:
        result = parse_xhs_html(self.OG_REVERSED_HTML, "https://xhslink.com/a/AbCdEf")
        self.assertEqual(result.error, "")
        self.assertEqual(result.image_urls, ["https://sns-webpic-qc.xhscdn.com/og/reversed.jpg"])

    def test_should_report_no_data_on_empty_html(self) -> None:
        result = parse_xhs_html("<html><body>页面渲染中...</body></html>", "https://xhslink.com/a/AbCdEf")
        self.assertEqual(result.error, "no_data")
        self.assertEqual(result.image_urls, [])
        self.assertEqual(result.video_url, "")

    def test_should_report_blocked_on_login_wall(self) -> None:
        blocked_html = "<html><body>请先登录后再查看完整内容</body></html>"
        result = parse_xhs_html(blocked_html, "https://www.xiaohongshu.com/explore/64abc")
        self.assertEqual(result.error, "blocked")

    def test_should_report_blocked_on_captcha_page(self) -> None:
        blocked_html = '<html><body><div id="captcha">安全验证</div></body></html>'
        result = parse_xhs_html(blocked_html, "https://www.xiaohongshu.com/explore/64abc")
        self.assertEqual(result.error, "blocked")

    def test_should_not_fake_data_from_og_cover_of_video_page(self) -> None:
        # 视频页只给 og:image 时按图文处理是错的 —— og:image 是封面，
        # 若同时有 og:video 应判为视频。
        html = (
            '<html><head>'
            '<meta property="og:image" content="https://sns-webpic-qc.xhscdn.com/cover.jpg!x" />'
            '<meta property="og:video" content="https://sns-video-qc.xhscdn.com/v/a.mp4" />'
            "</head><body></body></html>"
        )
        result = parse_xhs_html(html, "https://xhslink.com/a/AbCdEf")
        self.assertEqual(result.kind, "video")
        self.assertEqual(result.video_url, "https://sns-video-qc.xhscdn.com/v/a.mp4")


class XhsResolveChainTests(unittest.TestCase):
    """解析链接线：_try_resolve_xhs_post 图文/视频/失败 三类结果。"""

    def setUp(self) -> None:
        self.executor = _DummyExecutor()

    async def _resolve(self, result: XhsResult) -> object:
        with patch("core.tools_video.fetch_xhs_post", AsyncMock(return_value=result)):
            return await self.executor._try_resolve_xhs_post(
                url="https://www.xiaohongshu.com/explore/64abc",
                query="看看这个小红书",
                method_name="parse_video",
            )

    def test_non_xhs_url_should_return_none(self) -> None:
        with patch("core.tools_video.fetch_xhs_post", AsyncMock()) as mocked:
            result = asyncio.run(
                self.executor._try_resolve_xhs_post(
                    url="https://www.bilibili.com/video/BV1xx411c7mD",
                    query="",
                    method_name="parse_video",
                )
            )
            self.assertIsNone(result)
            mocked.assert_not_awaited()

    def test_image_post_should_return_image_mode_with_all_images(self) -> None:
        result = asyncio.run(
            self._resolve(
                XhsResult(
                    kind="image",
                    title="探店",
                    uploader="博主",
                    image_urls=[
                        "http://127.0.0.1:9/1.jpg",
                        "http://127.0.0.1:9/2.jpg",
                    ],
                    source_url="https://www.xiaohongshu.com/explore/64abc",
                )
            )
        )
        self.assertTrue(result.ok)
        payload = result.payload or {}
        self.assertEqual(payload.get("mode"), "image")
        self.assertEqual(payload.get("post_type"), "image_text")
        self.assertEqual(len(payload.get("image_urls") or []), 2)
        # 本地地址不可发 → 回退第一张作为 image_url
        self.assertEqual(payload.get("image_url"), "http://127.0.0.1:9/1.jpg")
        self.assertIn("共 2 张图", payload.get("text", ""))

    def test_video_post_should_return_video_mode(self) -> None:
        result = asyncio.run(
            self._resolve(
                XhsResult(
                    kind="video",
                    title="教程",
                    video_url="https://sns-video-qc.xhscdn.com/v/stream.m3u8",
                    source_url="https://www.xiaohongshu.com/explore/64abc",
                )
            )
        )
        self.assertTrue(result.ok)
        payload = result.payload or {}
        self.assertEqual(payload.get("mode"), "video")
        self.assertEqual(payload.get("video_url"), "https://sns-video-qc.xhscdn.com/v/stream.m3u8")

    def test_blocked_should_return_clear_error(self) -> None:
        result = asyncio.run(
            self._resolve(XhsResult(error="blocked", source_url="https://xhslink.com/a/AbCdEf"))
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "xhs_blocked")
        self.assertIn("反爬拦截", (result.payload or {}).get("text", ""))

    def test_no_data_should_return_clear_error(self) -> None:
        result = asyncio.run(
            self._resolve(XhsResult(error="no_data", source_url="https://xhslink.com/a/AbCdEf"))
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "xhs_no_data")

    def test_xhs_cookie_should_be_read_from_config(self) -> None:
        executor = _DummyExecutor(
            {"video_analysis": {"xiaohongshu": {"cookie": "a=1; b=2"}}}
        )
        self.assertEqual(executor._get_xhs_cookie(), "a=1; b=2")
        self.assertEqual(_DummyExecutor()._get_xhs_cookie(), "")

    def test_resolve_platform_video_should_return_xhs_video_url(self) -> None:
        with patch(
            "core.tools_video.fetch_xhs_post",
            AsyncMock(
                return_value=XhsResult(
                    kind="video",
                    video_url="https://sns-video-qc.xhscdn.com/v/stream.m3u8",
                    source_url="https://www.xiaohongshu.com/explore/64abc",
                )
            ),
        ):
            resolved = asyncio.run(
                self.executor._resolve_platform_video(
                    "https://www.xiaohongshu.com/explore/64abc"
                )
            )
        self.assertEqual(resolved, "https://sns-video-qc.xhscdn.com/v/stream.m3u8")

    def test_resolve_platform_video_should_mark_image_post_diagnostic(self) -> None:
        with patch(
            "core.tools_video.fetch_xhs_post",
            AsyncMock(
                return_value=XhsResult(
                    kind="image",
                    image_urls=["http://127.0.0.1:9/1.jpg"],
                    source_url="https://www.xiaohongshu.com/explore/64abc",
                )
            ),
        ):
            resolved = asyncio.run(
                self.executor._resolve_platform_video(
                    "https://www.xiaohongshu.com/explore/64abc"
                )
            )
        self.assertEqual(resolved, "")
        self.assertEqual(
            self.executor._last_video_resolve_diagnostic.get(
                "https://www.xiaohongshu.com/explore/64abc"
            ),
            "xhs_image_post_no_video",
        )


if __name__ == "__main__":
    unittest.main()
