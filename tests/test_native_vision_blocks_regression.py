"""回归：原生看图的 image_url 块只能是 data URI，且张数/字节/去重都有硬上限。

背景（实测）：`agent.vision_enabled` 恒为 False（两处真相源都没这个键），
所以模型从来没原生看过图。而打开它的那段朴素实现是把 OneBot image 段里的
`data.url` 原样塞进 `image_url` 块 —— 那是 QQ CDN 链接，对外不可达：

    HTTP 400 {"retcode":-5503022,"retmsg":"appid is not supported"}
    HTTP 400 {"retcode":-5503007,"retmsg":"download url has expired"}

日志侧的比分同样清楚：`source=onebot_get_image` 45/45 成功、
`source=onebot_local_file` 61/61 成功、`source=http_url` 直连 CDN 50/50 全败。
所以块里出现 http 链接，等于让模型收到死链之后瞎猜。

本文件里前两组用例在基线上是**断言失败**（拿到 http 链接）而不是
AttributeError —— 见 `_blocks_from` 的基线兜底。
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from core.tools import ToolExecutor

_LOGGER_NAME = "yukiko.tools"

# 最小合法 PNG（1x1），用来喂过 _is_known_image_signature。
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
    "q842iQAAAABJRU5ErkJggg=="
)

_QQ_CDN_URL = (
    "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=EhRC7DhvSTaMXbQ"
    "vz9C44wgCEOQ-RxiX_S4g_woolbj13pWJlgMyBHByb2Q&rkey=CAQSKAB6JWENi5LM_xp9vum"
)
_QQ_EXPIRED_BODY = b'{"retcode":-5503007,"retmsg":"download url has expired"}'


def _png_of_size(total_bytes: int) -> bytes:
    """造一张指定字节数的「PNG」：真签名 + 尾部填充。

    只有前 16 字节参与签名判定，填充不影响 `_is_known_image_signature`。
    """

    if total_bytes <= len(_PNG_1PX):
        return _PNG_1PX
    return _PNG_1PX + b"\x00" * (total_bytes - len(_PNG_1PX))


def _image_segment(
    *,
    url: str = "",
    file_token: str = "",
    path: str = "",
    memory_data_uri: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if url:
        data["url"] = url
    if file_token:
        data["file"] = file_token
    if path:
        data["path"] = path
    if memory_data_uri:
        data["memory_data_uri"] = memory_data_uri
    return {"type": "image", "data": data}


class _FakeAsyncClient:
    """只实现 _download_image_as_data_uri 用到的那部分 httpx.AsyncClient。"""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        _ = (exc_type, exc, tb)
        return False

    async def get(self, url: str) -> httpx.Response:
        _ = url
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _response(
    status_code: int, content: bytes, *, content_type: str = "image/png"
) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", _QQ_CDN_URL),
    )


def _patch_client(response: httpx.Response | Exception):
    return patch(
        "core.tools_vision.httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(response),
    )


def _make_executor(
    *,
    vision_enable: bool = True,
    safe_url: bool = True,
    provider: str = "",
    min_bytes: int = 8,
    max_bytes: int = 6 * 1024 * 1024,
    raw_config: dict[str, Any] | None = None,
) -> ToolExecutor:
    """真构造器要模型和网络，按仓库惯例手工装配所需属性。"""

    executor = ToolExecutor.__new__(ToolExecutor)
    executor._vision_enable = vision_enable
    executor._vision_min_image_bytes = min_bytes
    executor._vision_max_image_bytes = max_bytes
    executor._vision_provider = provider
    executor.image_engine = None
    executor._project_root = Path(tempfile.gettempdir())
    executor._raw_config = raw_config if raw_config is not None else {}
    executor._is_safe_public_http_url = lambda _url: safe_url  # type: ignore[method-assign]
    return executor


def _shipped_naive_blocks(
    raw_segments: list[dict[str, Any]] | None,
    reply_media_segments: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """基线上线的那段注入逻辑（HEAD 的 core/agent.py:3565 起），逐行照抄。

    它把 image 段的 `data.url` 原样塞进块里、硬编码 4 张、不看字节数。
    保留它是为了让本文件的断言在基线上**因为行为不对而红**（拿到 http 链接、
    没有张数/字节把关），而不是只因为新方法不存在报 AttributeError。
    """

    blocks: list[dict[str, Any]] = []
    for group in (raw_segments or [], reply_media_segments or []):
        for seg in group:
            if isinstance(seg, dict) and seg.get("type") == "image":
                url = (seg.get("data") or {}).get("url", "")
                if url and url.startswith("http"):
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for block in blocks:
        url = block["image_url"]["url"]
        if url not in seen:
            seen.add(url)
            unique.append(block)
            if len(unique) >= 4:
                break
    return unique


async def _blocks_from(
    executor: ToolExecutor,
    *,
    raw_segments: list[dict[str, Any]] | None = None,
    reply_media_segments: list[dict[str, Any]] | None = None,
    api_call: Any = None,
    max_images: int = 0,
) -> tuple[list[dict[str, Any]], str]:
    """走新方法；新方法不存在时退回基线上线的那段注入逻辑。"""

    builder = getattr(executor, "build_native_vision_blocks", None)
    if builder is None:
        return _shipped_naive_blocks(raw_segments, reply_media_segments), ""
    return await builder(
        raw_segments=raw_segments,
        reply_media_segments=reply_media_segments,
        api_call=api_call,
        max_images=max_images,
    )


def _urls_of(blocks: list[dict[str, Any]]) -> list[str]:
    return [str(block["image_url"]["url"]) for block in blocks]


class BlocksNeverCarryUnreachableCdnLinkTests(unittest.IsolatedAsyncioTestCase):
    """块里只能出现 data URI —— QQ CDN 直链外部 API 打不开。"""

    async def test_dead_cdn_segment_yields_no_block_instead_of_dead_link(self) -> None:
        executor = _make_executor()
        segments = [_image_segment(url=_QQ_CDN_URL)]
        with _patch_client(
            _response(400, _QQ_EXPIRED_BODY, content_type="application/json")
        ):
            blocks, reason = await _blocks_from(executor, raw_segments=segments)
        self.assertEqual(
            _urls_of(blocks),
            [],
            "失效 CDN 链接不该变成 image_url 块 —— 模型只会收到死链",
        )
        self.assertEqual(reason, "all_conversions_failed")

    async def test_transient_download_failure_does_not_leak_raw_url(self) -> None:
        """5xx 时 `_prepare_vision_image_ref` 会回退直传原 URL，这里必须挡住。"""

        executor = _make_executor()
        segments = [_image_segment(url=_QQ_CDN_URL)]
        with _patch_client(_response(503, b"upstream down", content_type="text/plain")):
            blocks, reason = await _blocks_from(executor, raw_segments=segments)
        self.assertEqual(_urls_of(blocks), [])
        self.assertEqual(reason, "all_conversions_failed")

    async def test_shipped_helper_documents_the_direct_url_fallback(self) -> None:
        """特征化：`_prepare_vision_image_ref` 在中性 provider + 5xx 下确实回传原 URL。

        这条在改动前后都绿 —— 它的作用是钉住上面两条断言防的是什么。
        """

        executor = _make_executor()
        with _patch_client(_response(503, b"upstream down", content_type="text/plain")):
            prepared = await executor._prepare_vision_image_ref(_QQ_CDN_URL)
        self.assertEqual(prepared, _QQ_CDN_URL)
        self.assertFalse(prepared.startswith("data:image"))

    async def test_direct_url_rejection_is_logged_with_reason(self) -> None:
        executor = _make_executor()
        segments = [_image_segment(url=_QQ_CDN_URL)]
        with _patch_client(_response(503, b"nope", content_type="text/plain")):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                await executor.build_native_vision_blocks(raw_segments=segments)
        joined = "\n".join(captured.output)
        self.assertIn("native_vision_block_reject", joined)
        self.assertIn("reason=direct_url_not_accepted", joined)
        self.assertIn("native_vision_blocks_empty", joined)


class LocalAndNapCatPathsArePreferredTests(unittest.IsolatedAsyncioTestCase):
    """实测成功率顺序：local_file 61/61、onebot_get_image 45/45、http_url 0/50。"""

    async def test_local_file_segment_becomes_data_uri_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "shot.png"
            image_path.write_bytes(_png_of_size(4096))
            executor = _make_executor()
            segments = [_image_segment(path=str(image_path), url=_QQ_CDN_URL)]
            # 任何 HTTP 调用都会炸 —— 证明本地文件那条路根本没碰网络。
            with _patch_client(AssertionError("本地文件路径不该走 HTTP")):
                blocks, reason = await _blocks_from(executor, raw_segments=segments)
        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 1)
        self.assertTrue(_urls_of(blocks)[0].startswith("data:image/png;base64,"))

    async def test_napcat_get_image_is_used_when_only_file_token_present(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "cached.png"
            image_path.write_bytes(_png_of_size(2048))

            async def _api_call(action: str, **kwargs: Any) -> dict[str, Any]:
                calls.append((action, kwargs))
                return {"data": {"file": str(image_path)}}

            executor = _make_executor()
            segments = [_image_segment(file_token="ABCDEF0123.png")]
            blocks, reason = await _blocks_from(
                executor, raw_segments=segments, api_call=_api_call
            )

        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 1)
        self.assertTrue(_urls_of(blocks)[0].startswith("data:image/png;base64,"))
        self.assertEqual([action for action, _ in calls], ["get_image"])

    async def test_napcat_path_recovers_a_segment_whose_cdn_url_is_dead(self) -> None:
        """线上最常见的形状：url 是死链、file token 能救回来。"""

        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "recovered.png"
            image_path.write_bytes(_png_of_size(3000))

            async def _api_call(action: str, **kwargs: Any) -> dict[str, Any]:
                _ = (action, kwargs)
                return {"data": {"file": str(image_path)}}

            executor = _make_executor()
            segments = [
                _image_segment(url=_QQ_CDN_URL, file_token="DEADBEEF9999.png")
            ]
            with _patch_client(
                _response(400, _QQ_EXPIRED_BODY, content_type="application/json")
            ):
                blocks, reason = await _blocks_from(
                    executor, raw_segments=segments, api_call=_api_call
                )

        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 1)
        self.assertTrue(_urls_of(blocks)[0].startswith("data:image"))

    async def test_cached_memory_data_uri_is_reused_without_network(self) -> None:
        b64 = base64.b64encode(_png_of_size(2048)).decode("ascii")
        executor = _make_executor()
        segments = [
            _image_segment(
                memory_data_uri=f"data:image/png;base64,{b64}", url=_QQ_CDN_URL
            )
        ]
        with _patch_client(AssertionError("已有 data URI 时不该再下载")):
            blocks, reason = await _blocks_from(executor, raw_segments=segments)
        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 1)
        self.assertTrue(_urls_of(blocks)[0].startswith("data:image/png;base64,"))

    async def test_public_http_image_still_works_when_download_succeeds(self) -> None:
        executor = _make_executor()
        segments = [_image_segment(url="https://img.example.com/cat.png")]
        with _patch_client(_response(200, _png_of_size(2048))):
            blocks, reason = await _blocks_from(executor, raw_segments=segments)
        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 1)
        self.assertTrue(_urls_of(blocks)[0].startswith("data:image/png;base64,"))


def _local_segments(tmp: str, count: int, size: int = 2048) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for idx in range(count):
        path = Path(tmp) / f"img{idx}.png"
        # 每张内容不同，否则会被内容去重合并成一张。
        path.write_bytes(_png_of_size(size) + bytes([idx % 251]) * 16)
        segments.append(_image_segment(path=str(path)))
    return segments


class ImageCountAndByteLimitsAreEnforcedTests(unittest.IsolatedAsyncioTestCase):
    """data URI 会显著放大请求体，张数和单图字节都必须封顶。"""

    async def test_more_images_than_the_cap_are_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = _make_executor()
            segments = _local_segments(tmp, 7)
            blocks, reason = await _blocks_from(executor, raw_segments=segments)
        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 4, "默认张数上限是 4")

    async def test_configured_max_images_overrides_the_builtin_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = _make_executor(
                raw_config={"search": {"vision": {"native_max_images": 2}}}
            )
            segments = _local_segments(tmp, 5)
            blocks, _ = await _blocks_from(executor, raw_segments=segments)
        self.assertEqual(len(blocks), 2)

    async def test_caller_argument_can_lower_but_not_raise_the_configured_cap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = _make_executor(
                raw_config={"search": {"vision": {"native_max_images": 3}}}
            )
            segments = _local_segments(tmp, 6)
            lowered, _ = await _blocks_from(
                executor, raw_segments=segments, max_images=1
            )
            raised, _ = await _blocks_from(
                executor, raw_segments=segments, max_images=99
            )
        self.assertEqual(len(lowered), 1)
        self.assertEqual(len(raised), 3, "调用方不能突破配置上限")

    async def test_oversized_image_is_skipped_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.png"
            big.write_bytes(_png_of_size(600 * 1024))
            executor = _make_executor()
            segments = [_image_segment(path=str(big))]
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                blocks, reason = await _blocks_from(executor, raw_segments=segments)
        self.assertEqual(blocks, [], "超过单图上限的图不进上下文")
        self.assertEqual(reason, "all_conversions_failed")
        joined = "\n".join(captured.output)
        self.assertIn("reason=too_large_for_native", joined)
        self.assertIn(f"limit={512 * 1024}", joined)

    async def test_configured_byte_cap_lets_a_bigger_image_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.png"
            big.write_bytes(_png_of_size(600 * 1024))
            executor = _make_executor(
                raw_config={
                    "search": {"vision": {"native_max_image_bytes": 1024 * 1024}}
                }
            )
            blocks, reason = await _blocks_from(
                executor, raw_segments=[_image_segment(path=str(big))]
            )
        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 1)

    async def test_absurd_config_values_are_clamped(self) -> None:
        executor = _make_executor(
            raw_config={
                "search": {
                    "vision": {
                        "native_max_images": 0,
                        "native_max_image_bytes": 512 * 1024 * 1024,
                    }
                }
            }
        )
        enabled, max_images, max_bytes = executor._native_vision_limits()
        self.assertTrue(enabled)
        self.assertEqual(max_images, 1)
        self.assertEqual(max_bytes, 4 * 1024 * 1024)

    async def test_garbage_config_values_fall_back_to_defaults(self) -> None:
        executor = _make_executor(
            raw_config={
                "search": {
                    "vision": {
                        "native_max_images": "四张",
                        "native_max_image_bytes": None,
                    }
                }
            }
        )
        _, max_images, max_bytes = executor._native_vision_limits()
        self.assertEqual(max_images, 4)
        self.assertEqual(max_bytes, 512 * 1024)


class DuplicateImagesCollapseToOneBlockTests(unittest.IsolatedAsyncioTestCase):
    """同一张图在原消息和被引用消息里各出现一次是常态，不能算两张。"""

    async def test_same_file_token_in_reply_and_message_is_deduplicated(self) -> None:
        api_calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "same.png"
            image_path.write_bytes(_png_of_size(2048))

            async def _api_call(action: str, **kwargs: Any) -> dict[str, Any]:
                _ = kwargs
                api_calls.append(action)
                return {"data": {"file": str(image_path)}}

            executor = _make_executor()
            seg = _image_segment(file_token="SAME0001.png")
            blocks, reason = await _blocks_from(
                executor,
                raw_segments=[seg],
                reply_media_segments=[dict(seg)],
                api_call=_api_call,
            )

        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(api_calls), 1, "去重要发生在下载之前，省的是网络往返")

    async def test_identical_content_under_different_keys_collapses(self) -> None:
        """两个不同 file token 指向同一份图片内容时，仍只留一块。"""

        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.png"
            second = Path(tmp) / "b.png"
            payload = _png_of_size(2048)
            first.write_bytes(payload)
            second.write_bytes(payload)
            executor = _make_executor()
            blocks, reason = await _blocks_from(
                executor,
                raw_segments=[
                    _image_segment(path=str(first)),
                    _image_segment(path=str(second)),
                ],
            )
        self.assertEqual(reason, "")
        self.assertEqual(len(blocks), 1)


class DisabledAndEmptyCasesReportWhyTests(unittest.IsolatedAsyncioTestCase):
    """空列表必须带上可区分的原因码，调用方靠它决定退回纯文本。"""

    async def test_no_image_segment_reports_no_image_segments(self) -> None:
        executor = _make_executor()
        blocks, reason = await executor.build_native_vision_blocks(
            raw_segments=[{"type": "text", "data": {"text": "在吗"}}]
        )
        self.assertEqual(blocks, [])
        self.assertEqual(reason, "no_image_segments")

    async def test_vision_disabled_reports_vision_disabled(self) -> None:
        executor = _make_executor(vision_enable=False)
        blocks, reason = await executor.build_native_vision_blocks(
            raw_segments=[_image_segment(url=_QQ_CDN_URL)]
        )
        self.assertEqual(blocks, [])
        self.assertEqual(reason, "vision_disabled")

    async def test_native_blocks_switch_off_reports_its_own_reason(self) -> None:
        executor = _make_executor(
            raw_config={"search": {"vision": {"native_blocks_enable": False}}}
        )
        blocks, reason = await executor.build_native_vision_blocks(
            raw_segments=[_image_segment(url=_QQ_CDN_URL)]
        )
        self.assertEqual(blocks, [])
        self.assertEqual(reason, "native_blocks_disabled")

    async def test_missing_api_call_is_recorded_in_the_failure_reason(self) -> None:
        executor = _make_executor()
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
            blocks, reason = await executor.build_native_vision_blocks(
                raw_segments=[_image_segment(file_token="TOKENONLY.png")],
                api_call=None,
            )
        self.assertEqual(blocks, [])
        self.assertEqual(reason, "all_conversions_failed")
        joined = "\n".join(captured.output)
        self.assertIn("native_vision_block_failed", joined)
        self.assertIn("onebot_get_image:no_api_call", joined)

    async def test_segment_without_any_usable_field_is_reported(self) -> None:
        executor = _make_executor()
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
            blocks, reason = await executor.build_native_vision_blocks(
                raw_segments=[{"type": "image", "data": {}}]
            )
        self.assertEqual(blocks, [])
        self.assertEqual(reason, "all_conversions_failed")
        self.assertIn("no_usable_field", "\n".join(captured.output))

    async def test_ready_log_carries_token_estimate_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executor = _make_executor()
            segments = _local_segments(tmp, 2)
            with self.assertLogs(_LOGGER_NAME, level="INFO") as captured:
                blocks, _ = await executor.build_native_vision_blocks(
                    raw_segments=segments
                )
        self.assertEqual(len(blocks), 2)
        joined = "\n".join(captured.output)
        self.assertIn("native_vision_blocks_ready", joined)
        self.assertIn("est_tokens=", joined)
        self.assertIn(f"max_image_bytes={512 * 1024}", joined)


class TokenEstimateTracksBase64LengthTests(unittest.TestCase):
    """token ≈ base64 长度 / 4 —— 供调用方做预算判断。"""

    def test_estimate_scales_with_payload_size(self) -> None:
        small = base64.b64encode(_png_of_size(3 * 1024)).decode("ascii")
        large = base64.b64encode(_png_of_size(30 * 1024)).decode("ascii")
        one = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{small}"}}]
        two = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{large}"}}]
        est_small = ToolExecutor.estimate_native_vision_tokens(one)
        est_large = ToolExecutor.estimate_native_vision_tokens(two)
        self.assertEqual(est_small, len(small) // 4)
        self.assertEqual(est_large, len(large) // 4)
        self.assertGreater(est_large, est_small * 5)

    def test_estimate_sums_over_blocks_and_ignores_text_blocks(self) -> None:
        b64 = base64.b64encode(_png_of_size(4096)).decode("ascii")
        image_block = {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        }
        blocks = [{"type": "text", "text": "看看这个"}, image_block, dict(image_block)]
        self.assertEqual(
            ToolExecutor.estimate_native_vision_tokens(blocks),
            2 * (len(b64) // 4),
        )

    def test_empty_input_estimates_zero(self) -> None:
        self.assertEqual(ToolExecutor.estimate_native_vision_tokens([]), 0)

    def test_payload_bytes_matches_the_real_decoded_length(self) -> None:
        for size in (1024, 4095, 4096, 4097, 65537):
            payload = _png_of_size(size)
            b64 = base64.b64encode(payload).decode("ascii")
            self.assertEqual(
                ToolExecutor._data_uri_payload_bytes(f"data:image/png;base64,{b64}"),
                len(payload),
            )


if __name__ == "__main__":
    unittest.main()
