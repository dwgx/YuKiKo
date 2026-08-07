"""Phase 1a：Hermes 式错误回喂自纠回归测试。

锁两件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.2）：
1. `_decode_tool_call_arguments` 加 markdown ```json``` 块提取兜底（Hermes 三级兜底第三级）。
2. 工具失败时回喂带 retry_instruction，引导模型自纠重调而非代码侧猜参。

判据落在真实调用上。
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.agent import AgentLoop


class ToolCallArgsMarkdownFallbackTests(unittest.TestCase):
    """_decode_tool_call_arguments 的 JSON 三级兜底（含 markdown 提取）。"""

    def test_markdown_json_block_is_extracted(self) -> None:
        decoded = AgentLoop._decode_tool_call_arguments(
            '```json\n{"keyword": "测试"}\n```',
            tool_name="search", trace_id="t", step_idx=0,
        )
        self.assertEqual(decoded, {"keyword": "测试"})

    def test_markdown_without_lang_tag_is_extracted(self) -> None:
        decoded = AgentLoop._decode_tool_call_arguments(
            '```\n{"mode": "video"}\n```',
            tool_name="parse_video", trace_id="t", step_idx=0,
        )
        self.assertEqual(decoded, {"mode": "video"})

    def test_plain_json_still_parses(self) -> None:
        decoded = AgentLoop._decode_tool_call_arguments(
            '{"keyword": "x"}', tool_name="search", trace_id="t", step_idx=0,
        )
        self.assertEqual(decoded, {"keyword": "x"})

    def test_concatenated_json_still_recovers(self) -> None:
        # skiapi 历史 bug：`{}{"keyword": "y"}` 串接 JSON。
        decoded = AgentLoop._decode_tool_call_arguments(
            '{}{"keyword": "y"}', tool_name="search", trace_id="t", step_idx=0,
        )
        self.assertEqual(decoded, {"keyword": "y"})

    def test_markdown_wrapping_concatenated_json_recovers(self) -> None:
        decoded = AgentLoop._decode_tool_call_arguments(
            '```json\n{}{"keyword": "z"}\n```',
            tool_name="search", trace_id="t", step_idx=0,
        )
        self.assertEqual(decoded, {"keyword": "z"})

    def test_unparseable_returns_empty(self) -> None:
        decoded = AgentLoop._decode_tool_call_arguments(
            "not json at all", tool_name="search", trace_id="t", step_idx=0,
        )
        self.assertEqual(decoded, {})

    def test_dict_passthrough(self) -> None:
        decoded = AgentLoop._decode_tool_call_arguments(
            {"keyword": "直接给 dict"}, tool_name="search", trace_id="t", step_idx=0,
        )
        self.assertEqual(decoded, {"keyword": "直接给 dict"})


class ToolResultRetryInstructionTests(unittest.TestCase):
    """失败回喂必须带 retry_instruction（Hermes 式自纠引导）。"""

    @staticmethod
    def _assistant_msg() -> dict:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "search", "arguments": "{}"}}
            ],
        }

    def test_failure_appends_retry_instruction(self) -> None:
        """失败回喂 content 必须含 retry_instruction，引导模型自纠。"""
        loop = AgentLoop.__new__(AgentLoop)
        import json

        tool_result_msg = {
            "tool": "search",
            "ok": False,
            "display": "search 失败: 参数缺 keyword",
            "error": "missing_arg:keyword",
            "retry_instruction": (
                "工具调用失败。请阅读上方 error 定位原因，用正确的参数重新调用该工具；"
                "如果参数确实无法满足，直接向用户说明失败和替代方案。不要臆造工具结果。"
            ),
        }
        parsed = {"id": "call_1", "name": "search", "arguments": {}}
        messages: list = []
        loop._append_tool_result(messages, parsed, self._assistant_msg(), "resp", tool_result_msg)
        content = json.loads(messages[-1]["content"])
        self.assertIn("retry_instruction", content["tool_result"])
        self.assertIn("不要臆造工具结果", content["tool_result"]["retry_instruction"])

    def test_success_has_no_retry_instruction(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        import json

        tool_result_msg = {"tool": "search", "ok": True, "display": "ok"}
        parsed = {"id": "call_1", "name": "search", "arguments": {}}
        messages: list = []
        loop._append_tool_result(messages, parsed, self._assistant_msg(), "resp", tool_result_msg)
        content = json.loads(messages[-1]["content"])
        self.assertNotIn("retry_instruction", content["tool_result"])


if __name__ == "__main__":
    unittest.main()
