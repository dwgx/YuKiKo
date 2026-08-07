"""全工具 QA 剩余 Medium（第 2 轮）回归测试。

覆盖接手文档 TAKEOVER-2026-08-08 §4 的 5 个 Medium 项：
1. search_zhihu：search 模式缺 query / answers 模式缺 question_id 时给出具体缺参错误，
   而不是模糊的 "invalid mode or missing query"，让模型能自纠。
2. get_qq_avatar：不传 / 传空串 qq 时回退到当前用户（schema 允许，fallback 生效）。
3. translate_en2zh：schema 改为 string（LLM 常发字符串），handler 兼容 string/list。
4. delete_message：普通用户只能撤回机器人自己发送的消息，不能撤回他人消息。
5. parse_video：抖音用户主页 /user/xxx 提前拒绝，不再误入解析流程烧近 10 秒。
"""
from __future__ import annotations

import asyncio
import unittest

from core.agent_tools_knowledge import _handle_search_zhihu
from core.agent_tools_media import _handle_parse_video
from core.agent_tools_napcat import _handle_delete_message, _handle_translate_en2zh
from core.agent_tools_web import _handle_get_qq_avatar
from core.tools_video import ToolVideoMixin


def _run(coro):
    return asyncio.run(coro)


class _VideoUrlStub(ToolVideoMixin):
    """带平台域名集的 ToolVideoMixin 最小实例，用于测真实 URL 判定。"""

    def __init__(self) -> None:
        self._platform_video_domains = {
            "douyin.com",
            "iesdouyin.com",
            "kuaishou.com",
            "chenzhongtech.com",
            "bilibili.com",
            "b23.tv",
            "acfun.cn",
            "acfun.com",
            "youku.com",
            "youtube.com",
            "youtu.be",
            "iqiyi.com",
            "qiyi.com",
            "iq.com",
            "v.qq.com",
            "m.v.qq.com",
            "qq.com",
        }


class _FakeItem:
    def __init__(self, title: str, snippet: str | None = None, heat: str | None = None) -> None:
        self.title = title
        self.snippet = snippet
        self.heat = heat


class _FakeZhihu:
    async def hot_list(self, limit: int = 15) -> list[_FakeItem]:
        return [_FakeItem("热榜1", heat="100万热度")]

    async def search(self, query: str, limit: int = 8) -> list[_FakeItem]:
        return [_FakeItem(f"搜索结果: {query}", "简介")]

    async def get_top_answers(self, question_id: str, limit: int = 3) -> list[_FakeItem]:
        return [_FakeItem(f"回答 {question_id}", "回答正文")]


class _FakeCrawlerHub:
    zhihu = _FakeZhihu()


class SearchZhihuSchemaTests(unittest.TestCase):
    """search_zhihu：条件必填参数给出具体错误提示。"""

    def _context(self) -> dict:
        return {"crawler_hub": _FakeCrawlerHub()}

    def test_search_mode_requires_query(self) -> None:
        result = _run(_handle_search_zhihu({"mode": "search"}, self._context()))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing_query")
        self.assertIn("query", result.display)

    def test_answers_mode_requires_question_id(self) -> None:
        result = _run(_handle_search_zhihu({"mode": "answers"}, self._context()))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing_question_id")
        self.assertIn("question_id", result.display)

    def test_invalid_mode_rejected(self) -> None:
        result = _run(_handle_search_zhihu({"mode": "bogus"}, self._context()))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_mode")

    def test_hot_mode_works_without_extra_args(self) -> None:
        result = _run(_handle_search_zhihu({"mode": "hot"}, self._context()))
        self.assertTrue(result.ok)
        self.assertIn("热榜", result.display)

    def test_search_mode_with_query_works(self) -> None:
        result = _run(_handle_search_zhihu({"mode": "search", "query": "量子纠缠"}, self._context()))
        self.assertTrue(result.ok)
        self.assertIn("量子纠缠", result.display)

    def test_answers_mode_with_question_id_works(self) -> None:
        result = _run(
            _handle_search_zhihu({"mode": "answers", "question_id": "123456"}, self._context())
        )
        self.assertTrue(result.ok)

    def test_search_mode_none_query_treated_as_missing(self) -> None:
        # registry 的 string 强转会把 null 变成 "None"，不应真的去搜 "None"。
        result = _run(_handle_search_zhihu({"mode": "search", "query": "None"}, self._context()))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing_query")

    def test_answers_mode_none_question_id_treated_as_missing(self) -> None:
        result = _run(_handle_search_zhihu({"mode": "answers", "question_id": "null"}, self._context()))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing_question_id")


class GetQqAvatarFallbackTests(unittest.TestCase):
    """get_qq_avatar：空 qq 回退到当前用户，显式 qq 优先。"""

    def test_empty_qq_falls_back_to_current_user(self) -> None:
        result = _run(_handle_get_qq_avatar({"qq": ""}, {"user_id": "123456789"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["qq"], "123456789")
        self.assertIn("123456789", result.data["image_url"])

    def test_missing_qq_falls_back_to_current_user(self) -> None:
        result = _run(_handle_get_qq_avatar({}, {"user_id": 987654321}))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["qq"], "987654321")

    def test_explicit_qq_wins(self) -> None:
        result = _run(_handle_get_qq_avatar({"qq": "10001"}, {"user_id": "123456789"}))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["qq"], "10001")

    def test_empty_qq_passes_registry_validation(self) -> None:
        # 空串且非必填：registry 不应报 invalid_qq，应保留空串交给 handler 回退当前用户。
        from core.agent_tools_registry import AgentToolRegistry
        from core.agent_tools_types import ToolCallResult, ToolSchema

        reg = AgentToolRegistry()
        reg.register(
            ToolSchema(
                name="get_qq_avatar",
                description="x",
                parameters={
                    "type": "object",
                    "properties": {"qq": {"type": "string"}},
                    "required": [],
                },
                category="media",
            ),
            lambda args, ctx: ToolCallResult(ok=True),
        )
        sanitized, err = reg._sanitize_and_validate_args("get_qq_avatar", {"qq": ""})
        self.assertEqual(err, "")
        self.assertEqual(sanitized.get("qq"), "")


class TranslateEn2zhArgsTests(unittest.TestCase):
    """translate_en2zh：schema 为 string，handler 兼容 string/list。"""

    def test_string_words_wrapped_to_list(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {"data": {"result": "你好"}}

        result = _run(
            _handle_translate_en2zh(
                {"words": "good morning"}, {"api_call": fake_api_call}
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls[0][0], "translate_en2zh")
        self.assertEqual(calls[0][1]["words"], ["good morning"])

    def test_list_words_passed_through(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {"data": {"result": "你好"}}

        result = _run(
            _handle_translate_en2zh(
                {"words": ["hello", "world"]}, {"api_call": fake_api_call}
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls[0][1]["words"], ["hello", "world"])

    def test_empty_words_rejected(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append((api, dict(kwargs)))
            return {}

        result = _run(_handle_translate_en2zh({"words": ""}, {"api_call": fake_api_call}))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "missing words")
        self.assertEqual(calls, [])

    def test_junk_string_rejected_not_translated(self) -> None:
        # registry 的 string 强转会把 None/空容器转成 "None"/"[]" 字面量串，
        # 这些不是待翻译文本，应视为缺参而不是真去调翻译 API。
        for junk in ("None", "null", "[]", "{}"):
            calls: list[tuple[str, dict]] = []

            async def fake_api_call(api: str, **kwargs):
                calls.append((api, dict(kwargs)))
                return {}

            result = _run(_handle_translate_en2zh({"words": junk}, {"api_call": fake_api_call}))
            self.assertFalse(result.ok, f"junk={junk} should be rejected")
            self.assertEqual(result.error, "missing words", f"junk={junk}")
            self.assertEqual(calls, [], f"junk={junk} should not call API")


class DeleteMessageSelfRecallTests(unittest.TestCase):
    """delete_message：普通用户只能撤回机器人自己的消息。"""

    def _run_delete(self, sender_id: int, permission_level: str, bot_id: str = "100") -> tuple[object, list[str]]:
        calls: list[str] = []

        async def fake_api_call(api: str, **kwargs):
            calls.append(api)
            if api == "get_msg":
                return {"data": {"message_id": kwargs.get("message_id"), "sender": {"user_id": sender_id}}}
            return {"data": {}, "retcode": 0}

        context = {
            "api_call": fake_api_call,
            "permission_level": permission_level,
            "bot_id": bot_id,
        }
        result = _run(_handle_delete_message({"message_id": 12345}, context))
        return result, calls

    def test_user_cannot_recall_others_message(self) -> None:
        result, calls = self._run_delete(sender_id=999, permission_level="user")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "permission_denied:not_bot_own_message")
        self.assertNotIn("delete_msg", calls)

    def test_user_can_recall_bot_own_message(self) -> None:
        result, calls = self._run_delete(sender_id=100, permission_level="user", bot_id="100")
        self.assertTrue(result.ok)
        self.assertIn("delete_msg", calls)

    def test_group_admin_can_recall_any_message(self) -> None:
        # 管理员不校验归属，直接撤回。
        result, calls = self._run_delete(sender_id=999, permission_level="group_admin")
        self.assertTrue(result.ok)
        self.assertIn("delete_msg", calls)

    def test_agent_loop_allows_regular_user_delete_message(self) -> None:
        # agent 循环的群管理门必须放行 delete_message 给 handler 做归属校验，
        # 否则普通用户的自助撤回在 agent 路径直接被挡回 need_group_admin。
        from core.agent import AgentLoop

        class _FakeCtx:
            user_id = "12345"

        self.assertTrue(
            AgentLoop._is_regular_user_self_ban_attempt(
                _FakeCtx(), "delete_message", {"message_id": 1}
            )
        )

    def test_agent_loop_blocks_recall_recent_for_regular_user(self) -> None:
        from core.agent import AgentLoop

        class _FakeCtx:
            user_id = "12345"

        self.assertFalse(
            AgentLoop._is_regular_user_self_ban_attempt(
                _FakeCtx(), "recall_recent_messages", {"group_id": 1}
            )
        )


class _FakeResolveResult:
    ok = True
    payload = {"video_url": "https://example.com/out.mp4", "text": "ok"}


class _FakeToolExecutor:
    def __init__(self) -> None:
        self.resolve_calls = 0
        self.detail_urls: set[str] = set()
        self.direct_urls: set[str] = set()

    def _is_platform_video_detail_url(self, url: str) -> bool:
        return url in self.detail_urls

    def _is_direct_video_url(self, url: str) -> bool:
        return url in self.direct_urls

    async def _method_browser_resolve_video(self, method_name: str, method_args: dict, query: str):
        self.resolve_calls += 1
        return _FakeResolveResult()


class ParseVideoDouyinProfileTests(unittest.TestCase):
    """parse_video：抖音用户主页提前拒绝，不进入解析流程。"""

    def test_douyin_user_profile_rejected_before_resolve(self) -> None:
        executor = _FakeToolExecutor()
        url = "https://www.douyin.com/user/MS4wLjABAAAAexample"
        result = _run(_handle_parse_video({"url": url}, {"tool_executor": executor}))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_args:not_supported_video_url")
        self.assertEqual(executor.resolve_calls, 0)

    def test_real_video_detail_still_resolves(self) -> None:
        executor = _FakeToolExecutor()
        url = "https://www.douyin.com/video/1234567890123456789"
        executor.detail_urls.add(url)
        result = _run(_handle_parse_video({"url": url}, {"tool_executor": executor}))
        self.assertTrue(result.ok)
        self.assertEqual(executor.resolve_calls, 1)

    def test_platform_detail_recognizes_douyin_profile_as_non_video(self) -> None:
        stub = _VideoUrlStub()
        self.assertFalse(
            stub._is_platform_video_detail_url(
                "https://www.douyin.com/user/MS4wLjABAAAAexample"
            )
        )
        self.assertFalse(
            ToolVideoMixin._is_douyin_video_or_note_url(
                "https://www.douyin.com/user/MS4wLjABAAAAexample"
            )
        )

    def test_platform_detail_keeps_real_douyin_video(self) -> None:
        stub = _VideoUrlStub()
        self.assertTrue(
            stub._is_platform_video_detail_url(
                "https://v.douyin.com/hskaBb36Hfg/"
            )
        )
        self.assertTrue(
            ToolVideoMixin._is_douyin_video_or_note_url(
                "https://www.douyin.com/video/1234567890123456789"
            )
        )

    def test_douyin_modal_id_video_still_resolves(self) -> None:
        # 抖音 web 分享用 ?modal_id= 指代视频，不能被 /user/ 修复误拒。
        stub = _VideoUrlStub()
        url = "https://www.douyin.com/?modal_id=7113211048889477411"
        self.assertTrue(stub._is_platform_video_detail_url(url))
        self.assertTrue(ToolVideoMixin._is_douyin_video_or_note_url(url))


if __name__ == "__main__":
    unittest.main()
