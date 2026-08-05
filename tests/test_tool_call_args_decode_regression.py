from __future__ import annotations

import json
import unittest

from core.agent import AgentLoop


class ToolCallArgumentDecodeRegressionTests(unittest.TestCase):
    """原生 tool_call 的 arguments 解析此前是 `json.loads` + 静默 except → `{}`。

    skiapi 实测返回 `{}{"keyword": "..."}`（真参数前粘了一个空对象），
    `json.loads` 抛 Extra data，于是每个带参工具都拿到空参数、日志无痕。
    实测后果：music_play 连调 5 次全失败，搜索报「参数一直不完整」。
    """

    def _decode(self, raw: object) -> dict:
        return AgentLoop._decode_tool_call_arguments(
            raw, tool_name="music_play", trace_id="t", step_idx=0
        )

    def test_should_recover_args_from_empty_object_prefix(self) -> None:
        self.assertEqual(
            self._decode('{}{"keyword": "周杰伦 稻香"}'),
            {"keyword": "周杰伦 稻香"},
        )

    def test_should_merge_multiple_concatenated_objects(self) -> None:
        self.assertEqual(self._decode('{"a": 1}{"b": 2}'), {"a": 1, "b": 2})

    def test_should_let_later_chunk_override_earlier_key(self) -> None:
        self.assertEqual(self._decode('{"k": "old"}{"k": "new"}'), {"k": "new"})

    def test_should_parse_well_formed_arguments_unchanged(self) -> None:
        self.assertEqual(self._decode('{"keyword": "abc"}'), {"keyword": "abc"})

    def test_should_return_empty_dict_for_genuinely_empty_inputs(self) -> None:
        for raw in ("{}", "", "   ", None):
            self.assertEqual(self._decode(raw), {}, repr(raw))

    def test_should_pass_through_dict_without_reparsing(self) -> None:
        payload = {"keyword": "already dict"}
        self.assertEqual(self._decode(payload), payload)

    def test_should_return_empty_dict_for_unparseable_text(self) -> None:
        self.assertEqual(self._decode("not json at all"), {})

    def test_repaired_args_are_written_back_into_conversation_history(self) -> None:
        """`assistant_msg` 会原样追加进 messages 回送 provider。

        只修执行用的参数不够 —— 历史里留着畸形串，下一轮 provider 解析自己吐出的
        `{}{"url": ...}` 得到空参数，模型认为「上一轮我没带参数」并重复调用同一工具
        直到熔断。实测：畸形串 → 模型重复调 parse_video；修正后 → final_answer。
        """

        tool_call = {
            "id": "toolu_x",
            "type": "function",
            "function": {
                "name": "parse_video",
                "arguments": '{}{"url": "https://www.bilibili.com/video/BV1x"}',
            },
        }
        args = self._decode(tool_call["function"]["arguments"])
        AgentLoop._rewrite_tool_call_arguments(tool_call, args)

        written = tool_call["function"]["arguments"]
        self.assertEqual(json.loads(written), {"url": "https://www.bilibili.com/video/BV1x"})
        self.assertNotIn("}{", written)

    def test_rewrite_falls_back_to_empty_object_when_unserializable(self) -> None:
        tool_call = {"function": {"name": "t", "arguments": "whatever"}}
        AgentLoop._rewrite_tool_call_arguments(tool_call, {"bad": object()})
        self.assertEqual(tool_call["function"]["arguments"], "{}")

    def test_rewrite_tolerates_malformed_tool_call_shape(self) -> None:
        for shape in ({}, {"function": None}, {"function": "not-a-dict"}):
            with self.subTest(repr(shape)):
                AgentLoop._rewrite_tool_call_arguments(shape, {"a": 1})

    def test_should_log_instead_of_silently_swallowing_bad_payload(self) -> None:
        """静默是这个 bug 能藏住的唯一原因，恢复路径和失败路径都必须留日志。"""

        with self.assertLogs("yukiko.agent", level="WARNING") as captured:
            self._decode('{}{"keyword": "x"}')
        self.assertTrue(
            any("recovered_from_malformed_json" in line for line in captured.output),
            captured.output,
        )

        with self.assertLogs("yukiko.agent", level="WARNING") as captured:
            self._decode("not json at all")
        self.assertTrue(
            any("unparseable" in line for line in captured.output), captured.output
        )


if __name__ == "__main__":
    unittest.main()
