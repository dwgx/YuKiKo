from __future__ import annotations

import unittest

from core.agent import AgentLoop

_META_TOOLS = {"policy_guard", "think", "navigate_section"}


def _pick_success_display(steps: list[dict]) -> str | None:
    """复现 `_build_fallback_result` 的第一个循环：找最后一个可展示的成功步骤。"""

    for step in reversed(steps):
        display = str(step.get("display", ""))
        if not display or not step.get("ok"):
            continue
        tool = str(step.get("tool", "")).lower()
        if tool in _META_TOOLS:
            continue
        if AgentLoop._skip_raw_tool_display_in_fallback(tool, display):
            continue
        return display
    return None


def _pick_failure_display(steps: list[dict]) -> str | None:
    """复现第二个循环：没有可展示的成功步骤时，把失败原因如实告知。"""

    for step in reversed(steps):
        if step.get("ok"):
            continue
        display = str(step.get("display", ""))
        if not display:
            continue
        tool = str(step.get("tool", "")).lower()
        if AgentLoop._skip_raw_tool_display_in_fallback(tool, display):
            continue
        if tool in _META_TOOLS:
            continue
        return display
    return None


class FallbackDisplaySelectionRegressionTests(unittest.TestCase):
    """兜底不能把发现类工具的 display 当成给用户的答案。

    实测「画一只戴着宇航员头盔的柴犬」：generate_image_enhanced 失败，模型接着
    调 list_image_models 做诊断，随后 LLM 超时进入兜底。兜底扫到最后一个成功步骤
    就是那次诊断，于是把「可用模型: 1 个」当回复发给了用户 ——
    用户既没拿到图，也不知道生成失败了。
    """

    def _image_failure_steps(self) -> list[dict]:
        return [
            {
                "step": 1,
                "tool": "generate_image_enhanced",
                "ok": False,
                "display": "generate_image_enhanced 失败: 图片生成失败（回退通道异常）",
                "error": "image_gen_failed",
            },
            {"step": 2, "tool": "list_image_models", "ok": True, "display": "可用模型: 1 个"},
        ]

    def test_listing_tool_display_is_not_offered_as_an_answer(self) -> None:
        self.assertIsNone(_pick_success_display(self._image_failure_steps()))

    def test_real_failure_reason_is_surfaced_instead(self) -> None:
        got = _pick_failure_display(self._image_failure_steps())
        self.assertIsNotNone(got)
        self.assertIn("图片生成失败", got)

    def test_all_discovery_tools_are_skipped(self) -> None:
        for tool in (
            "list_image_models",
            "list_faces",
            "list_emojis",
            "browse_sticker_categories",
        ):
            with self.subTest(tool):
                self.assertTrue(
                    AgentLoop._skip_raw_tool_display_in_fallback(tool, "可用模型: 1 个"),
                    tool,
                )

    def test_genuine_answer_tools_are_still_offered(self) -> None:
        """回归防线：别把正常工具一起挡掉。"""

        for tool, display in (
            ("get_group_info", "群名: 帝王Sense, 成员数: 373"),
            ("get_affinity", "好感度: 67.4, Lv.6 挚友"),
            ("parse_video", "解析成功: 视频信息：标题 xxx"),
            ("search_media", "找到一张图片"),
        ):
            with self.subTest(tool):
                self.assertFalse(
                    AgentLoop._skip_raw_tool_display_in_fallback(tool, display), tool
                )

    def test_later_success_still_wins_over_earlier_failure(self) -> None:
        """工具先失败、重试成功时，仍应回报成功结果而不是失败原因。"""

        steps = [
            {"step": 1, "tool": "parse_video", "ok": False, "display": "解析失败: 超时"},
            {"step": 2, "tool": "parse_video", "ok": True, "display": "解析成功: 标题 xxx"},
        ]
        self.assertEqual(_pick_success_display(steps), "解析成功: 标题 xxx")

    def test_scrape_tools_remain_skipped(self) -> None:
        for tool in (
            "scrape_extract",
            "scrape_summarize",
            "scrape_structured",
            "scrape_follow_links",
            "fetch_webpage",
        ):
            with self.subTest(tool):
                self.assertTrue(
                    AgentLoop._skip_raw_tool_display_in_fallback(tool, "some content")
                )


if __name__ == "__main__":
    unittest.main()
