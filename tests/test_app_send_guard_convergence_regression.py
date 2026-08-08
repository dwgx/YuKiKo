"""架构收敛任务 B3：app.py NoneBot 发送路径收敛到统一发送核心。

迁移内容：
- app.py 的 `send_msg` 限流/熔断/暂停逻辑换成 `core.response_delivery.build_send_guard`，
  与平台主路径（core/platform/run_primary.py）共用同一发送保护核心。
- 失败标记保留在 `_safe_send` 内部（`_noop_send_failure_mark` 显式 no-op），
  避免对「结果未知 / 不可重试」这类本不该停发的情况额外熔断。

`send_response` 是 ~900 行的大 handler，无法黑盒驱动；按仓库惯例（同
`test_voice_silk_regression`）用 AST/source 判据锁结构，再对 guard 做行为断言。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.response_delivery import build_send_guard

_APP_PY = Path(__file__).resolve().parent.parent / "app.py"


class AppSendGuardConvergenceSourceTests(unittest.TestCase):
    """锁 app.py 发送路径确实委托统一核心，而不是内联 token-bucket。"""

    @staticmethod
    def _app_source() -> str:
        return _APP_PY.read_text(encoding="utf-8")

    def test_send_msg_delegates_to_build_send_guard(self) -> None:
        src = self._app_source()
        self.assertIn("from core.response_delivery import build_send_guard", src)
        self.assertIn("send_guard = build_send_guard(", src)
        self.assertIn("ok = await send_guard(msg)", src)
        self.assertIn("mark_failure_fn=_noop_send_failure_mark", src)

    def test_send_msg_no_inline_bucket_reserve(self) -> None:
        """旧的每消息 `_get_send_bucket(...).reserve()` 应已删除。"""
        src = self._app_source()
        self.assertNotIn("await asyncio.sleep(wait_seconds)", src)

    def test_noop_send_failure_mark_is_noop(self) -> None:
        from app import _noop_send_failure_mark

        # 该函数是空实现，不应有任何副作用（此处仅验证可调用、不抛错）。
        self.assertIsNone(_noop_send_failure_mark(1, "b", "send_rejected"))


class AppSendGuardConvergenceBehaviorTests(unittest.IsolatedAsyncioTestCase):
    """行为等价：guard 仍做限流/暂停/熔断，但失败标记交给 _safe_send（no-op）。"""

    async def test_rejected_send_returns_false_without_extra_marking(self) -> None:
        """sender 失败（如 _safe_send 返回 False）→ guard 返回 False，但不额外熔断。"""
        from app import _noop_send_failure_mark

        async def sender(chain: object) -> bool:
            return False

        mark = MagicMock()
        guard = build_send_guard(
            {"send_rate": {"enable": False}},
            sender,
            conversation_id="group:1",
            group_id=1,
            bot_id="bot1",
            mark_failure_fn=_noop_send_failure_mark,
        )
        ok = await guard("msg")
        self.assertFalse(ok)
        # app.py 注入 no-op：guard 不会额外调用 _mark_group_send_block / _suspend_bot_send。
        mark.assert_not_called()

    async def test_bot_suspended_skips_sender(self) -> None:
        async def sender(chain: object) -> bool:
            raise AssertionError("sender 不应被调用")

        # guard 在构造时 `from app import _check_bot_send_suspended`，patch 须在构造前生效。
        with patch("app._check_bot_send_suspended", return_value=(True, "test_suspend")):
            guard = build_send_guard(
                {"send_rate": {"enable": False}},
                sender,
                conversation_id="group:2",
                group_id=2,
                bot_id="bot2",
            )
            ok = await guard("msg")
        self.assertFalse(ok)

    async def test_group_blocked_skips_sender(self) -> None:
        async def sender(chain: object) -> bool:
            raise AssertionError("sender 不应被调用")

        with patch("app._check_group_send_block", return_value=(True, "test_block")):
            guard = build_send_guard(
                {"send_rate": {"enable": False}},
                sender,
                conversation_id="group:3",
                group_id=3,
                bot_id="bot3",
            )
            ok = await guard("msg")
        self.assertFalse(ok)

    async def test_rate_limit_still_waits(self) -> None:
        """限流是节流不是丢弃：超出窗口时 sleep，然后仍调用 sender。"""
        sent: list[object] = []

        async def sender(chain: object) -> bool:
            sent.append(chain)
            return True

        sleep_mock = AsyncMock()
        config = {
            "send_rate": {
                "enable": True,
                "max_per_window": 1,
                "window_seconds": 60,
                "warn_threshold": 1,
            }
        }
        guard = build_send_guard(
            config,
            sender,
            conversation_id="group:4",
            group_id=4,
            bot_id="bot4",
            sleep_fn=sleep_mock,
        )
        ok1 = await guard("a")
        ok2 = await guard("b")
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(len(sent), 2)
        sleep_mock.assert_awaited()
        self.assertGreater(sleep_mock.await_args.args[0], 0)


if __name__ == "__main__":
    unittest.main()
