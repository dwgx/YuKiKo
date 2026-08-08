"""蓝图 §9 协议残余回归测试（P9）。

三条残余（docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §9）：
1. QQ CDN 图片 URL 约 2h 过期 —— is_qq_image_url_expired 检测 +
   refresh_expired_image_url 尽力刷新（get_msg / get_image 通道）。
2. get_group_file_url 参数归一 —— normalize_napcat_api_kwargs 把早期 NapCat
   的 `group` 拼写归一成现版 schema 的 `group_id`。
3. 入站段转换 —— mface/marketface 跳过不抛异常；reply 段保留 message_id。
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import AsyncMock

from core.napcat_compat import (
    IMAGE_URL_TTL_SECONDS,
    extract_image_url_from_msg_payload,
    is_qq_image_url_expired,
    normalize_napcat_api_kwargs,
    parse_image_url_expiry_ts,
    refresh_expired_image_url,
)
from core.platform.components import MessageChain, Reply


class ImageUrlExpiryDetectionTests(unittest.TestCase):
    """QQ CDN 图片 URL 的 `t=` 时间戳解析与过期判定（蓝图 §9.4 / §9.7-5）。"""

    def test_parse_image_url_expiry_ts_extracts_epoch_seconds(self) -> None:
        url = "https://p.qpic.cn/group/abc/0?t=1712345678&term=2"
        self.assertEqual(parse_image_url_expiry_ts(url), 1712345678)

    def test_parse_image_url_expiry_ts_accepts_hex_timestamp(self) -> None:
        url = "https://gchat.qpic.cn/gchatpic_new/1/2/3-0.jpg?t=5f1d2c3a"
        self.assertEqual(parse_image_url_expiry_ts(url), int("5f1d2c3a", 16))

    def test_parse_image_url_expiry_ts_returns_none_without_timestamp(self) -> None:
        self.assertIsNone(parse_image_url_expiry_ts("https://example.com/a.png"))
        self.assertIsNone(parse_image_url_expiry_ts("https://p.qpic.cn/x/0?term=2"))
        self.assertIsNone(parse_image_url_expiry_ts("https://p.qpic.cn/x/0?t=zzz"))

    def test_is_expired_true_when_older_than_ttl(self) -> None:
        now = int(time.time())
        url = f"https://p.qpic.cn/x/0?t={now - IMAGE_URL_TTL_SECONDS - 60}"
        self.assertTrue(is_qq_image_url_expired(url, now=now))

    def test_is_expired_false_when_within_ttl(self) -> None:
        now = int(time.time())
        url = f"https://p.qpic.cn/x/0?t={now - 1800}"
        self.assertFalse(is_qq_image_url_expired(url, now=now))

    def test_is_expired_false_without_timestamp(self) -> None:
        """没有 t= 时间戳的 URL 判未过期 —— 宁可漏报也不误伤普通直链。"""
        self.assertFalse(is_qq_image_url_expired("https://example.com/a.png"))
        self.assertFalse(is_qq_image_url_expired(""))

    def test_is_expired_respects_custom_ttl(self) -> None:
        now = int(time.time())
        url = f"https://p.qpic.cn/x/0?t={now - 3600}"
        self.assertTrue(is_qq_image_url_expired(url, now=now, ttl_seconds=1800))
        self.assertFalse(is_qq_image_url_expired(url, now=now, ttl_seconds=7200))


class ImageUrlRefreshTests(unittest.IsolatedAsyncioTestCase):
    """refresh_expired_image_url 的 get_msg / get_image 刷新通道。"""

    def _get_msg_payload(self, image_url: str) -> dict:
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "message_id": "10001",
                "message": [
                    {"type": "text", "data": {"text": "看这张图"}},
                    {"type": "image", "data": {"file": "file://abcd", "url": image_url}},
                ],
            },
        }

    async def test_refresh_via_get_msg_returns_fresh_url(self) -> None:
        api_call = AsyncMock(return_value=self._get_msg_payload("https://p.qpic.cn/fresh/0?t=9999999999"))
        result = await refresh_expired_image_url(
            "https://p.qpic.cn/stale/0?t=1",
            api_call,
            message_id="10001",
        )
        self.assertEqual(result, "https://p.qpic.cn/fresh/0?t=9999999999")
        api_call.assert_awaited_once_with("get_msg", message_id="10001")

    async def test_refresh_swallows_get_msg_error(self) -> None:
        async def failing_api(api: str, **kwargs):
            raise RuntimeError("ws disconnected")

        result = await refresh_expired_image_url("https://p.qpic.cn/stale/0?t=1", failing_api, message_id="10001")
        self.assertEqual(result, "https://p.qpic.cn/stale/0?t=1")

    async def test_refresh_without_message_id_returns_original(self) -> None:
        url = "https://p.qpic.cn/stale/0?t=1"
        result = await refresh_expired_image_url(url, AsyncMock())
        self.assertEqual(result, url)

    async def test_refresh_without_api_call_returns_original(self) -> None:
        url = "https://p.qpic.cn/stale/0?t=1"
        result = await refresh_expired_image_url(url, None, message_id="10001")
        self.assertEqual(result, url)

    async def test_refresh_file_uri_via_get_image_returns_local_path(self) -> None:
        api_call = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"file": "/tmp/xx.png"}})
        result = await refresh_expired_image_url("file://1234567890", api_call)
        self.assertEqual(result, "/tmp/xx.png")
        api_call.assert_awaited_once_with("get_image", file="file://1234567890")

    async def test_get_image_error_returns_original(self) -> None:
        async def failing_api(api: str, **kwargs):
            raise RuntimeError("boom")

        result = await refresh_expired_image_url("file://1234567890", failing_api)
        self.assertEqual(result, "file://1234567890")

    def test_extract_image_url_from_msg_payload_handles_shapes(self) -> None:
        payload = {
            "data": {
                "message": [
                    {"type": "text", "data": {"text": "hi"}},
                    {"type": "image", "data": {"file": "file://x", "url": "https://p.qpic.cn/a/0?t=9"}},
                ]
            }
        }
        self.assertEqual(extract_image_url_from_msg_payload(payload), "https://p.qpic.cn/a/0?t=9")
        self.assertEqual(extract_image_url_from_msg_payload({"data": {"message": []}}), "")
        self.assertEqual(extract_image_url_from_msg_payload(None), "")

    def test_extract_image_url_falls_back_to_http_file_field(self) -> None:
        payload = {
            "message": [
                {"type": "image", "data": {"file": "https://cdn.example.com/x.png"}},
            ]
        }
        self.assertEqual(extract_image_url_from_msg_payload(payload), "https://cdn.example.com/x.png")


class GetGroupFileUrlParamNormalizationTests(unittest.TestCase):
    """get_group_file_url 参数归一（蓝图 §9.5）：group → group_id。"""

    def test_group_normalized_to_group_id(self) -> None:
        payload = normalize_napcat_api_kwargs("get_group_file_url", {"group": "123456", "file_id": "abc123"})
        self.assertEqual(payload, {"group_id": "123456", "file_id": "abc123"})

    def test_integer_group_becomes_string_group_id(self) -> None:
        payload = normalize_napcat_api_kwargs(
            "get_group_file_url", {"group": 123456, "file_id": "abc123", "busid": 102}
        )
        self.assertEqual(payload, {"group_id": "123456", "file_id": "abc123", "busid": 102})

    def test_explicit_group_id_wins_over_alias(self) -> None:
        payload = normalize_napcat_api_kwargs(
            "get_group_file_url",
            {"group": "111111", "group_id": "222222", "file_id": "abc"},
        )
        self.assertEqual(payload, {"group_id": "222222", "file_id": "abc"})

    def test_other_apis_pass_group_key_through_untouched(self) -> None:
        payload = normalize_napcat_api_kwargs("send_group_msg", {"group": "x", "message": "hi"})
        self.assertEqual(payload, {"group": "x", "message": "hi"})


class InboundSegmentConversionTests(unittest.TestCase):
    """入站段转换：mface 跳过 + reply 保留 message_id（蓝图 §4.4（2））。"""

    def test_mface_and_marketface_are_skipped_without_exception(self) -> None:
        chain = MessageChain.from_onebot_segments(
            [
                {"type": "text", "data": {"text": "hello"}},
                {"type": "mface", "data": {"emoji_id": "1", "key": "sticker"}},
                {"type": "marketface", "data": {"id": "2"}},
                {"type": "mface"},  # 无 data 字段的极端形态也不能炸
                {"type": "text", "data": {"text": " world"}},
            ]
        )
        self.assertEqual(chain.get_plain_text(), "hello world")
        self.assertEqual(len(chain.components), 2)

    def test_reply_segment_keeps_message_id(self) -> None:
        chain = MessageChain.from_onebot_segments([{"type": "reply", "data": {"id": "12345", "user_id": 777}}])
        self.assertEqual(len(chain.components), 1)
        reply = chain.components[0]
        self.assertIsInstance(reply, Reply)
        self.assertEqual(reply.message_id, "12345")

    def test_reply_segment_accepts_message_id_key_fallback(self) -> None:
        chain = MessageChain.from_onebot_segments([{"type": "reply", "data": {"message_id": "98765"}}])
        self.assertEqual(chain.components[0].message_id, "98765")

    def test_reply_roundtrip_via_to_onebot_segments(self) -> None:
        chain = MessageChain.from_onebot_segments(
            [{"type": "reply", "data": {"id": "42"}}, {"type": "text", "data": {"text": "hi"}}]
        )
        segments = chain.to_onebot_segments()
        self.assertEqual(segments[0], {"type": "reply", "data": {"id": "42"}})


if __name__ == "__main__":
    unittest.main()
