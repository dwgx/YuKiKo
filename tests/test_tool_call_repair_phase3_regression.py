"""Phase 3b：validate-then-repair 工具调用修复回归测试。

锁四件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.3（5））：
1. P0 四项通用修复：剥离 null / 字符串化 JSON 数组 / 单键对象解包 / 裸值包数组。
2. P2 关系不变量：read_file 缺 offset/limit 补默认。
3. 修复是「扩展语义 + 透明标记」，不做拒绝。
4. 快通道：非 dict 参数零开销原样返回。
"""
from __future__ import annotations

import unittest

from core.tool_call_repair import repair_tool_call


class RepairP0Tests(unittest.TestCase):
    def test_strips_null_values(self) -> None:
        args = {"query": "test", "mode": None, "limit": None}
        out, repairs = repair_tool_call(args)
        self.assertEqual(out, {"query": "test"})
        self.assertIn("stripped_null", repairs)

    def test_json_array_string_converted_to_real_array(self) -> None:
        schema = {"properties": {"tags": {"type": "array"}}}
        args = {"tags": '["a", "b"]'}
        out, repairs = repair_tool_call(args, schema=schema)
        self.assertEqual(out["tags"], ["a", "b"])
        self.assertTrue(any(r.startswith("json_array_string") for r in repairs))

    def test_single_key_object_unwrapped_when_keys_match_schema(self) -> None:
        schema = {"properties": {"query": {"type": "string"}, "mode": {"type": "string"}}}
        args = {"参数": {"query": "你好", "mode": "text"}}
        out, repairs = repair_tool_call(args, schema=schema)
        self.assertEqual(out, {"query": "你好", "mode": "text"})
        self.assertIn("unwrapped_single_key_object", repairs)

    def test_bare_value_wrapped_into_single_element_array(self) -> None:
        schema = {"properties": {"urls": {"type": "array"}}}
        args = {"urls": "https://example.com/a.png"}
        out, repairs = repair_tool_call(args, schema=schema)
        self.assertEqual(out["urls"], ["https://example.com/a.png"])
        self.assertTrue(any(r.startswith("wrapped_single") for r in repairs))

    def test_array_value_left_untouched(self) -> None:
        schema = {"properties": {"urls": {"type": "array"}}}
        args = {"urls": ["a", "b"]}
        out, repairs = repair_tool_call(args, schema=schema)
        self.assertEqual(out["urls"], ["a", "b"])
        self.assertEqual(repairs, [])


class RepairP2Tests(unittest.TestCase):
    def test_read_file_missing_offset_limit_gets_defaults(self) -> None:
        args = {"path": "/tmp/x.txt"}
        out, repairs = repair_tool_call(args, tool_name="read_file")
        self.assertEqual(out["offset"], 0)
        self.assertEqual(out["limit"], 2000)
        self.assertIn("default_offset", repairs)
        self.assertIn("default_limit", repairs)

    def test_read_file_explicit_args_not_overwritten(self) -> None:
        args = {"path": "/tmp/x.txt", "offset": 5, "limit": 100}
        out, _ = repair_tool_call(args, tool_name="read_file")
        self.assertEqual(out["offset"], 5)
        self.assertEqual(out["limit"], 100)

    def test_other_tools_do_not_get_defaults(self) -> None:
        args = {"query": "x"}
        out, repairs = repair_tool_call(args, tool_name="search")
        self.assertEqual(out, {"query": "x"})
        self.assertEqual(repairs, [])


class RepairFastPathTests(unittest.TestCase):
    def test_non_dict_args_returned_unchanged(self) -> None:
        out, repairs = repair_tool_call("not a dict")
        self.assertEqual(out, "not a dict")
        self.assertEqual(repairs, [])

    def test_none_args_returned_unchanged(self) -> None:
        out, repairs = repair_tool_call(None)
        self.assertIsNone(out)
        self.assertEqual(repairs, [])


if __name__ == "__main__":
    unittest.main()
