"""M1：蓝图 §4.6 记忆注入 token 预算 端到端回归测试。

锁三件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.6 第 4 条：
记忆注入三段拼接要有 token 预算护栏）：
1. budget_text_parts 已在注入路径接线——画像段（profile + 知识库 + 图谱）
   在 engine._build_turn_context 内按 _profile_summary_max_chars 截断。
2. 记忆 / 相关两段有字符预算——budget_text_lines 在注入前做总量闸
   （此前只有条数上限，单行超长或总量失控没有护栏）。
3. 端到端：handle_message 注入到模型消息的记忆文本，三段各自 ≤ 上限、
   总长 ≤ 三段预算和。

判据落在真实 handle_message 链路 + 真实 AgentLoop._build_system_prompt
组装（注入点）上，不用 mock 掉组装逻辑。
"""
from __future__ import annotations

import unittest

from core.agent import AgentLoop
from core.memory import budget_text_lines
from tests.conftest import StubMemory, make_engine, make_message


class _BurstMemory(StubMemory):
    """返回超长记忆数据的 stub：验证预算截断确实发生在注入前。"""

    def get_recent_texts(self, conversation_id, limit=24) -> list[str]:
        _ = (conversation_id, limit)
        return [f"[u] 记忆行{i} " + "甲" * 500 for i in range(20)]

    def search_related(
        self, conversation_id, text, roles=("user",), user_id=None, top_k=None
    ) -> list[str]:
        _ = (conversation_id, text, roles, user_id, top_k)
        return [f"相关记忆{i} " + "乙" * 400 for i in range(15)]

    def get_user_profile_summary(self, user_id) -> str:
        _ = user_id
        return "丙" * 2000


def _section_lines(prompt: str, header: str) -> list[str]:
    """取 prompt 中某节的数据行（去掉 "- " 渲染前缀），到下一节标题为止。"""
    lines = prompt.splitlines()
    out: list[str] = []
    started = False
    for line in lines:
        if line == header:
            started = True
            continue
        if not started:
            continue
        if line.startswith("- "):
            out.append(line[2:])
        else:
            break
    return out


class BudgetTextLinesTests(unittest.TestCase):
    """budget_text_lines：列表形态预算（新增护栏的单元面）。"""

    def test_under_budget_keeps_all_lines(self) -> None:
        out = budget_text_lines(["A" * 100, "B" * 100], max_chars=1000)
        self.assertEqual(out, ["A" * 100, "B" * 100])

    def test_long_single_line_truncated_to_budget(self) -> None:
        out = budget_text_lines(["A" * 500], max_chars=200)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), 200)

    def test_cumulative_stop_after_budget_exceeded(self) -> None:
        out = budget_text_lines(["A" * 100, "B" * 100, "C" * 100], max_chars=150)
        self.assertLessEqual(sum(len(item) for item in out), 150)
        self.assertTrue(out[0].startswith("A"))
        self.assertNotIn("C", "".join(out))

    def test_blank_lines_skipped(self) -> None:
        self.assertEqual(budget_text_lines(["", "   ", "x"], max_chars=10), ["x"])

    def test_non_positive_budget_returns_empty(self) -> None:
        self.assertEqual(budget_text_lines(["x"], max_chars=0), [])
        self.assertEqual(budget_text_lines(["x"], max_chars=-1), [])

    def test_empty_lines_return_empty(self) -> None:
        self.assertEqual(budget_text_lines([], max_chars=100), [])


class MemoryInjectionBudgetE2ETests(unittest.IsolatedAsyncioTestCase):
    """handle_message 全流程：超长记忆 → 三段各自截断 → 注入文本有界。"""

    async def test_burst_memories_are_budgeted_before_injection(self) -> None:
        engine = make_engine(
            responses=["记忆预算端到端验证通过。"],
            config={
                "memory": {
                    "profile_summary_max_chars": 200,
                    "memory_context_max_chars": 400,
                    "related_memories_max_chars": 300,
                }
            },
        )
        engine.memory = _BurstMemory()

        # 捕获注入源（TurnContext）与注入点（真实 system prompt 组装）。
        captured_ctx: dict[str, object] = {}
        real_build_turn = engine._build_turn_context

        async def wrap_turn_ctx(
            message, text, trigger, allow_memory, recent_messages, alias_call_hint
        ):
            turn_ctx = await real_build_turn(
                message=message,
                text=text,
                trigger=trigger,
                allow_memory=allow_memory,
                recent_messages=recent_messages,
                alias_call_hint=alias_call_hint,
            )
            captured_ctx["turn"] = turn_ctx
            return turn_ctx

        engine._build_turn_context = wrap_turn_ctx  # type: ignore[method-assign]
        captured_system: list[str] = []
        real_build_system = AgentLoop._build_system_prompt

        def wrap_system(ctx) -> str:
            text = real_build_system(engine.agent, ctx)
            captured_system.append(text)
            return text

        engine.agent._build_system_prompt = wrap_system  # type: ignore[method-assign]

        message = make_message(text="你好", message_id="m-budget-1")
        response = await engine.handle_message(message)

        self.assertEqual(response.action, "reply")
        self.assertIn("m-budget-1", engine._seen_message_ids)
        turn = captured_ctx["turn"]
        mem_total = sum(len(line) for line in turn.memory_context)
        rel_total = sum(len(line) for line in turn.related_memories)
        profile_len = len(turn.user_profile_summary)

        # 三段各自 ≤ 上限（注入源层：预算确实生效，原始数据远超上限）
        burst = _BurstMemory()
        self.assertGreaterEqual(len(burst.get_recent_texts("", 1)[0]), 500)
        self.assertLessEqual(mem_total, engine._memory_context_max_chars)
        self.assertLessEqual(rel_total, engine._related_memories_max_chars)
        self.assertLessEqual(profile_len, engine._profile_summary_max_chars)
        self.assertLessEqual(
            mem_total + rel_total + profile_len,
            engine._memory_context_max_chars
            + engine._related_memories_max_chars
            + engine._profile_summary_max_chars,
        )

        # 注入点（模型消息）：三段文本都被截断且各自有界
        self.assertTrue(captured_system, "handle_message 应调用真实 _build_system_prompt")
        system_text = captured_system[0]
        self.assertIn("最近对话:", system_text)
        self.assertIn("相关记忆:", system_text)
        self.assertIn("用户画像:", system_text)
        mem_lines = _section_lines(system_text, "最近对话:")
        rel_lines = _section_lines(system_text, "相关记忆:")
        self.assertGreater(len(mem_lines), 0)
        self.assertGreater(len(rel_lines), 0)
        self.assertLessEqual(
            sum(len(line) for line in mem_lines), engine._memory_context_max_chars
        )
        self.assertLessEqual(
            sum(len(line) for line in rel_lines), engine._related_memories_max_chars
        )
        profile_part = (
            system_text.split("用户画像: ", 1)[1].splitlines()[0]
            if "用户画像: " in system_text
            else ""
        )
        self.assertTrue(profile_part)
        self.assertLessEqual(len(profile_part), 300)
        self.assertLess(len(profile_part), 2000)

    async def test_default_budgets_also_cap_burst_memories(self) -> None:
        """不传配置时用默认预算，超长记忆同样被截断（护栏默认开启）。"""
        engine = make_engine(responses=["默认预算也生效。"])
        engine.memory = _BurstMemory()
        message = make_message(text="你好", message_id="m-budget-2")

        response = await engine.handle_message(message)

        self.assertEqual(response.action, "reply")
        # 配置缺省时护栏默认开启（1600 / 1200 / 800）
        self.assertEqual(engine._memory_context_max_chars, 1600)
        self.assertEqual(engine._related_memories_max_chars, 1200)
        self.assertEqual(engine._profile_summary_max_chars, 800)
        self.assertIn("默认预算也生效", response.reply_text)

    async def test_under_budget_lines_kept_intact(self) -> None:
        """短记忆不受影响：预算护栏不误伤正常规模注入。"""
        engine = make_engine(
            responses=["短记忆注入正常。"],
            config={
                "memory": {
                    "profile_summary_max_chars": 800,
                    "memory_context_max_chars": 1600,
                    "related_memories_max_chars": 1200,
                }
            },
        )

        class _SmallMemory(StubMemory):
            def get_recent_texts(self, conversation_id, limit=24) -> list[str]:
                _ = (conversation_id, limit)
                return ["[u] 今天天气不错", "[bot] 是呀，适合出门散步"]

            def search_related(
                self, conversation_id, text, roles=("user",), user_id=None, top_k=None
            ) -> list[str]:
                _ = (conversation_id, text, roles, user_id, top_k)
                return ["你之前问过 Python 的列表推导式"]

            def get_user_profile_summary(self, user_id) -> str:
                _ = user_id
                return "喜欢编程，常用 Python。"

        engine.memory = _SmallMemory()
        captured_system: list[str] = []
        real_build_system = AgentLoop._build_system_prompt

        def wrap_system(ctx) -> str:
            text = real_build_system(engine.agent, ctx)
            captured_system.append(text)
            return text

        engine.agent._build_system_prompt = wrap_system  # type: ignore[method-assign]

        message = make_message(text="你好", message_id="m-budget-3")
        response = await engine.handle_message(message)

        self.assertEqual(response.action, "reply")
        system_text = captured_system[0]
        mem_lines = _section_lines(system_text, "最近对话:")
        rel_lines = _section_lines(system_text, "相关记忆:")
        # 群聊缓存会再追加一行 [群聊定向缓存]，短记忆本身原样保留
        self.assertIn("[u] 今天天气不错", mem_lines)
        self.assertIn("[bot] 是呀，适合出门散步", mem_lines)
        self.assertEqual(rel_lines, ["你之前问过 Python 的列表推导式"])
        self.assertIn("喜欢编程，常用 Python。", system_text)


if __name__ == "__main__":
    unittest.main()
