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

        with self.assertLogs("yukiko.agent", level="DEBUG") as captured:
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


class MalformedArgsAlertGradingTests(unittest.TestCase):
    """畸形 JSON 参数的告警分级。

    实测（2026-08-06，重启后 539 行日志）：`agent_tool_call` 37 次里
    `agent_tool_args_recovered_from_malformed_json` 33 次 = **89%**，
    形态全部是 `chunks=2`（provider 吐 `{}` + 真参数两段）。恢复机制兜住了
    （13/13 回合成功），但 33 条 WARNING 是那份日志里最大的噪音来源，
    而且「合并成功」和「合并出错误参数」当时用的是同一条日志 —— 真出问题时看不出来。

    分级依据不是 `chunks` 的条数（两段是 provider 的稳定形态，本身不代表异常），
    而是**这次恢复有没有损失**：

    * 后段把前段某个键覆盖成了不同的值 → 有一个真参数被丢了
    * 文本没被消费完（某段解不出来） → 有一段参数被丢了，而 merged 非空
      会让它看起来像成功

    这两种是 WARNING；无损的那 89% 降到 DEBUG，另外每 25 次汇总一条 INFO。
    """

    def _decode(self, raw: object, tool: str = "music_play") -> dict:
        return AgentLoop._decode_tool_call_arguments(
            raw, tool_name=tool, trace_id="t", step_idx=0
        )

    def test_lossless_recovery_does_not_emit_warning(self) -> None:
        """89% 的正常路径不该刷 WARNING。"""

        with self.assertLogs("yukiko.agent", level="DEBUG") as captured:
            self._decode('{}{"keyword": "x"}')
        warnings = [line for line in captured.output if line.startswith("WARNING")]
        self.assertEqual(warnings, [], f"无损恢复仍在刷 WARNING: {warnings}")

    def test_key_clobbered_with_different_value_is_a_warning(self) -> None:
        """后段覆盖前段的**不同**值 —— 有一个真参数被静默丢掉了。"""

        with self.assertLogs("yukiko.agent", level="WARNING") as captured:
            result = self._decode('{"keyword": "真参数"}{"keyword": "覆盖值"}')
        self.assertEqual(result, {"keyword": "覆盖值"}, "覆盖行为本身不该变")
        self.assertTrue(
            any("recovery_lossy" in line for line in captured.output), captured.output
        )
        self.assertTrue(
            any("keyword" in line for line in captured.output),
            f"告警没指出是哪个键被覆盖: {captured.output}",
        )

    def test_repeated_identical_value_is_not_lossy(self) -> None:
        """同一个键重复出现但值相同 —— 没有信息损失，不该报警。"""

        with self.assertLogs("yukiko.agent", level="DEBUG") as captured:
            self._decode('{"k": "same"}{"k": "same"}')
        warnings = [line for line in captured.output if line.startswith("WARNING")]
        self.assertEqual(warnings, [], f"重复但等值的键被误报成有损: {warnings}")

    def test_unconsumed_trailing_text_is_a_warning(self) -> None:
        """第二段被截断时 merged 非空，看起来像成功 —— 必须报警。

        这正是交接文档担心的「一旦形态变了就大面积断」：
        `{"a": 1}{"b":` 会解出 `{"a": 1}` 然后 break，尾巴被丢掉且此前无痕。
        """

        with self.assertLogs("yukiko.agent", level="WARNING") as captured:
            result = self._decode('{"keyword": "x"}{"mode": ')
        self.assertEqual(result, {"keyword": "x"})
        self.assertTrue(
            any("recovery_lossy" in line for line in captured.output), captured.output
        )
        self.assertTrue(
            any("unconsumed" in line for line in captured.output),
            f"告警没提未消费的尾巴: {captured.output}",
        )

    def test_three_chunk_payload_is_not_flagged_merely_for_being_three(self) -> None:
        """段数本身不是判据 —— 三段无冲突也是无损的。"""

        with self.assertLogs("yukiko.agent", level="DEBUG") as captured:
            result = self._decode('{}{"a": 1}{"b": 2}')
        self.assertEqual(result, {"a": 1, "b": 2})
        warnings = [line for line in captured.output if line.startswith("WARNING")]
        self.assertEqual(warnings, [], f"三段无冲突被误报: {warnings}")

    def test_rollup_info_line_appears_on_the_configured_interval(self) -> None:
        """降级成 DEBUG 后仍要有可见度：每 N 次一条 INFO 汇总。"""

        AgentLoop._malformed_args_recovered_total = 0
        interval = AgentLoop._MALFORMED_ARGS_LOG_EVERY
        with self.assertLogs("yukiko.agent", level="INFO") as captured:
            for _ in range(interval):
                self._decode('{}{"keyword": "x"}')
        rollups = [line for line in captured.output if "malformed_json_rollup" in line]
        self.assertEqual(
            len(rollups), 1, f"{interval} 次无损恢复应汇总成 1 条 INFO: {captured.output}"
        )
        self.assertIn(f"total={interval}", rollups[0])

    def test_rollup_counter_does_not_count_lossy_recoveries(self) -> None:
        """有损恢复有自己的 WARNING，不该混进无损计数。"""

        AgentLoop._malformed_args_recovered_total = 0
        self._decode('{"k": "a"}{"k": "b"}')
        self.assertEqual(
            AgentLoop._malformed_args_recovered_total,
            1,
            "计数器统计的是全部恢复次数，用于汇总节流",
        )

    def tearDown(self) -> None:
        AgentLoop._malformed_args_recovered_total = 0


if __name__ == "__main__":
    unittest.main()
