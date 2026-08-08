"""H2：回合级记忆反思（Hermes 风格后台反思）回归测试。

锁四件事：
1. 节流：每 N 条消息（默认 15）或每 T 分钟（默认 10）触发一次，不足不触发，配置可调。
2. 提炼写入：模型返回 JSON 事实 → 逐条走 add_memory_record（actor=agent.reflection）。
3. 失败安全：模型失败/超时/解析失败静默跳过，不阻塞回复、不抛异常。
4. 防重复：同一事实靠 embeddings 唯一索引幂等（memory_exists），不重复落库。

判据落在真实调用上，不做源码子串匹配。
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from core.audit import AuditTrail
from core.engine import YukikoEngine
from core.engine_types import EngineMessage
from core.memory import MemoryEngine


class _StubTrigger:
    def activate_session(self, **kwargs) -> None:
        pass

    def mark_reply_target(self, *args, **kwargs) -> None:
        pass

    def mark_proactive_reply(self, *args, **kwargs) -> None:
        pass


class ReflectionThrottleTests(unittest.IsolatedAsyncioTestCase):
    """锁 `_after_reply` 里反思节流接线：15 条触发 + 时间兜底 + 配置可调 + 开关。"""

    def _engine(
        self,
        *,
        reflection_enable: bool = True,
        interval_messages: int = 15,
        interval_minutes: int = 10,
    ) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.logger = logging.getLogger("test")
        engine.trigger = _StubTrigger()
        engine.followup_consume_on_send = False
        engine._last_reply_state = {}
        engine._runtime_group_chat_cache = {}
        engine.config = {
            "bot": {"name": "YuKiKo", "allow_memory": True},
            "memory": {
                "promotion_enable": False,
                "reflection_enable": reflection_enable,
                "reflection_interval_messages": interval_messages,
                "reflection_interval_minutes": interval_minutes,
            },
        }
        engine.memory = MagicMock()
        engine.memory.get_recent_messages.return_value = []
        engine._promotion_counters = {}
        engine._reflection_counters = {}
        engine._reflection_last_ts = {}
        engine._run_memory_reflection = AsyncMock(return_value=None)
        return engine

    def _message(self, conversation_id: str = "group:1") -> EngineMessage:
        return EngineMessage(
            conversation_id=conversation_id,
            user_id="10001",
            text="回复内容",
            mentioned=True,
        )

    async def test_fires_once_after_15_replies_and_resets_counter(self) -> None:
        engine = self._engine()
        for _ in range(15):
            await engine._after_reply(self._message(), "好的")
        await asyncio.sleep(0)  # 让 create_task 排程的反思任务跑完
        self.assertEqual(engine._run_memory_reflection.await_count, 1)
        self.assertEqual(engine._reflection_counters["group:1"], 0)

    async def test_does_not_fire_before_15(self) -> None:
        engine = self._engine()
        for _ in range(14):
            await engine._after_reply(self._message(), "好的")
        await asyncio.sleep(0)
        self.assertEqual(engine._run_memory_reflection.await_count, 0)
        self.assertEqual(engine._reflection_counters["group:1"], 14)

    async def test_interval_configurable(self) -> None:
        engine = self._engine(interval_messages=5)
        for _ in range(5):
            await engine._after_reply(self._message(), "好的")
        await asyncio.sleep(0)
        self.assertEqual(engine._run_memory_reflection.await_count, 1)

    async def test_time_interval_forces_reflection_even_at_low_count(self) -> None:
        engine = self._engine()
        # 距上次反思已远超 10 分钟 → 第 1 条消息就该触发
        engine._reflection_last_ts["group:1"] = time.monotonic() - 9999
        await engine._after_reply(self._message(), "好的")
        await asyncio.sleep(0)
        self.assertEqual(engine._run_memory_reflection.await_count, 1)
        self.assertEqual(engine._reflection_counters["group:1"], 0)

    async def test_respects_reflection_enable_off(self) -> None:
        engine = self._engine(reflection_enable=False)
        for _ in range(20):
            await engine._after_reply(self._message(), "好的")
        await asyncio.sleep(0)
        self.assertEqual(engine._run_memory_reflection.await_count, 0)
        self.assertNotIn("group:1", engine._reflection_counters)

    async def test_counters_are_per_conversation(self) -> None:
        engine = self._engine(interval_messages=15)
        for _ in range(15):
            await engine._after_reply(self._message("group:1"), "好的")
        for _ in range(10):
            await engine._after_reply(self._message("group:2"), "好的")
        await asyncio.sleep(0)
        self.assertEqual(engine._run_memory_reflection.await_count, 1)
        self.assertEqual(engine._reflection_counters["group:2"], 10)


class ReflectionWriteTests(unittest.IsolatedAsyncioTestCase):
    """锁 `_run_memory_reflection`：提炼 → 写入带 provenance → 失败静默。"""

    def _engine(self, chat_result: str = "") -> YukikoEngine:
        class _FakeModel:
            def __init__(self, result: str) -> None:
                self.result = result
                self.last_kwargs: dict[str, Any] = {}

            async def chat_text(self, messages, max_tokens=None, model=None) -> str:  # type: ignore[no-untyped-def]
                self.last_kwargs = {"max_tokens": max_tokens, "model": model}
                return self.result

        engine = YukikoEngine.__new__(YukikoEngine)
        engine.logger = logging.getLogger("test")
        engine.config = {
            "memory": {
                "reflection_model": "gpt-4o-mini",
                "reflection_max_tokens": 120,
            }
        }
        engine.memory = MagicMock()
        engine.memory.get_recent_texts.return_value = [
            "[小明] 我喜欢摄影",
            "[小明] 我住在杭州",
            "[YuKiKo] 好的，记下了",
        ]
        engine.memory.add_memory_record.return_value = (
            True,
            "memory_added",
            {},
        )
        engine.model_client = _FakeModel(chat_result)
        return engine

    async def test_writes_extracted_facts_with_provenance(self) -> None:
        engine = self._engine('{"facts":["小明喜欢摄影","小明住在杭州"]}')
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        calls = [call.kwargs for call in engine.memory.add_memory_record.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0],
            {
                "conversation_id": "group:1",
                "user_id": "10001",
                "role": "user",
                "content": "小明喜欢摄影",
                "actor": "agent.reflection",
                "note": "回合反思提炼",
                "reason": "memory_reflection",
            },
        )
        self.assertEqual(calls[1]["content"], "小明住在杭州")
        self.assertEqual(
            engine.model_client.last_kwargs,
            {"max_tokens": 120, "model": "gpt-4o-mini"},
        )

    async def test_empty_model_uses_main_model_with_small_max_tokens(self) -> None:
        engine = self._engine('{"facts":["小明喜欢摄影"]}')
        engine.config = {"memory": {"reflection_model": "", "reflection_max_tokens": 200}}
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        self.assertEqual(
            engine.model_client.last_kwargs,
            {"max_tokens": 200, "model": None},
        )
        engine.memory.add_memory_record.assert_called_once()

    async def test_model_failure_is_silent(self) -> None:
        class _Boom:
            async def chat_text(self, messages, max_tokens=None, model=None):  # type: ignore[no-untyped-def]
                raise RuntimeError("provider down")

        engine = self._engine()
        engine.model_client = _Boom()
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        engine.memory.add_memory_record.assert_not_called()

    async def test_model_timeout_is_silent(self) -> None:
        class _Slow:
            async def chat_text(self, messages, max_tokens=None, model=None):  # type: ignore[no-untyped-def]
                await asyncio.sleep(30)

        engine = self._engine()
        engine.model_client = _Slow()
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        engine.memory.add_memory_record.assert_not_called()

    async def test_invalid_json_is_skipped(self) -> None:
        engine = self._engine("这不是JSON，我是来聊天的")
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        engine.memory.add_memory_record.assert_not_called()

    async def test_empty_facts_list_is_skipped(self) -> None:
        engine = self._engine('{"facts":[]}')
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        engine.memory.add_memory_record.assert_not_called()

    async def test_no_messages_is_skipped(self) -> None:
        engine = self._engine('{"facts":["小明喜欢摄影"]}')
        engine.memory.get_recent_texts.return_value = []
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        engine.memory.add_memory_record.assert_not_called()

    async def test_memory_exists_duplicate_is_tolerated(self) -> None:
        engine = self._engine('{"facts":["小明喜欢摄影"]}')
        engine.memory.add_memory_record.return_value = (
            True,
            "memory_exists",
            {"duplicate": True},
        )
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        engine.memory.add_memory_record.assert_called_once()

    async def test_write_failure_is_silent(self) -> None:
        engine = self._engine('{"facts":["小明喜欢摄影"]}')
        engine.memory.add_memory_record.return_value = (
            False,
            "memory_disabled",
            {},
        )
        await engine._run_memory_reflection("10001", "group:1", "trace-1")
        # 不抛异常即通过；写入失败静默跳过
        self.assertTrue(True)

    def test_parse_reflection_facts_lenient(self) -> None:
        cases = [
            ('{"facts":["小明喜欢摄影","小明住在杭州"]}', ["小明喜欢摄影", "小明住在杭州"]),
            ('```json\n{"facts":["小明喜欢摄影"]}\n```', ["小明喜欢摄影"]),
            ('前置文本{"facts":["小明喜欢摄影"]}后置', ["小明喜欢摄影"]),
            ("", []),
            ("{}", []),
            ('{"facts":"小明喜欢摄影"}', []),
            ('{"facts":["小明喜欢摄影","小明喜欢摄影"]}', ["小明喜欢摄影"]),
            # 单字符碎片视为无意义，过滤（与 add_user_fact 的 len<2 拒绝一致）
            ('{"facts":["a","b"]}', []),
        ]
        for raw, expected in cases:
            self.assertEqual(YukikoEngine._parse_reflection_facts(raw), expected, raw)


class ReflectionDedupeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """真 MemoryEngine 验证：同一事实第二次反思写入被唯一索引幂等拦截。"""

    async def test_same_fact_written_twice_only_lands_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trail = AuditTrail(root / "audit")
            memory = MemoryEngine(
                config={"enable_daily_log": False},
                memory_dir=root / "memory",
                audit=trail,
            )

            class _FakeModel:
                async def chat_text(self, messages, max_tokens=None, model=None):  # type: ignore[no-untyped-def]
                    return '{"facts":["小明喜欢摄影"]}'

            engine = YukikoEngine.__new__(YukikoEngine)
            engine.logger = logging.getLogger("test")
            engine.config = {"memory": {}}
            engine.memory = memory
            engine.model_client = _FakeModel()

            # 先写入一条真实对话消息，让 get_recent_texts 有内容可提炼
            memory.add_message(
                conversation_id="group:1",
                user_id="10001",
                role="user",
                content="我喜欢摄影，还住在杭州",
                user_name="小明",
            )
            await engine._run_memory_reflection("10001", "group:1", "trace-1")
            await engine._run_memory_reflection("10001", "group:1", "trace-1")

            records, _ = memory.list_memory_records(conversation_id="group:1", user_id="10001")
            matched = [r for r in records if r.get("content") == "小明喜欢摄影"]
            self.assertEqual(len(matched), 1)


if __name__ == "__main__":
    unittest.main()
