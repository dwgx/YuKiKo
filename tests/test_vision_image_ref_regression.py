"""回归：图片下载失败必须留下可定位的原因，且失效链接不再回退直传。

背景（实测）：QQ CDN 的图片链接带 rkey，失效后返回
`HTTP 400 {"retcode":-5503007,"retmsg":"download url has expired"}`。
`_download_image_as_data_uri` 原先有四条静默 `return ""`（SSRF 拦截、非 200、
空响应体、超限），线上只能看到最外层的 `vision_image_ref_empty`，
25 次失败里没有一条能说明是哪一步坏的 —— 这个 bug 就是这么活下来的。
"""

from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

import httpx
from core.tools import ToolExecutor

_LOGGER_NAME = "yukiko.tools"

# 最小合法 PNG（1x1），用于喂过 _is_known_image_signature。
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
    "q842iQAAAABJRU5ErkJggg=="
)

_QQ_EXPIRED_BODY = b'{"retcode":-5503007,"retmsg":"download url has expired","retryflag":0}'
_QQ_URL = (
    "https://gchat.qpic.cn/download?appid=1407&fileid=EhRC7DhvSTaMXbQvz9C44wgCEOQ"
    "-RxiX_S4g_woolbj13pWJlgMyBHByb2RQgL2jAVoQjL4xQutb7SELnESCmEkHq3oCwqKCAQJuag"
    "&rkey=CAQSKAB6JWENi5LM_xp9vumLbuThJSaYf-yzMrbZsuq7Uz2qffcqm614gds&spec=0"
)


def _response(
    status_code: int,
    content: bytes,
    *,
    content_type: str = "image/png",
    url: str = _QQ_URL,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


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


def _make_executor(
    *,
    safe_url: bool = True,
    provider: str = "",
    min_bytes: int = 128,
    max_bytes: int = 6 * 1024 * 1024,
) -> ToolExecutor:
    """真构造器要模型和网络，按仓库惯例手工装配所需属性。"""

    executor = ToolExecutor.__new__(ToolExecutor)
    executor._vision_min_image_bytes = min_bytes
    executor._vision_max_image_bytes = max_bytes
    executor._vision_provider = provider
    executor.image_engine = None
    executor._project_root = None
    executor._is_safe_public_http_url = lambda _url: safe_url  # type: ignore[method-assign]
    return executor


def _patch_client(response: httpx.Response | Exception):
    return patch(
        "core.tools_vision.httpx.AsyncClient",
        lambda *args, **kwargs: _FakeAsyncClient(response),
    )


class DownloadFailurePathsAreLoggedTests(unittest.IsolatedAsyncioTestCase):
    """四条原本静默的 return "" 各自必须给出可区分的 reason。"""

    async def test_ssrf_rejection_logs_reason(self) -> None:
        executor = _make_executor(safe_url=False)
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
            data_uri, reason = await executor._download_image_as_data_uri_detailed(
                "http://10.0.0.5/x.png"
            )
        self.assertEqual(data_uri, "")
        self.assertEqual(reason, "ssrf_blocked")
        joined = "\n".join(captured.output)
        self.assertIn("vision_image_download_fail", joined)
        self.assertIn("reason=ssrf_blocked", joined)
        self.assertIn("10.0.0.5", joined)

    async def test_qq_cdn_expired_url_logs_status_and_body(self) -> None:
        """实测形态：HTTP 400 + retcode -5503007，必须单独归类为 url_expired。"""

        executor = _make_executor()
        response = _response(400, _QQ_EXPIRED_BODY, content_type="application/json")
        with _patch_client(response):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                data_uri, reason = await executor._download_image_as_data_uri_detailed(
                    _QQ_URL
                )
        self.assertEqual(data_uri, "")
        self.assertEqual(reason, "url_expired")
        joined = "\n".join(captured.output)
        self.assertIn("reason=url_expired", joined)
        self.assertIn("status=400", joined)
        self.assertIn("download url has expired", joined)
        self.assertIn("application/json", joined)

    async def test_plain_non_200_logs_http_status(self) -> None:
        executor = _make_executor()
        with _patch_client(_response(503, b"upstream down", content_type="text/plain")):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                data_uri, reason = await executor._download_image_as_data_uri_detailed(
                    "https://cdn.example/x.png"
                )
        self.assertEqual(data_uri, "")
        self.assertEqual(reason, "http_503")
        joined = "\n".join(captured.output)
        self.assertIn("reason=http_503", joined)
        self.assertIn("status=503", joined)

    async def test_empty_body_logs_reason(self) -> None:
        executor = _make_executor()
        with _patch_client(_response(200, b"")):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                data_uri, reason = await executor._download_image_as_data_uri_detailed(
                    "https://cdn.example/x.png"
                )
        self.assertEqual(data_uri, "")
        self.assertEqual(reason, "empty_body")
        joined = "\n".join(captured.output)
        self.assertIn("reason=empty_body", joined)
        self.assertIn("status=200", joined)

    async def test_oversized_image_logs_bytes_and_limit(self) -> None:
        executor = _make_executor(max_bytes=256 * 1024)
        payload = _PNG_1PX + b"\x00" * (256 * 1024)
        with _patch_client(_response(200, payload)):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                data_uri, reason = await executor._download_image_as_data_uri_detailed(
                    "https://cdn.example/big.png"
                )
        self.assertEqual(data_uri, "")
        self.assertEqual(reason, "too_large")
        joined = "\n".join(captured.output)
        self.assertIn("reason=too_large", joined)
        self.assertIn(f"bytes={len(payload)}", joined)
        self.assertIn(f"limit={256 * 1024}", joined)

    async def test_exception_logs_type_and_message(self) -> None:
        executor = _make_executor()
        with _patch_client(httpx.ConnectTimeout("connect timed out")):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                data_uri, reason = await executor._download_image_as_data_uri_detailed(
                    "https://cdn.example/x.png"
                )
        self.assertEqual(data_uri, "")
        self.assertEqual(reason, "exception_ConnectTimeout")
        joined = "\n".join(captured.output)
        self.assertIn("reason=exception", joined)
        self.assertIn("exc=ConnectTimeout", joined)
        self.assertIn("connect timed out", joined)

    async def test_html_error_page_with_status_200_logs_signature_reject(self) -> None:
        """CDN 用 200 返回错误页时，旧代码在 _to_data_uri_from_image_bytes 里静默丢弃。"""

        executor = _make_executor(min_bytes=8)
        body = b"<html><body>403 forbidden</body></html>"
        with _patch_client(_response(200, body, content_type="text/html")):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                data_uri, reason = await executor._download_image_as_data_uri_detailed(
                    "https://cdn.example/x.png"
                )
        self.assertEqual(data_uri, "")
        self.assertEqual(reason, "encode_rejected")
        joined = "\n".join(captured.output)
        self.assertIn("vision_image_encode_reject", joined)
        self.assertIn("reason=not_image_signature", joined)
        self.assertIn("reason=encode_rejected", joined)

    async def test_successful_download_logs_no_warning(self) -> None:
        executor = _make_executor(min_bytes=8)
        with _patch_client(_response(200, _PNG_1PX)):
            with self.assertNoLogs(_LOGGER_NAME, level="WARNING"):
                data_uri, reason = await executor._download_image_as_data_uri_detailed(
                    "https://cdn.example/x.png"
                )
        self.assertEqual(reason, "")
        self.assertTrue(data_uri.startswith("data:image/png;base64,"))

    async def test_thin_wrapper_still_returns_plain_string(self) -> None:
        """`_download_image_as_data_uri` 的旧签名（只返回字符串）仍被其它调用点使用。"""

        executor = _make_executor(min_bytes=8)
        with _patch_client(_response(200, _PNG_1PX)):
            data_uri = await executor._download_image_as_data_uri(
                "https://cdn.example/x.png"
            )
        self.assertIsInstance(data_uri, str)
        self.assertTrue(data_uri.startswith("data:image/png;base64,"))


class EncodeRejectionLoggingTests(unittest.TestCase):
    """`_to_data_uri_from_image_bytes` 的三条静默 return "" 也要有日志。"""

    def test_empty_bytes_logs_reason(self) -> None:
        executor = _make_executor()
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
            self.assertEqual(
                executor._to_data_uri_from_image_bytes(b"", source="unit"), ""
            )
        self.assertIn("reason=empty_bytes", "\n".join(captured.output))

    def test_too_small_logs_size_range(self) -> None:
        executor = _make_executor(min_bytes=4096)
        with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
            self.assertEqual(
                executor._to_data_uri_from_image_bytes(_PNG_1PX, source="unit"), ""
            )
        joined = "\n".join(captured.output)
        self.assertIn("reason=size_out_of_range", joined)
        self.assertIn("min=4096", joined)


class FailureCauseVisibleThroughPublicPathTests(unittest.IsolatedAsyncioTestCase):
    """同样的失败路径，走改动前就存在的入口 `_prepare_vision_image_ref`。

    这几条不依赖新方法名：改动前这里只会打一条笼统的
    `vision_image_ref_empty | reason=download_failed_for_provider_skiapi`，
    具体原因（状态码 / 字节数 / 异常类型）一个字都没有。
    """

    async def _prepare(self, response: httpx.Response | Exception, **kwargs) -> str:
        executor = _make_executor(provider="skiapi", **kwargs)
        with _patch_client(response):
            return await executor._prepare_vision_image_ref(_QQ_URL)

    async def test_expired_cdn_cause_is_visible(self) -> None:
        executor = _make_executor(provider="skiapi")
        response = _response(400, _QQ_EXPIRED_BODY, content_type="application/json")
        with _patch_client(response):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                self.assertEqual(await executor._prepare_vision_image_ref(_QQ_URL), "")
        joined = "\n".join(captured.output)
        self.assertIn("status=400", joined)
        self.assertIn("download url has expired", joined)

    async def test_empty_body_cause_is_visible(self) -> None:
        executor = _make_executor(provider="skiapi")
        with _patch_client(_response(200, b"")):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                self.assertEqual(await executor._prepare_vision_image_ref(_QQ_URL), "")
        self.assertIn("reason=empty_body", "\n".join(captured.output))

    async def test_oversized_cause_is_visible(self) -> None:
        executor = _make_executor(provider="skiapi", max_bytes=256 * 1024)
        payload = _PNG_1PX + b"\x00" * (256 * 1024)
        with _patch_client(_response(200, payload)):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                self.assertEqual(await executor._prepare_vision_image_ref(_QQ_URL), "")
        joined = "\n".join(captured.output)
        self.assertIn("reason=too_large", joined)
        self.assertIn(f"bytes={len(payload)}", joined)

    async def test_exception_cause_is_visible(self) -> None:
        executor = _make_executor(provider="skiapi")
        with _patch_client(httpx.ReadTimeout("read timed out")):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                self.assertEqual(await executor._prepare_vision_image_ref(_QQ_URL), "")
        joined = "\n".join(captured.output)
        self.assertIn("exc=ReadTimeout", joined)
        self.assertIn("read timed out", joined)


class DirectUrlFallbackTests(unittest.IsolatedAsyncioTestCase):
    """失效链接不能回退直传 —— 那只是让外部 API 去撞同一面 400 墙。"""

    async def test_expired_url_is_not_passed_through_for_neutral_provider(self) -> None:
        executor = _make_executor(provider="openai")
        response = _response(400, _QQ_EXPIRED_BODY, content_type="application/json")
        with _patch_client(response):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                prepared = await executor._prepare_vision_image_ref(_QQ_URL)
        self.assertEqual(prepared, "")
        joined = "\n".join(captured.output)
        self.assertIn("direct_url_fallback=skipped", joined)
        self.assertIn("reason=url_expired", joined)

    async def test_transient_failure_still_falls_back_to_direct_url(self) -> None:
        """5xx / 超时可能只是本机被挡，外部 API 仍有机会 —— 保持原回退。"""

        executor = _make_executor(provider="openai")
        with _patch_client(_response(503, b"nope", content_type="text/plain")):
            prepared = await executor._prepare_vision_image_ref(
                "https://cdn.example/x.png"
            )
        self.assertEqual(prepared, "https://cdn.example/x.png")

    async def test_provider_hint_branch_reports_the_underlying_cause(self) -> None:
        """skiapi/anthropic/gemini 分支原先只说 download_failed，不说为什么。"""

        executor = _make_executor(provider="skiapi")
        response = _response(400, _QQ_EXPIRED_BODY, content_type="application/json")
        with _patch_client(response):
            with self.assertLogs(_LOGGER_NAME, level="WARNING") as captured:
                prepared = await executor._prepare_vision_image_ref(_QQ_URL)
        self.assertEqual(prepared, "")
        joined = "\n".join(captured.output)
        self.assertIn("download_failed_for_provider_skiapi", joined)
        self.assertIn("cause=url_expired", joined)

    async def test_data_uri_input_passes_through_without_download(self) -> None:
        executor = _make_executor(min_bytes=8)
        b64 = base64.b64encode(_PNG_1PX).decode("ascii")
        prepared = await executor._prepare_vision_image_ref(
            f"data:image/png;base64,{b64}"
        )
        self.assertTrue(prepared.startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
