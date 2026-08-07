"""模型沉默权威性回归 —— 车道 silence。

四个真实线上缺陷：
1. 模型交的空 final_answer 被四个与「模型想不想沉默」无关的硬编码条件否决，
   改写成一句道歉发进群（实测 239 条 final_answer 里 60 条空，
   `agent_intentional_silence` 全天只有 5 次）。
2. 旁听轮（群友之间聊天，没跟机器人说话）的兜底把内部故障文案发进群。
3. 模型自己写的 final_answer 从不过 `_scrub_internal_state_text`，
   `analyze_image 执行超时（>45s）` 被当正文发出。
4. preflight + 超时重试串行叠加 20+45+20 秒，且 `agent_llm_step_latency`
   只在成功分支记录，超时样本被系统性排除。
"""
from __future__ import annotations

import ast
import asyncio
import json
import time
import unittest
from pathlib import Path

from core.agent import AgentContext, AgentLoop
from core.agent_tools import ToolCallResult

_AGENT_PY = Path(__file__).resolve().parent.parent / "core" / "agent.py"


class _StubRegistry:
    """最小工具注册表 stub（照 tests/test_agent_smoke.py 的形状）。"""

    tool_count = 3

    def __init__(self, names: set[str] | None = None, ok: bool = True):
        self._names = names or {"web_search", "final_answer", "think"}
        self._ok = ok
        self.calls: list[tuple[str, dict]] = []

    def has_tool(self, name: str) -> bool:
        return name in self._names

    def get_schema(self, name: str):
        return None

    def select_tools_for_intent(self, message_text: str, perm_level: str) -> list[str]:
        _ = (message_text, perm_level)
        return list(self._names)

    def get_schemas_for_prompt_filtered(self, selected_tools: list[str]) -> str:
        return "\n".join(f"- {n}" for n in selected_tools)

    def get_prompt_hints_text(self, section: str, tool_names: list[str] | None = None) -> str:
        _ = (section, tool_names)
        return ""

    def list_tools_for_permission(self, permission_level: str = "user") -> list[str]:
        _ = permission_level
        return list(self._names)

    def get_schemas_for_native_tools(self, tool_names: list[str]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": n,
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for n in tool_names
        ]

    def get_dynamic_context(self, payload: dict, tool_names: list[str] | None = None) -> str:
        _ = (payload, tool_names)
        return ""

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        if self._ok:
            return ToolCallResult(ok=True, data={}, display=f"{name} 执行完成")
        return ToolCallResult(ok=False, data={}, display="", error="upstream_5xx")


class _SequencedModelClient:
    enabled = True

    def __init__(self, responses: list[str], native_tools: bool = False):
        self._responses = list(responses)
        self._native_tools = native_tools
        self.text_calls = 0

    def supports_native_tool_calling(self) -> bool:
        return self._native_tools

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0, **kwargs):
        _ = (messages, max_tokens, retries, backoff, kwargs)
        self.text_calls += 1
        if not self._responses:
            raise AssertionError("No more model responses prepared for test")
        return self._responses.pop(0)

    async def chat_completion_with_retry(
        self, messages, max_tokens=0, tools=None, retries=0, backoff=0.0, **kwargs
    ):
        _ = (messages, max_tokens, tools, retries, backoff, kwargs)
        if not self._responses:
            raise AssertionError("No more model responses prepared for test")
        resp = self._responses.pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": resp}}]}


def _make_ctx(**overrides) -> AgentContext:
    base = AgentContext(
        conversation_id="group:1:user:2",
        user_id="2",
        user_name="tester",
        group_id=1,
        bot_id="bot",
        is_private=False,
        mentioned=True,
        message_text="今天天气怎么样",
        trace_id="silence-test",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _make_loop(
    responses: list[str],
    registry: _StubRegistry | None = None,
) -> AgentLoop:
    reg = registry or _StubRegistry()
    loop = AgentLoop(
        model_client=_SequencedModelClient(responses),
        tool_registry=reg,
        config={
            "agent": {
                "enable": True,
                "max_steps": 6,
                "fallback_on_parse_error": True,
            },
            "admin": {"super_users": ["10001"]},
            "queue": {"process_timeout_seconds": 120},
        },
    )
    loop.high_risk_control_enable = False
    loop._build_system_prompt = lambda ctx: "system prompt"
    loop._build_user_message = lambda ctx: ctx.message_text
    return loop


_EMPTY_FINAL = json.dumps({"tool": "final_answer", "args": {"text": ""}}, ensure_ascii=False)


class ModelEmptyFinalAnswerIsSilenceTests(unittest.TestCase):
    """修复 1：空 final_answer 的归属只看「本轮有没有真的工具失败」。"""

    def test_empty_final_answer_stays_empty_when_mentioned_and_no_tool_failed(self) -> None:
        """被 @ 且没调过任何工具时，空 final_answer 依然是模型选择的沉默。

        基线里 `not ctx.mentioned` / `has_thought` / `len(msg) <= 4` 三个条件
        任一不满足就走兜底，这个用例三个全不满足 → 基线返回一句兜底文案。
        """
        loop = _make_loop([_EMPTY_FINAL])
        result = asyncio.run(loop.run(_make_ctx(mentioned=True, message_text="今天天气怎么样")))

        self.assertEqual(result.reply_text, "")
        self.assertEqual(result.reason, "agent_final_answer")

    def test_empty_final_answer_stays_empty_in_private_chat(self) -> None:
        """私聊同理：`is_private` 与「模型想不想沉默」无关。"""
        loop = _make_loop([_EMPTY_FINAL])
        result = asyncio.run(
            loop.run(_make_ctx(is_private=True, mentioned=False, message_text="在吗"))
        )

        self.assertEqual(result.reply_text, "")

    def test_empty_final_answer_after_successful_tool_stays_empty(self) -> None:
        """工具成功但模型决定不说话 → 不拿工具 display 顶上。"""
        registry = _StubRegistry(ok=True)
        loop = _make_loop(
            [
                json.dumps({"tool": "web_search", "args": {"query": "天气"}}, ensure_ascii=False),
                _EMPTY_FINAL,
            ],
            registry=registry,
        )
        result = asyncio.run(loop.run(_make_ctx()))

        self.assertEqual(registry.calls and registry.calls[0][0], "web_search")
        self.assertEqual(result.reply_text, "")

    def test_empty_final_answer_after_failed_tool_falls_back(self) -> None:
        """有真失败的工具步 → 空 final_answer 允许兜底（这条不能被修坏）。"""
        registry = _StubRegistry(ok=False)
        loop = _make_loop(
            [
                json.dumps({"tool": "web_search", "args": {"query": "天气"}}, ensure_ascii=False),
                _EMPTY_FINAL,
                "网上没查到，你再给我点线索。",
            ],
            registry=registry,
        )
        result = asyncio.run(loop.run(_make_ctx()))

        self.assertTrue(result.reply_text, "有失败工具步时应当兜底，不能静默")

    def test_internal_orchestration_steps_are_not_counted_as_failures(self) -> None:
        """`policy_guard` / `think` / `navigate_section` 没有 `ok` 字段。

        把它们算成失败会让每一轮都走兜底，沉默永远到不了。
        """
        from core.agent import _INTERNAL_STEP_TOOLS  # noqa: PLC0415 — 基线红要落在行为上

        steps = [
            {"step": 0, "tool": "policy_guard", "error": "navigator_tool_required"},
            {"step": 1, "tool": "think", "thought": "想一下"},
            {"step": 2, "tool": "navigate_section", "ok": True, "display": "已切换"},
        ]
        self.assertEqual(AgentLoop._real_tool_failure_count(steps), 0)
        self.assertIn("policy_guard", _INTERNAL_STEP_TOOLS)

        steps.append({"step": 3, "tool": "web_search", "ok": False, "error": "upstream"})
        self.assertEqual(AgentLoop._real_tool_failure_count(steps), 1)

    def test_silence_predicate_reads_no_direction_or_length_signal(self) -> None:
        """AST 断言：`intentional_silence` 的判据里不能再有指向性/长度条件。

        落在语法结构上，不靠源码子串匹配（注释里同样的词不会误伤）。
        """
        tree = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))
        assigns = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "intentional_silence"
                for t in node.targets
            )
        ]
        self.assertEqual(len(assigns), 1, "intentional_silence 应当只有一处赋值")
        expr = assigns[0].value

        attrs = {
            n.attr
            for n in ast.walk(expr)
            if isinstance(n, ast.Attribute)
        }
        self.assertNotIn("mentioned", attrs)
        self.assertNotIn("is_private", attrs)
        names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
        self.assertNotIn("len", names, "不能再用消息长度判沉默")
        self.assertNotIn("has_thought", names, "空 final_answer 本身就是沉默表达")


class FinalAnswerInternalStateScrubTests(unittest.TestCase):
    """修复 3：模型自己写的 final_answer 也要过内部状态清洗。"""

    def test_tool_name_prefixed_timeout_never_reaches_the_user(self) -> None:
        """实测泄漏串（日志 L961，群友引用可见）。"""
        self.assertEqual(
            AgentLoop._normalize_final_answer_text("analyze_image 执行超时（>45s）"),
            "执行超时（>45s）",
        )

    def test_key_value_machine_state_is_stripped(self) -> None:
        out = AgentLoop._normalize_final_answer_text("retcode=-1 没拿到")
        self.assertNotIn("retcode", out)
        self.assertIn("没拿到", out)

    def test_normal_reply_with_url_is_untouched(self) -> None:
        """URL 要原样保留 —— 媒体链接靠它投递。"""
        text = "这是你要的图 https://example.com/a_b.png"
        self.assertEqual(AgentLoop._normalize_final_answer_text(text), text)

    def test_plain_chinese_reply_is_untouched(self) -> None:
        text = "今天上海多云，最高 31 度，出门带把伞。"
        self.assertEqual(AgentLoop._normalize_final_answer_text(text), text)

    def test_normalize_calls_the_existing_scrubber(self) -> None:
        """AST 断言：`_normalize_final_answer_text` 里必须真的调用清洗器。

        判据落在 AST 的 Call 节点上，不是源码子串 —— 注释里写了名字不算。
        """
        tree = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_normalize_final_answer_text":
                target = node
                break
        self.assertIsNotNone(target, "_normalize_final_answer_text 没找到")
        called = {
            n.func.attr
            for n in ast.walk(target)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        self.assertIn("_scrub_internal_state_text", called)

    def test_empty_final_fallback_display_is_scrubbed(self) -> None:
        """修复 1 那条兜底路径返回的工具显示串同样要过清洗。"""
        loop = _make_loop(
            [
                json.dumps({"tool": "web_search", "args": {"query": "x"}}, ensure_ascii=False),
                _EMPTY_FINAL,
            ],
            registry=_ScrubProbeRegistry(),
        )
        result = asyncio.run(loop.run(_make_ctx()))

        self.assertNotIn("analyze_image", result.reply_text)
        self.assertNotIn("retcode", result.reply_text)


class _ScrubProbeRegistry(_StubRegistry):
    """一步成功（display 带机器标识符）+ 一步失败，逼出兜底取 display 的路径。"""

    def __init__(self) -> None:
        super().__init__({"web_search", "final_answer", "think"})
        self._n = 0

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = (args, context)
        self.calls.append((name, dict(args)))
        self._n += 1
        if self._n == 1:
            return ToolCallResult(
                ok=True,
                data={},
                display="analyze_image 拿到结果，retcode=0，图里是一只猫。",
            )
        return ToolCallResult(ok=False, data={}, display="", error="upstream_5xx")


class _TimeoutModelClient:
    """主 LLM 调用永远超时（模拟 provider 不可用）。"""

    enabled = True

    def __init__(self, native_tools: bool = False):
        self._native_tools = native_tools
        self.text_calls = 0

    def supports_native_tool_calling(self) -> bool:
        return self._native_tools

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0, **kwargs):
        _ = (messages, max_tokens, retries, backoff, kwargs)
        self.text_calls += 1
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def chat_completion_with_retry(self, messages, **kwargs):
        _ = (messages, kwargs)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _ErrorModelClient(_TimeoutModelClient):
    """主 LLM 调用直接抛错。"""

    async def chat_text_with_retry(self, messages, max_tokens=0, retries=0, backoff=0.0, **kwargs):
        _ = (messages, max_tokens, retries, backoff, kwargs)
        self.text_calls += 1
        raise RuntimeError("provider exploded")


def _loop_with_client(client) -> AgentLoop:
    loop = AgentLoop(
        model_client=client,
        tool_registry=_StubRegistry(),
        config={
            "agent": {"enable": True, "max_steps": 3, "llm_step_timeout_seconds": 6},
            "admin": {"super_users": []},
            "queue": {"process_timeout_seconds": 120},
        },
    )
    loop.high_risk_control_enable = False
    loop._build_system_prompt = lambda ctx: "system prompt"
    loop._build_user_message = lambda ctx: ctx.message_text
    return loop


class UndirectedTurnFallbackSilenceTests(unittest.TestCase):
    """修复 2：旁听轮的兜底不许把内部故障文案发进群。"""

    @staticmethod
    def _undirected_ctx() -> AgentContext:
        """群友之间的普通对话：没 @、非私聊、engine 也没判定指向。"""
        return _make_ctx(
            mentioned=False,
            is_private=False,
            explicit_bot_addressed=False,
            reply_to_user_id="",
            message_text="你昨天那个片子看完了吗",
        )

    def test_undirected_llm_timeout_sends_nothing(self) -> None:
        loop = _loop_with_client(_TimeoutModelClient())
        loop.llm_step_timeout_seconds = 1
        loop.llm_step_timeout_seconds_after_tool = 1

        result = asyncio.run(loop.run(self._undirected_ctx()))

        self.assertEqual(result.reply_text, "")
        self.assertEqual(result.image_url, "")
        self.assertEqual(result.video_url, "")

    def test_undirected_llm_error_sends_nothing_without_opt_in_config(self) -> None:
        """基线里静默还要 `allow_silent_on_llm_error`（默认 False）才生效。"""
        loop = _loop_with_client(_ErrorModelClient())
        self.assertFalse(loop.allow_silent_on_llm_error)

        result = asyncio.run(loop.run(self._undirected_ctx()))

        self.assertEqual(result.reply_text, "")

    def test_undirected_fallback_over_failed_tool_sends_nothing(self) -> None:
        """有失败工具步也一样：旁听轮不外发。"""
        loop = _loop_with_client(_TimeoutModelClient())
        result = asyncio.run(
            loop._build_fallback_result(
                self._undirected_ctx(),
                [{"step": 0, "tool": "web_search", "ok": False, "error": "upstream_5xx",
                  "display": "网络那边没给我结果，我换个说法再试试看行不行。"}],
                1,
                0.0,
                "llm_timeout",
            )
        )

        self.assertEqual(result.reply_text, "")

    def test_directed_turn_keeps_its_fallback_sentence(self) -> None:
        """被 @ 的回合必须保留兜底句，否则就是「装死」。"""
        loop = _loop_with_client(_TimeoutModelClient())
        loop.llm_step_timeout_seconds = 1
        loop.llm_step_timeout_seconds_after_tool = 1

        result = asyncio.run(loop.run(_make_ctx(mentioned=True)))

        self.assertTrue(result.reply_text, "指向轮次不能静默")

    def test_reply_to_bot_counts_as_directed(self) -> None:
        """引用机器人自己的发言 = 在跟机器人接话，是结构事实。"""
        loop = _loop_with_client(_TimeoutModelClient())
        ctx = _make_ctx(
            mentioned=False,
            is_private=False,
            explicit_bot_addressed=False,
            bot_id="99999",
            reply_to_user_id="99999",
        )
        self.assertTrue(loop._is_directed_turn(ctx))

    def test_engine_supplied_flag_wins_over_inference(self) -> None:
        """engine 显式说不指向时，即使被 @ 也听 engine 的（followup 语义由它决定）。"""
        loop = _loop_with_client(_TimeoutModelClient())
        self.assertFalse(
            loop._is_directed_turn(_make_ctx(mentioned=True, was_directed=False))
        )
        self.assertTrue(
            loop._is_directed_turn(
                _make_ctx(mentioned=False, explicit_bot_addressed=False, was_directed=True)
            )
        )


class NavigatorRetryBudgetTests(unittest.TestCase):
    """修复 4：preflight + 超时重试不再串行叠加 20+45+20 秒。"""

    def test_preflight_budget_is_capped_far_below_twenty_seconds(self) -> None:
        """preflight 一档预算。实测有收益的 preflight 均值 8.6s，20s 全是白等。"""
        loop = _loop_with_client(_TimeoutModelClient())
        self.assertLessEqual(loop.navigator_preflight_timeout_seconds, 10.0)

        # 墙钟很充裕时，拿到的仍是 preflight 那一档，不是旧的 20 秒。
        timeout = loop._resolve_navigator_retry_timeout(
            600.0, loop.navigator_preflight_timeout_seconds
        )
        self.assertLessEqual(timeout, 10.0)
        self.assertGreater(timeout, 2.5)

    def test_post_timeout_retry_budget_is_much_smaller_than_preflight(self) -> None:
        """主 LLM 已超时 = provider 不可用，那次 retry 不该再要 20 秒。"""
        loop = _loop_with_client(_TimeoutModelClient())
        self.assertLessEqual(loop.navigator_post_timeout_retry_seconds, 5.0)

        timeout = loop._resolve_navigator_retry_timeout(
            600.0, loop.navigator_post_timeout_retry_seconds
        )
        self.assertLessEqual(timeout, 5.0)

    def test_retry_is_skipped_when_budget_cap_is_zero(self) -> None:
        """预算配成 0 = 直接跳过这次调用，不是退回默认 20 秒。"""
        loop = _loop_with_client(_TimeoutModelClient())
        self.assertEqual(loop._resolve_navigator_retry_timeout(600.0, 0.0), 0.0)

    def test_retry_is_skipped_when_wall_clock_nearly_exhausted(self) -> None:
        """墙钟只剩 3 秒时不该再发一次几乎必然超时的调用。"""
        loop = _loop_with_client(_TimeoutModelClient())
        self.assertEqual(loop._resolve_navigator_retry_timeout(3.0, 8.0), 0.0)

    def test_wall_clock_shrinks_the_cap_when_it_is_the_tighter_bound(self) -> None:
        """两个上限取较小值：墙钟比档位紧时听墙钟。"""
        loop = _loop_with_client(_TimeoutModelClient())
        self.assertAlmostEqual(
            loop._resolve_navigator_retry_timeout(8.0, 20.0), 6.0, places=3
        )

    def test_both_retry_helpers_take_a_budget_cap_from_their_call_sites(self) -> None:
        """AST 断言：三个调用点都必须显式传 budget_cap。

        判据落在 AST 的关键字实参上，不是源码子串 —— 少传一个就红。
        """
        tree = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {"_navigator_timeout_section_retry", "_navigator_timeout_tool_retry"}
        ]
        self.assertGreaterEqual(len(calls), 3, "应当有 preflight + 超时后两处 retry")
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords}
            self.assertIn("budget_cap", kwargs, "每个调用点都要显式给预算档位")

    def test_chain_has_a_single_wall_clock_bound(self) -> None:
        """AST 断言：run() 里存在统一墙钟变量，且被主 LLM 预算计算用到。"""
        tree = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))
        run_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
                run_fn = node
                break
        self.assertIsNotNone(run_fn, "AgentLoop.run 没找到")
        assigned = {
            t.id
            for n in ast.walk(run_fn)
            if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        self.assertIn("chain_deadline_ts", assigned)
        loads = {
            n.id
            for n in ast.walk(run_fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        self.assertIn("chain_deadline_ts", loads)
        self.assertIn("chain_left", assigned, "主调用也要被链预算夹住")

    def test_explicit_small_llm_budget_is_not_raised_by_the_chain_floor(self) -> None:
        """链预算的地板不能反过来抬高配置故意给的小预算。

        写这条是因为第一版实现用了 `max(6.0, ...)`，把测试里配的 1 秒
        抬成 6 秒 —— 测试照样绿，只是每个超时用例慢 5 秒。
        """
        loop = _loop_with_client(_TimeoutModelClient())
        loop.llm_step_timeout_seconds = 1
        loop.llm_step_timeout_seconds_after_tool = 1

        started = asyncio.get_event_loop_policy().new_event_loop()
        try:
            elapsed_before = started.time()
            started.run_until_complete(loop.run(_make_ctx(mentioned=True)))
            elapsed = started.time() - elapsed_before
        finally:
            started.close()

        self.assertLess(
            elapsed, 5.0, f"配了 1 秒预算却跑了 {elapsed:.1f}s，地板抬高了配置值"
        )


class NavigatorChainWallClockBehaviourTests(unittest.TestCase):
    """修复 4 的行为断言：整条链的实测墙钟。

    这个用例**不引用任何新符号** —— 只给配置、驱动 `run()`、量耗时。
    所以它在基线上红的是行为（基线把 preflight 硬编码成 20 秒，
    压根不读这些键），不是 `AttributeError`。
    """

    _PREFLIGHT_TOOLS = [
        "think",
        "final_answer",
        "navigate_section",
        "search_media",
        "web_search",
    ]

    def _loop_with_preflight(self) -> AgentLoop:
        loop = AgentLoop(
            model_client=_TimeoutModelClient(),
            tool_registry=_StubRegistry(set(self._PREFLIGHT_TOOLS)),
            config={
                "agent": {
                    "enable": True,
                    "max_steps": 2,
                    "llm_step_timeout_seconds": 6,
                    "navigator_preflight_plain_text": True,
                    # 基线硬编码 min(20.0, …)，这两个键它完全不读。
                    "navigator_preflight_timeout_seconds": 3.0,
                    "navigator_post_timeout_retry_seconds": 0.0,
                    "navigator_chain_wall_clock_seconds": 12.0,
                },
                "admin": {"super_users": []},
                "queue": {"process_timeout_seconds": 300},
            },
        )
        loop.high_risk_control_enable = False
        loop._build_system_prompt = lambda ctx: "system prompt"
        loop._build_user_message = lambda ctx: ctx.message_text
        return loop

    def _ctx_in_general_chat(self) -> AgentContext:
        from core.prompt_navigator import (  # noqa: PLC0415
            PromptNavigator,
            default_prompt_navigator_payload,
        )

        ctx = _make_ctx(mentioned=True, message_text="帮我找个视频")
        ctx.navigator_state = PromptNavigator.from_payload(
            default_prompt_navigator_payload()
        ).initial_state(ctx, list(self._PREFLIGHT_TOOLS))
        return ctx

    def test_dead_provider_does_not_stack_preflight_and_retry_budgets(self) -> None:
        """provider 全程不响应时，整条链的墙钟必须被配置压住。

        基线：preflight 20s（忽略配置） + 主调用 + 超时后 retry 20s。
        实测 trace 118886-14 是 20+45+20 = 85 秒，用户只拿到一句超时道歉。
        """
        loop = self._loop_with_preflight()
        ctx = self._ctx_in_general_chat()

        started = time.monotonic()
        asyncio.run(loop.run(ctx))
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            14.0,
            f"整条链跑了 {elapsed:.1f}s，preflight/retry 预算没被配置压住",
        )


class LlmStepLatencyObservabilityTests(unittest.TestCase):
    """修复 4 的观测缺陷：超时样本此前被系统性排除在延迟日志外。"""

    def test_timeout_branch_also_logs_step_latency(self) -> None:
        """真实驱动一次超时，断言 latency 事件带 outcome=timeout。

        实测断言，不是源码匹配：日志里没有这条，下一个人会再次从
        `n=108 max=39.8s` 读出「45s 阈值很安全」的假结论。
        """
        loop = _loop_with_client(_TimeoutModelClient())
        loop.llm_step_timeout_seconds = 1
        loop.llm_step_timeout_seconds_after_tool = 1

        with self.assertLogs("yukiko.agent", level="INFO") as captured:
            asyncio.run(loop.run(_make_ctx(mentioned=True)))

        latency_lines = [
            line for line in captured.output if "agent_llm_step_latency" in line
        ]
        self.assertTrue(latency_lines, "超时分支没有记 agent_llm_step_latency")
        self.assertTrue(
            any("outcome=timeout" in line for line in latency_lines),
            f"超时样本没有 outcome=timeout 标记: {latency_lines}",
        )

    def test_success_branch_latency_carries_outcome_ok(self) -> None:
        """成功分支要带 outcome=ok，否则两类样本无法分开聚合。"""
        loop = _make_loop([_EMPTY_FINAL])

        with self.assertLogs("yukiko.agent", level="INFO") as captured:
            asyncio.run(loop.run(_make_ctx(mentioned=True)))

        latency_lines = [
            line for line in captured.output if "agent_llm_step_latency" in line
        ]
        self.assertTrue(latency_lines)
        self.assertTrue(any("outcome=ok" in line for line in latency_lines))


if __name__ == "__main__":
    unittest.main()
