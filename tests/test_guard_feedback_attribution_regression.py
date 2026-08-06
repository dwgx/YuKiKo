"""守卫回喂给模型的 payload 必须归属正确：谁的错误、谁的产物、最近一次到底成功还是失败。

重复守卫拦住一次调用时会回喂一段 display 给模型，让它别再原地重试。
这段文本一旦归属错了，比不回喂更糟 —— 模型会据此对用户撒谣：

1. 失败原因取自**全量** steps 而不是该工具自己的 steps →
   别的工具（比如 analyze_image 超时）的错误被写成「该工具最近一次失败的真实原因」。
2. 该工具先成功过、随后连续崩溃时，产物非空就抢先说「上一次调用已经成功」，
   最近两次的真实失败被整条丢掉，连 error 字段里都没有 ——
   与「让模型能向用户解释为什么失败」这个修复目标正好相反。
3. 产物取的是「最后一次成功」，与被拦那次的参数无关；
   最近一次是失败时还断言「原样携带其中的 image_urls」，等于让它把旧结果当本次交付。

三条同源：没有区分「该工具最近一次的结果」。本文件把归属契约钉住。
"""

from __future__ import annotations

import unittest

from core.agent import AgentLoop


class GuardFeedbackAttributionTests(unittest.TestCase):
    def _payload(self, steps: list[dict], tool: str = "parse_video") -> dict:
        loop = AgentLoop.__new__(AgentLoop)
        return loop._build_guard_feedback_payload(
            tool_name=tool,
            steps=steps,
            reason_key="repeated_tool_call",
            reason_text="同一工具和参数重复过多",
        )

    def test_other_tools_error_is_not_attributed_to_this_tool(self) -> None:
        """analyze_image 超时不能被说成 parse_video 的失败原因。"""

        payload = self._payload(
            [{"tool": "analyze_image", "ok": False, "error": "analyze_image 执行超时（>45s）"}]
        )
        self.assertNotIn("analyze_image", payload["display"])
        self.assertNotIn("45s", payload["display"])

    def test_own_error_is_reported(self) -> None:
        payload = self._payload(
            [
                {"tool": "parse_video", "ok": False, "error": "这个视频链接命中了安全限制"},
                {"tool": "parse_video", "ok": False, "error": "这个视频链接命中了安全限制"},
            ]
        )
        self.assertIn("安全限制", payload["display"])

    def test_own_error_wins_over_another_tools_error(self) -> None:
        payload = self._payload(
            [
                {"tool": "analyze_image", "ok": False, "error": "图片超时"},
                {"tool": "parse_video", "ok": False, "error": "视频元数据读不到"},
            ]
        )
        self.assertIn("元数据", payload["display"])
        self.assertNotIn("图片超时", payload["display"])

    def test_success_then_failure_reports_the_failure(self) -> None:
        """核心场景：先成功后崩溃，不能说「上一次调用已经成功」。"""

        payload = self._payload(
            [
                {
                    "tool": "parse_video",
                    "ok": True,
                    "display": "解析成功",
                    "data": {"video_url": "/tmp/a.mp4"},
                },
                {"tool": "parse_video", "ok": False, "error": "下载失败（链接失效）"},
            ]
        )
        self.assertIn("下载失败", payload["display"])
        self.assertNotIn("已经成功", payload["display"])
        # 更早的产物不能挂在 already_obtained 上被当成本次结果
        self.assertNotIn("already_obtained", payload)

    def test_success_then_failure_still_surfaces_the_earlier_artifact(self) -> None:
        """更早确实拿到过东西 —— 两件事都要给，只是要标清是更早那次的。"""

        payload = self._payload(
            [
                {
                    "tool": "parse_video",
                    "ok": True,
                    "display": "解析成功",
                    "data": {"video_url": "/tmp/a.mp4"},
                },
                {"tool": "parse_video", "ok": False, "error": "下载失败"},
            ]
        )
        earlier = payload.get("earlier_partial_result") or {}
        self.assertEqual(earlier.get("video_url"), "/tmp/a.mp4")

    def test_latest_success_claims_success(self) -> None:
        payload = self._payload(
            [
                {
                    "tool": "parse_video",
                    "ok": True,
                    "display": "解析成功",
                    "data": {"video_url": "/tmp/a.mp4"},
                }
            ]
        )
        self.assertIn("已经成功", payload["display"])
        self.assertEqual(
            (payload.get("already_obtained") or {}).get("video_url"), "/tmp/a.mp4"
        )
        self.assertNotIn("earlier_partial_result", payload)

    def test_guard_marker_steps_do_not_count_as_a_real_attempt(self) -> None:
        """守卫自己写进 steps 的标记不是一次真实调用结果，不能改变成功/失败判定。"""

        loop = AgentLoop.__new__(AgentLoop)
        guard_error = f"{loop._GUARD_STEP_ERRORS[0]}:3"
        payload = self._payload(
            [
                {
                    "tool": "parse_video",
                    "ok": True,
                    "display": "解析成功",
                    "data": {"video_url": "/tmp/a.mp4"},
                },
                {"tool": "parse_video", "ok": False, "error": guard_error},
            ]
        )
        self.assertIn("已经成功", payload["display"])

    def test_no_history_falls_back_to_the_reason_key(self) -> None:
        payload = self._payload([])
        self.assertIn("repeated_tool_call", payload["display"])
        self.assertFalse(payload.get("already_obtained"))

    def test_payload_always_marks_itself_not_ok(self) -> None:
        for steps in (
            [],
            [{"tool": "parse_video", "ok": False, "error": "x"}],
            [{"tool": "parse_video", "ok": True, "display": "y", "data": {}}],
        ):
            with self.subTest(steps=steps):
                payload = self._payload(steps)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["tool"], "parse_video")


if __name__ == "__main__":
    unittest.main()
