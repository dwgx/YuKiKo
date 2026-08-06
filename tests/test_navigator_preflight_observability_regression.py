"""preflight 的三条静默 `return None` 必须留日志，否则它的成本无法核算。

背景（实测 `storage/logs/yukiko.log`）：`navigator_preflight_plain_text` 在模板里是
`true`，而 `deep_merge_dict(template, raw)` 以模板为底，所以线上恒为开。日志里 186 个
`general_chat` 回合全部跑了这次 LLM 调用，但只有两条结果留痕：

- `navigator_preflight_section`  61 次（选出新分区）
- `navigator_timeout_section_retry_failed` 38 次（撞 20s 上限）

剩下 87 次走了三条不打日志的 `return None`（模型选了同一分区 / 未知分区 /
JSON 解不出）。这三条同样付了 4~20 秒的延迟，却在日志里完全不存在 ——
于是「preflight 值不值」这个问题在数据上无法回答，早先还因此得出过
「59/59 成功」这种只数成功日志的结论（真实成功率 61/186 ≈ 32.8%）。

本文件锁定：这三条各自留一条可计数的 `navigator_preflight_noop`，且带 outcome 与耗时。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import unittest

from core.agent import AgentContext, AgentLoop
from core.prompt_navigator import PromptNavigator, default_prompt_navigator_payload

_VISIBLE_TOOLS = [
    "think",
    "final_answer",
    "navigate_section",
    "search_media",
    "web_search",
]


class _ScriptedNavigatorClient:
    """按构造时给定的字符串回一次 —— 只用于驱动 preflight 的解析分支。"""

    enabled = True

    def __init__(self, raw: str):
        self._raw = raw
        self.calls = 0

    def supports_native_tool_calling(self) -> bool:
        return False

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0, model=None):
        _ = (messages, max_tokens, retries, backoff, model)
        self.calls += 1
        return self._raw


class _Registry:
    def has_tool(self, name: str) -> bool:
        return name in set(_VISIBLE_TOOLS)


@contextlib.contextmanager
def _capture(logger_name: str):
    """收集日志且允许零条 —— `assertLogs` 在零条时会自己失败。"""

    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger(logger_name)
    handler = _Handler(level=logging.INFO)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


class NavigatorPreflightObservabilityTests(unittest.TestCase):
    def _run(self, raw: str) -> tuple[object, list[str]]:
        client = _ScriptedNavigatorClient(raw)
        loop = AgentLoop(
            model_client=client,
            tool_registry=_Registry(),
            config={
                "agent": {"enable": True, "max_steps": 5},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="今天天气怎么样",
            trace_id="preflight-noop-test",
        )
        ctx.navigator_state = PromptNavigator.from_payload(default_prompt_navigator_payload()).initial_state(
            ctx, _VISIBLE_TOOLS
        )

        # 不用 assertLogs：成功那条分支在本函数内部一条日志都不打
        # （成功日志在调用点的 `navigator_preflight_section`），
        # assertLogs 会因为「零条记录」直接失败，掩盖真正要断言的东西。
        with _capture("yukiko.agent") as records:
            result = asyncio.run(
                loop._navigator_timeout_section_retry(
                    ctx=ctx,
                    step_idx=0,
                    tool_calls_made=0,
                    steps=[],
                    remaining=60.0,
                )
            )
        # 调用真的发生了 —— 否则这条测试证明不了「延迟已付」。
        self.assertEqual(client.calls, 1)
        return result, records

    def _noop_lines(self, lines: list[str]) -> list[str]:
        return [line for line in lines if "navigator_preflight_noop" in line]

    def test_same_section_choice_is_logged(self) -> None:
        """最常见的一条：模型认为该留在当前分区。合理，但这次调用没产生决策。"""

        result, lines = self._run(
            json.dumps(
                {"section_id": "general_chat", "reason": "闲聊即可"},
                ensure_ascii=False,
            )
        )
        self.assertIsNone(result)
        noop = self._noop_lines(lines)
        self.assertEqual(len(noop), 1, noop)
        self.assertIn("outcome=same_section", noop[0])
        self.assertIn("active=general_chat", noop[0])
        self.assertIn("elapsed=", noop[0])

    def test_unknown_section_is_logged_with_the_bad_id(self) -> None:
        result, lines = self._run(
            json.dumps(
                {"section_id": "no_such_section", "reason": "瞎选"},
                ensure_ascii=False,
            )
        )
        self.assertIsNone(result)
        noop = self._noop_lines(lines)
        self.assertEqual(len(noop), 1, noop)
        self.assertIn("outcome=unknown_section", noop[0])
        self.assertIn("no_such_section", noop[0])

    def test_unparseable_json_is_logged(self) -> None:
        result, lines = self._run("我觉得应该去搜一下")
        self.assertIsNone(result)
        noop = self._noop_lines(lines)
        self.assertEqual(len(noop), 1, noop)
        self.assertIn("outcome=unparseable_json", noop[0])

    def test_missing_section_id_is_logged(self) -> None:
        result, lines = self._run(json.dumps({"reason": "没给分区"}, ensure_ascii=False))
        self.assertIsNone(result)
        noop = self._noop_lines(lines)
        self.assertEqual(len(noop), 1, noop)
        self.assertIn("outcome=missing_section_id", noop[0])

    def test_successful_switch_is_not_counted_as_noop(self) -> None:
        """成功切分区时不能打 noop —— 否则两个 arm 的计数会混在一起。"""

        result, lines = self._run(
            json.dumps(
                {
                    "section_id": "media_search",
                    "reason": "用户要看视频",
                    "tool": "search_media",
                    "args": {"query": "天气", "media_type": "video"},
                },
                ensure_ascii=False,
            )
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "media_search")
        self.assertEqual(self._noop_lines(lines), [])

    def test_timeout_log_names_the_exception_type(self) -> None:
        """`asyncio.TimeoutError` 的 str() 是空的 —— 只打 `%s` 等于什么都没打。

        这正是那个 5 秒 cap 藏了很久的原因，见
        tests/test_navigator_timeout_cap_regression.py。
        """

        class _Hanging:
            enabled = True

            def supports_native_tool_calling(self) -> bool:
                return False

            async def chat_text_with_retry(self, *a, **kw):
                _ = (a, kw)
                await asyncio.sleep(30)
                return "{}"

        loop = AgentLoop(
            model_client=_Hanging(),
            tool_registry=_Registry(),
            config={
                "agent": {"enable": True, "max_steps": 5},
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 120},
            },
        )
        ctx = AgentContext(
            conversation_id="group:1:user:2",
            user_id="2",
            user_name="tester",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="今天天气怎么样",
            trace_id="preflight-timeout-test",
        )
        ctx.navigator_state = PromptNavigator.from_payload(default_prompt_navigator_payload()).initial_state(
            ctx, _VISIBLE_TOOLS
        )

        # remaining 给 5.5s -> timeout 3.5s，避免测试真等 20 秒。
        with _capture("yukiko.agent") as records:
            result = asyncio.run(
                loop._navigator_timeout_section_retry(
                    ctx=ctx,
                    step_idx=0,
                    tool_calls_made=0,
                    steps=[],
                    remaining=5.5,
                )
            )
        self.assertIsNone(result)
        failed = [line for line in records if "navigator_timeout_section_retry_failed" in line]
        self.assertEqual(len(failed), 1, records)
        self.assertIn("exc=TimeoutError", failed[0])
        self.assertIn("budget=", failed[0])
        self.assertIn("elapsed=", failed[0])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
