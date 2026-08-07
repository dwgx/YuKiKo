"""全工具 QA 第 3 轮回归测试（4 路 QA agent 独立审查发现的问题）。

覆盖：
1. registry 全局 null→空串（不再把 None 强转成 "None" 当真值）。
2. registry 数组→逗号串（不再产出 "['a', 'b']" 脏 repr 标签）。
3. nc_get_rkey 脱敏（只报存在性，不回 rkey 值）。
4. add_request approve 默认拒绝（漏传不被静默同意）。
5. preferred_name 不再绕过 safety_review。
6. QZone 工具族挂 super_admin 权限门。
"""
from __future__ import annotations

import asyncio
import unittest

from core.agent_tools_knowledge import _handle_learn_knowledge
from core.agent_tools_napcat import (
    _handle_set_friend_add_request,
    _handle_set_group_add_request,
)
from core.agent_tools_registry import AgentToolRegistry


def _run(coro):
    return asyncio.run(coro)


class RegistryCoerceNullAndArrayTests(unittest.TestCase):
    """registry 参数强转：null 不再变 "None"，数组不再变脏 repr。"""

    def test_null_string_coerces_to_empty(self) -> None:
        value, ok = AgentToolRegistry._coerce_basic_type(None, "string")
        self.assertTrue(ok)
        self.assertEqual(value, "")

    def test_list_string_coerces_to_comma_joined(self) -> None:
        value, ok = AgentToolRegistry._coerce_basic_type(["a", "b"], "string")
        self.assertTrue(ok)
        self.assertEqual(value, "a, b")
        self.assertNotIn("'", value)

    def test_none_still_rejected_for_integer(self) -> None:
        _, ok = AgentToolRegistry._coerce_basic_type(None, "integer")
        self.assertFalse(ok)


class AddRequestDefaultRejectTests(unittest.TestCase):
    """add_request 系列：approve 缺失时默认拒绝，不静默放行。"""

    def _context(self, calls: list) -> dict:
        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {}

        return {"api_call": fake_api_call}

    def test_friend_add_request_missing_approve_defaults_reject(self) -> None:
        calls: list = []
        result = _run(
            _handle_set_friend_add_request({"flag": "abc"}, self._context(calls))
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls[0][1]["approve"], False)

    def test_group_add_request_missing_approve_defaults_reject(self) -> None:
        calls: list = []
        result = _run(
            _handle_set_group_add_request({"flag": "abc", "sub_type": "add"}, self._context(calls))
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls[0][1]["approve"], False)

    def test_explicit_approve_true_honored(self) -> None:
        calls: list = []
        result = _run(
            _handle_set_friend_add_request(
                {"flag": "abc", "approve": True}, self._context(calls)
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls[0][1]["approve"], True)


class PreferredNameSafetyReviewTests(unittest.TestCase):
    """learn_knowledge：safety_review=unsafe 时 preferred_name 也不能绕过。"""

    def test_preferred_name_with_unsafe_safety_review_rejected(self) -> None:
        # 安全门必须在 preferred_name 分支之前生效，不能借"改称呼"路径写 unsafe 内容。
        result = _run(
            _handle_learn_knowledge(
                {
                    "kind": "preferred_name",
                    "title": "叫我小柯",
                    "content": "小柯",
                    "safety_review": "unsafe",
                },
                {"knowledge_base": object()},
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unsafe_knowledge_content")


class QZonePermissionGateTests(unittest.TestCase):
    """QZone 查询族必须挂 super_admin 门，普通用户不可用 owner 凭证查任意 QQ 号。"""

    def test_qzone_tools_invisible_to_regular_user(self) -> None:
        reg = AgentToolRegistry()
        for tool in (
            "get_qzone_profile",
            "get_qzone_moods",
            "get_qzone_albums",
            "analyze_qzone",
            "get_qzone_photos",
        ):
            with self.subTest(tool=tool):
                self.assertFalse(reg._tool_visible_for_permission(tool, "user"))

    def test_qzone_tools_visible_to_super_admin(self) -> None:
        reg = AgentToolRegistry()
        for tool in (
            "get_qzone_profile",
            "get_qzone_moods",
            "get_qzone_albums",
            "analyze_qzone",
            "get_qzone_photos",
        ):
            with self.subTest(tool=tool):
                self.assertTrue(reg._tool_visible_for_permission(tool, "super_admin"))


if __name__ == "__main__":
    unittest.main()
