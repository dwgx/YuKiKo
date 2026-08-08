"""E5：_maybe_mark_platform_send_block 补分支回归测试。

原测试只覆盖 299 限频分支（test_platform_primary_voice_guard_regression.py）。
这里补三条：
1. 120 错误码 → 群熔断 180s + bot 暂停 120s（reason platform_send_error_120_or_forbidden）。
2. forbidden / mute / 禁言 文本 → 同一分支。
3. 无匹配错误文本 → 不熔断、不暂停（避免瞬断整 bot 停发）。
"""
from __future__ import annotations

import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from core.platform.run_primary import _maybe_mark_platform_send_block


class PlatformSendBlockBranchTests(unittest.TestCase):
    def _run(self, error_text: str) -> tuple[MagicMock, MagicMock]:
        mark_block = MagicMock()
        suspend = MagicMock()
        with (
            patch("app._mark_group_send_block", mark_block),
            patch("app._suspend_bot_send", suspend),
        ):
            _maybe_mark_platform_send_block(777010, "999010", error_text)
        return mark_block, suspend

    def test_result_120_marks_180s_block_and_120s_suspend(self) -> None:
        mark_block, suspend = self._run('{"result": 120, "msg": "禁言中"}')

        self.assertEqual(mark_block.call_count, 1)
        _, when, reason = mark_block.call_args.args
        block_seconds = (when - datetime.now(UTC)).total_seconds()
        self.assertAlmostEqual(block_seconds, 180, delta=5)
        self.assertEqual(reason, "platform_send_error_120_or_forbidden")
        suspend.assert_called_once_with("999010", 120, "platform_send_error_120_or_forbidden")

    def test_forbidden_text_marks_180s_block_and_120s_suspend(self) -> None:
        mark_block, suspend = self._run("permission denied: forbidden to send group message")

        self.assertEqual(mark_block.call_count, 1)
        _, when, reason = mark_block.call_args.args
        block_seconds = (when - datetime.now(UTC)).total_seconds()
        self.assertAlmostEqual(block_seconds, 180, delta=5)
        self.assertEqual(reason, "platform_send_error_120_or_forbidden")
        suspend.assert_called_once()

    def test_mute_text_marks_180s_block_and_120s_suspend(self) -> None:
        mark_block, suspend = self._run("mute 或 禁言 状态，无法发送")

        self.assertEqual(mark_block.call_count, 1)
        self.assertEqual(suspend.call_count, 1)
        reason = mark_block.call_args.args[2]
        self.assertEqual(reason, "platform_send_error_120_or_forbidden")

    def test_result_120_equals_form_also_matches(self) -> None:
        mark_block, suspend = self._run("result = 120 被禁言")

        self.assertEqual(mark_block.call_count, 1)
        self.assertEqual(
            mark_block.call_args.args[2], "platform_send_error_120_or_forbidden"
        )
        suspend.assert_called_once()

    def test_unmatched_error_marks_nothing(self) -> None:
        """无匹配错误文本：不熔断、不暂停 —— 瞬断不能整 bot 停发。"""
        mark_block, suspend = self._run("some random network error: ECONNRESET")

        mark_block.assert_not_called()
        suspend.assert_not_called()

    def test_empty_error_marks_nothing(self) -> None:
        mark_block, suspend = self._run("")

        mark_block.assert_not_called()
        suspend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
