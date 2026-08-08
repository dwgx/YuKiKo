"""回归测试：media_utils 纯函数搬移后行为与原来一致。

原先这些逻辑散落在 core/agent.py / core/engine.py 的 staticmethod 里，
D2 收敛把它们搬进 core/media_utils.py。本测试覆盖：
- 占位媒体 URL 检测（is_placeholder_media_url）
- 本地路径归一化（is_local_media_path / normalize_local_media_path）
- URL 提取（extract_first_url / strip_trailing_url_noise）
- 图片/视频 URL 判断（looks_like_image_url / looks_like_video_url）
"""

from __future__ import annotations

import unittest

from core import media_utils
from core.agent import AgentLoop


class PlaceholderMediaUrlTests(unittest.TestCase):
    def test_should_flag_example_dot_com_urls(self) -> None:
        for url in (
            "http://example.com/a.png",
            "https://example.org/b.jpg",
            "https://example.net/video.mp4",
        ):
            self.assertTrue(media_utils.is_placeholder_media_url(url), url)

    def test_should_flag_loopback_and_reserved_hosts(self) -> None:
        for url in (
            "http://localhost:8080/x.jpg",
            "http://127.0.0.1:8080/x.jpg",
            "http://0.0.0.0/x.png",
            "http://host.invalid/x.png",
        ):
            self.assertTrue(media_utils.is_placeholder_media_url(url), url)

    def test_should_accept_real_download_urls(self) -> None:
        for url in (
            "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc123",
            "https://cdn.example.io/img/photo.jpg",
            "https://gchat.qpic.cn/download?appid=1",
        ):
            self.assertFalse(media_utils.is_placeholder_media_url(url), url)

    def test_should_ignore_non_http_values(self) -> None:
        self.assertFalse(media_utils.is_placeholder_media_url(""))
        self.assertFalse(media_utils.is_placeholder_media_url("local/path/x.jpg"))
        self.assertFalse(media_utils.is_placeholder_media_url("data:image/png;base64,abc"))


class LocalMediaPathTests(unittest.TestCase):
    def test_should_detect_local_paths(self) -> None:
        self.assertTrue(media_utils.is_local_media_path("/tmp/a.jpg"))
        self.assertTrue(media_utils.is_local_media_path("C:\\Users\\x\\a.png"))
        self.assertTrue(media_utils.is_local_media_path("storage/media/v.mp4"))
        self.assertFalse(media_utils.is_local_media_path("https://x.com/a.jpg"))
        self.assertFalse(media_utils.is_local_media_path("http://x.com/a.jpg"))

    def test_should_normalize_local_paths(self) -> None:
        self.assertEqual(media_utils.normalize_local_media_path("C:\\Users\\X\\A.JPG"), "c:/users/x/a.jpg")
        self.assertEqual(media_utils.normalize_local_media_path("storage/media/v.MP4"), "storage/media/v.mp4")
        self.assertEqual(media_utils.normalize_local_media_path(""), "")
        self.assertEqual(media_utils.normalize_local_media_path("  "), "")
        self.assertEqual(media_utils.normalize_local_media_path("https://x.com/a.jpg"), "")

    def test_should_handle_none_path_inputs(self) -> None:
        self.assertEqual(media_utils.normalize_local_media_path("None"), "none")


class UrlExtractionTests(unittest.TestCase):
    def test_should_extract_first_url(self) -> None:
        self.assertEqual(
            media_utils.extract_first_url("看看 https://b23.tv/AbC123 这个视频"),
            "https://b23.tv/AbC123",
        )
        self.assertEqual(
            media_utils.extract_first_url("https://a.com/x 然后是 https://b.com/y"),
            "https://a.com/x",
        )
        self.assertEqual(media_utils.extract_first_url("没有链接"), "")
        self.assertEqual(media_utils.extract_first_url(""), "")
        self.assertEqual(media_utils.extract_first_url(None), "")

    def test_should_strip_trailing_noise(self) -> None:
        self.assertEqual(
            media_utils.strip_trailing_url_noise("https://a.com/x。"),
            "https://a.com/x",
        )
        self.assertEqual(
            media_utils.strip_trailing_url_noise("https://a.com/x，解析"),
            "https://a.com/x",
        )
        self.assertEqual(
            media_utils.strip_trailing_url_noise("https://a.com/x"),
            "https://a.com/x",
        )
        self.assertEqual(media_utils.strip_trailing_url_noise(""), "")


class UrlKindDetectionTests(unittest.TestCase):
    def test_should_detect_image_urls(self) -> None:
        self.assertTrue(media_utils.looks_like_image_url("https://a.com/x.png"))
        self.assertTrue(media_utils.looks_like_image_url("https://a.com/x.heic"))
        self.assertTrue(media_utils.looks_like_image_url("data:image/png;base64,abc"))
        self.assertTrue(
            media_utils.looks_like_image_url(
                "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=abc"
            )
        )
        self.assertFalse(media_utils.looks_like_image_url("https://a.com/x.mp4"))
        self.assertFalse(media_utils.looks_like_image_url(""))

    def test_should_detect_video_urls(self) -> None:
        self.assertTrue(media_utils.looks_like_video_url("https://a.com/x.mp4"))
        self.assertTrue(media_utils.looks_like_video_url("https://bilibili.com/video/BV1xx"))
        self.assertTrue(media_utils.looks_like_video_url("https://b23.tv/AbC123"))
        self.assertTrue(media_utils.looks_like_video_url("https://v.qq.com/x/page/abc.html"))
        self.assertFalse(media_utils.looks_like_video_url("https://a.com/x.jpg"))
        self.assertFalse(media_utils.looks_like_video_url(""))


class ForwarderConsistencyTests(unittest.TestCase):
    """搬移后，agent.py 上的薄转发 staticmethod 仍指向 media_utils，行为一致。"""

    def setUp(self) -> None:
        self.loop = AgentLoop.__new__(AgentLoop)

    def test_agent_forwarders_match_media_utils(self) -> None:
        self.assertTrue(self.loop._looks_like_image_url("https://a.com/x.png"))
        self.assertEqual(
            self.loop._extract_first_url("看 https://b23.tv/AbC123"),
            "https://b23.tv/AbC123",
        )
        self.assertTrue(self.loop._is_placeholder_media_url("http://example.com/a.png"))
        self.assertTrue(self.loop._is_local_media_path("/tmp/a.jpg"))
        self.assertEqual(
            self.loop._normalize_local_media_path("C:\\Users\\X\\A.JPG"),
            "c:/users/x/a.jpg",
        )
        self.assertTrue(self.loop._looks_like_video_url("https://a.com/x.mp4"))

    def test_engine_placeholder_forwarder_matches_media_utils(self) -> None:
        from core.engine import YukikoEngine

        engine = YukikoEngine.__new__(YukikoEngine)
        self.assertTrue(engine._is_placeholder_media_url("http://example.com/a.png"))
        self.assertFalse(
            engine._is_placeholder_media_url(
                "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=abc"
            )
        )
