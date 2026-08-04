"""C1 回归 — QQ 凭证值不得进入 tool_result.data。

背景：`get_cookies` / `get_credentials` / `get_csrf_token` 原先走
`_handle_generic_napcat_api`，把整个 OneBot 响应塞进 `ToolCallResult.data`。
该 data 会被 `core/agent.py` 的 `_compact_data()` 原样喂回 LLM
（`core/agent.py:2033-2034`），模型随后可能用 final_answer 复述进群聊。

这组测试钉住的契约是：**凭证值不出 handler**。
任何人以后把值放回 data（例如把 handler 改回 `_handle_generic_napcat_api`、
或在摘要里"顺手"带上原串），这里必须红。
"""
from __future__ import annotations

import asyncio
import unittest
from typing import Any

from core.agent_tools_napcat import (
    _handle_napcat_credential_probe,
    _mask_credential_text,
    _register_napcat_extended_tools,
)
from core.agent_tools_registry import AgentToolRegistry

# 探针里用的假凭证值。任何一个出现在 data/display/error 里都算泄漏。
_SKEY = "xYzZzTop5ecretSkey"
_PSKEY = "pSkeyMustNeverLeak42"
_CSRF = 1854698342

_CREDENTIAL_TOOLS = ("get_cookies", "get_credentials", "get_csrf_token")


def _compact_data(data: dict[str, Any], max_items: int = 20) -> dict[str, Any]:
    """`core/agent.py:5341 _compact_data` 的逐行拷贝。

    该文件不属于本 wave 的可改范围，这里复制它的行为，
    以证明泄漏是在**经过压缩之后**仍然不存在的。
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, list):
            result[key] = value[:max_items]
            if len(value) > max_items:
                result[f"{key}_total"] = len(value)
        elif isinstance(value, str) and len(value) > 1000:
            result[key] = value[:1000] + "..."
        else:
            result[key] = value
    return result


class _RecordingApiCall:
    """记录上游调用，并返回指定 payload 或抛出指定异常。"""

    def __init__(self, payload: Any = None, exc: Exception | None = None):
        self.payload = payload
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, api: str, **kwargs: Any) -> Any:
        self.calls.append((api, dict(kwargs)))
        if self.exc is not None:
            raise self.exc
        return self.payload


def _call(tool: str, payload: Any, args: dict[str, Any] | None = None):
    registry = AgentToolRegistry()
    _register_napcat_extended_tools(registry)
    api = _RecordingApiCall(payload=payload)
    context = {"api_call": api, "permission_level": "user"}
    result = asyncio.run(registry.call(tool, dict(args or {}), context))
    return result, api


class NapcatCredentialRedactionRegressionTests(unittest.TestCase):
    def _assert_no_secret_anywhere(self, result, label: str) -> None:
        compacted = _compact_data(result.data) if isinstance(result.data, dict) else {}
        blob = f"{result.data}|{compacted}|{result.display}|{result.error}"
        for secret in (_SKEY, _PSKEY, str(_CSRF)):
            self.assertNotIn(
                secret,
                blob,
                f"{label}: 凭证值 {secret!r} 泄漏进了 tool_result 载荷",
            )

    def test_get_cookies_returns_presence_only_not_values(self):
        """get_cookies 只返回存在性摘要，不返回 cookie 值。"""
        payload = {
            "cookies": f"uin=o1234567890; skey={_SKEY}; p_skey={_PSKEY}; p_uin=o1234567890"
        }
        result, api = _call("get_cookies", payload, {"domain": "qzone.qq.com"})

        self.assertTrue(result.ok, result.error)
        self._assert_no_secret_anywhere(result, "get_cookies")
        # 上游仍被真实调用过 —— 脱敏不等于假装调用。
        self.assertEqual(len(api.calls), 1)
        # 存在性事实必须保留，否则这个工具就没有排障价值了。
        self.assertTrue(result.data["redacted"])
        self.assertTrue(result.data["cookies_present"])
        self.assertTrue(result.data["has_qzone_signing_key"])
        self.assertEqual(result.data["cookie_key_count"], 4)
        self.assertEqual(
            result.data["cookie_keys"], ["uin", "skey", "p_skey", "p_uin"]
        )
        self.assertEqual(result.data["domain"], "qzone.qq.com")

    def test_all_credential_tools_redact_values(self):
        """三个凭证工具在各种上游响应形状下都不得回传值。"""
        payloads: list[Any] = [
            {"cookies": f"skey={_SKEY}; p_skey={_PSKEY}", "token": _CSRF},
            # NapCat 有时把内容包在 data 里
            {"status": "ok", "data": {"cookies": f"p_skey={_PSKEY}", "token": _CSRF}},
            # cookie 也可能是 dict
            {"cookies": {"p_skey": _PSKEY, "skey": _SKEY}},
            {"token": _CSRF},
            {},
            None,
            "unexpected string body",
        ]
        for tool in _CREDENTIAL_TOOLS:
            for payload in payloads:
                with self.subTest(tool=tool, payload=payload):
                    result, _ = _call(tool, payload, {"domain": "qzone.qq.com"})
                    self.assertTrue(result.ok, result.error)
                    self._assert_no_secret_anywhere(result, tool)
                    self.assertIs(result.data["redacted"], True)

    def test_credential_tools_do_not_use_the_passthrough_handler(self):
        """注册表必须指向脱敏 handler，而不是原样透传的通用 handler。

        这一条是给未来的人看的：把 handler 改回 `_handle_generic_napcat_api`
        会立刻让本用例红，而不是等到线上把 cookie 说进群里才发现。
        """
        registry = AgentToolRegistry()
        _register_napcat_extended_tools(registry)
        for tool in _CREDENTIAL_TOOLS:
            with self.subTest(tool=tool):
                self.assertIs(
                    registry._handlers[tool],
                    _handle_napcat_credential_probe,
                    f"{tool} 必须使用脱敏 handler",
                )

    def test_credential_tool_descriptions_do_not_promise_values(self):
        """工具描述不能承诺返回凭证值，否则模型会据此复述。"""
        registry = AgentToolRegistry()
        _register_napcat_extended_tools(registry)
        for tool in _CREDENTIAL_TOOLS:
            with self.subTest(tool=tool):
                desc = registry._schemas[tool].description
                self.assertNotIn("获取QQ Cookies", desc)
                self.assertIn("不返回", desc)

    def test_missing_credentials_reported_as_absence_not_error(self):
        """上游没有凭证时要给出可排障的"未配置"结论，而不是空 data。"""
        result, _ = _call("get_cookies", {"cookies": ""}, {"domain": "qzone.qq.com"})
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.data["cookies_present"])
        self.assertFalse(result.data["has_qzone_signing_key"])
        self.assertEqual(result.data["cookie_keys"], [])
        self.assertIn("未配置", result.display)

    def test_expiry_is_surfaced_because_it_is_not_a_secret(self):
        """过期时间是排障事实，允许返回；navigator 的 qzone failure_policy 要用它。"""
        payload = {"cookies": f"p_skey={_PSKEY}", "expires_at": "2026-09-01T00:00:00Z"}
        result, _ = _call("get_cookies", payload, {"domain": "qzone.qq.com"})
        self.assertEqual(result.data["expires_at"], "2026-09-01T00:00:00Z")
        self._assert_no_secret_anywhere(result, "expiry case")

    def test_upstream_exception_body_is_masked(self):
        """上游把响应体塞进异常消息时，error 里的凭证也要脱敏。

        error 同样会被喂回 LLM（core/agent.py:2031-2032）。
        """
        registry = AgentToolRegistry()
        _register_napcat_extended_tools(registry)
        api = _RecordingApiCall(
            exc=RuntimeError(f"napcat 500 body={{'cookies': 'p_skey={_PSKEY}; skey={_SKEY}'}}")
        )
        result = asyncio.run(
            registry.call(
                "get_cookies",
                {"domain": "qzone.qq.com"},
                {"api_call": api, "permission_level": "user"},
            )
        )
        self.assertFalse(result.ok)
        self.assertIn("get_cookies", result.error)
        self._assert_no_secret_anywhere(result, "exception path")

    def test_mask_credential_text_keeps_key_names_and_drops_values(self):
        masked = _mask_credential_text(
            f"skey={_SKEY}; p_skey={_PSKEY}; csrf_token={_CSRF}; unrelated=keepme"
        )
        self.assertNotIn(_SKEY, masked)
        self.assertNotIn(_PSKEY, masked)
        self.assertNotIn(str(_CSRF), masked)
        # 键名保留，否则排障时看不出缺哪一项。
        self.assertIn("skey=", masked)
        self.assertIn("csrf_token=", masked)
        # 与凭证无关的键值不该被误伤。
        self.assertIn("unrelated=keepme", masked)

    def test_no_api_call_available_is_reported(self):
        result = asyncio.run(
            _handle_napcat_credential_probe(
                {"domain": "qzone.qq.com"}, {"permission_level": "user"}
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_api_call_available")

    def test_credential_tools_stay_outside_the_navigator_menu(self):
        """凭证工具不得出现在 navigator 的任何分区里。

        脱敏之后它们即使被调用也不再泄漏值，但仍无需暴露给模型；
        这一条防止有人顺手把它们塞进某个 section 的 tools。
        """
        from core.prompt_navigator import (
            PromptNavigator,
            default_prompt_navigator_payload,
        )

        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        menu_tools: set[str] = set()
        for section in nav.config.sections.values():
            menu_tools |= set(section.tools or ())
        for tool in (*_CREDENTIAL_TOOLS, "nc_get_rkey"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, menu_tools)


class NapcatRkeyKnownExposureTests(unittest.TestCase):
    """`nc_get_rkey` 仍回传 rkey 值 —— 本 wave 只报告，未修改。

    rkey 是 TTL 受限的媒体访问密钥，会出现在 QQ CDN 直链里
    （`core/tools_vision.py:129-130` 就依赖 URL 中带 rkey），
    与登录凭证不同级，且不在 C1 范围内。
    这个用例把现状钉成**已知且有意为之**，而不是被忽略的漏洞：
    如果以后有人把它也脱敏了，本用例会红，届时请连同本文件的注释一起更新。
    """

    def test_rkey_currently_still_returned_documented_exposure(self):
        payload = {"rkeys": [{"type": "private", "rkey": "&rkey=DEADBEEFCAFE", "ttl": "86400"}]}
        result, _ = _call("nc_get_rkey", payload)
        self.assertTrue(result.ok, result.error)
        self.assertIn("DEADBEEFCAFE", str(result.data))

    def test_mask_helper_would_cover_rkey_if_wired(self):
        """脱敏工具本身已能覆盖 rkey，接线只是一个决定。"""
        self.assertNotIn(
            "DEADBEEFCAFE", _mask_credential_text("rkey=DEADBEEFCAFE&x=1")
        )


class CompactDataCopyFidelityTests(unittest.TestCase):
    """确认本文件里的 `_compact_data` 拷贝与 core/agent.py 的实现仍一致。

    否则上面"经过压缩后也不泄漏"的论证会随 agent.py 演进而失效。
    """

    def test_local_copy_matches_agent_implementation(self):
        import inspect

        from core.agent import AgentLoop

        upstream = inspect.getsource(AgentLoop._compact_data)
        # 比对函数体的关键行为：list 截断 + >1000 字符串截断 + 其余原样。
        self.assertIn("value[:max_items]", upstream)
        self.assertIn("len(value) > 1000", upstream)
        self.assertIn("result[key] = value", upstream)
        # 上游不含任何按键名脱敏的逻辑 —— 这正是必须在 handler 层脱敏的原因。
        self.assertNotIn("skey", upstream)
        self.assertNotIn("redact", upstream)

        sample = {"s": "x" * 1500, "l": list(range(30)), "k": 1}
        mine = _compact_data(sample)
        self.assertEqual(len(mine["s"]), 1003)
        self.assertEqual(len(mine["l"]), 20)
        self.assertEqual(mine["l_total"], 30)
        self.assertEqual(mine["k"], 1)


if __name__ == "__main__":
    unittest.main()
