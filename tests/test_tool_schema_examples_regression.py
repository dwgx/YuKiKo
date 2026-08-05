"""ToolSchema.input_examples 回归 — 示例必须能到达模型，且不影响老注册。

覆盖:
1. input_examples 默认为空，导出路径对老注册逐字节不变（~170 个内置注册全部省略该字段）
2. 有示例时，示例出现在原生 function calling 的 description 里（走文本，保证可见）
3. 纯文本 prompt 渲染路（非原生 tool calling / Navigator 决策器）同样能看到示例
4. 示例数量有上限，脏数据不会炸掉整份 schema 导出
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent_tools_registry import AgentToolRegistry, register_builtin_tools  # noqa: E402
from core.agent_tools_types import ToolCallResult, ToolSchema  # noqa: E402


async def _noop_handler(args: dict, context: dict) -> ToolCallResult:
    return ToolCallResult(ok=True)


def _build_full_registry() -> AgentToolRegistry:
    """构建一份包含所有内置工具的 registry（与 test_tool_schema_audit 同法）。"""
    registry = AgentToolRegistry()
    config = {
        "api": {"api_key": "test", "base_url": "http://test", "model": "test"},
        "social": {"qzone_enabled": False},
        "media": {
            "image_gen_enabled": True,
            "image_gen_api_key": "test",
            "image_gen_base_url": "http://test",
        },
    }
    try:
        register_builtin_tools(registry, MagicMock(), MagicMock(), MagicMock(), config)
    except Exception:
        # 测试环境下部分工具可能注册失败，测能测的那些就够
        pass
    return registry


class ToolSchemaInputExamplesDefaultTests(unittest.TestCase):
    """默认值与向后兼容。"""

    def test_input_examples_defaults_to_empty_tuple(self):
        schema = ToolSchema(name="t", description="d")
        self.assertEqual(schema.input_examples, ())

    def test_list_of_dicts_is_coerced_to_tuple(self):
        schema = ToolSchema(
            name="t",
            description="d",
            input_examples=[{"a": 1}, {"b": 2}],
        )
        self.assertIsInstance(schema.input_examples, tuple)
        self.assertEqual(schema.input_examples, ({"a": 1}, {"b": 2}))

    def test_non_dict_examples_are_discarded(self):
        schema = ToolSchema(
            name="t",
            description="d",
            input_examples=["not a dict", 42, None, {"ok": 1}],  # type: ignore[list-item]
        )
        self.assertEqual(schema.input_examples, ({"ok": 1},))

    def test_garbage_input_examples_type_becomes_empty(self):
        schema = ToolSchema(name="t", description="d", input_examples="oops")  # type: ignore[arg-type]
        self.assertEqual(schema.input_examples, ())


class ToolSchemaExamplesBackwardCompatTests(unittest.TestCase):
    """老注册（全部省略 input_examples）的导出结果必须完全不变。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = _build_full_registry()
        cls.tool_names = list(cls.registry._schemas.keys())

    def test_registry_is_not_empty(self):
        self.assertGreater(len(self.tool_names), 100, "内置工具没注册上，后面的断言没意义")

    def test_examples_are_opt_in_so_most_tools_still_have_none(self):
        """原断言是「全部内置注册都为空」—— 那在示例机制刚落地、还没有任何工具用它时成立，
        现在已有 10 个工具用上了，断言随之过期。

        真正要守的契约不是「没人用这个字段」，而是「它是可选的」：绝大多数工具
        不带示例，机制不能变成每个注册都必须填的负担。
        """

        total = len(self.registry._schemas)
        with_examples = [
            name for name, schema in self.registry._schemas.items() if schema.input_examples
        ]
        self.assertGreater(total, 100, "注册表规模异常，后面的比例断言没有意义")
        self.assertLess(
            len(with_examples),
            total // 4,
            f"带示例的工具占比过高（{len(with_examples)}/{total}），示例应是精选而非普遍",
        )

    def test_description_identical_to_raw_for_tools_without_examples(self):
        """不带示例的工具，原生 payload 的 description 必须与 schema.description 逐字符相同。

        这是向后兼容的真正含义：加机制不能动到没用它的那 ~150 个工具。
        """

        native = self.registry.get_schemas_for_native_tools(self.tool_names)
        self.assertEqual(len(native), len(self.tool_names))
        checked = 0
        for tool in native:
            function = tool["function"]
            schema = self.registry._schemas[function["name"]]
            if schema.input_examples:
                continue
            self.assertEqual(
                function["description"], schema.description, f"{function['name']} description 被改动"
            )
            checked += 1
        self.assertGreater(checked, 0, "样本里没有不带示例的工具，这个测试没验到东西")

    def test_examples_marker_appears_only_for_tools_that_declared_them(self):
        """示例标记不能泄漏到没声明示例的工具上。"""

        native = self.registry.get_schemas_for_native_tools(self.tool_names)
        for tool in native:
            function = tool["function"]
            schema = self.registry._schemas[function["name"]]
            has_marker = "调用示例" in function["description"]
            # 有两条通路会产生这个标记：
            #   1. `input_examples` 字段 —— 由 `_render_input_examples` 追加
            #   2. 描述串内联 —— `download_file` / `smart_download` 是 `_ext_tools`
            #      的 5 元组行（core/agent_tools_napcat.py:2004 起），没有 ToolSchema
            #      字面量可用，只能把示例写进描述。加字段要改约 110 行元组，不值得。
            inline = "调用示例" in schema.description
            self.assertEqual(
                has_marker,
                bool(schema.input_examples) or inline,
                f"{function['name']}: 标记出现={has_marker} 但既无字段也非内联",
            )

        # prompt 导出路径把所有工具拼成一个 blob，没法按工具切分，
        # 所以只断言「声明过示例的工具，其示例确实出现在 blob 里」。
        # 泄漏方向的契约由上面逐工具的原生检查覆盖。
        prompt = self.registry.get_schemas_for_prompt_filtered(self.tool_names)
        for name in self.tool_names:
            schema = self.registry._schemas.get(name)
            if schema is None or not schema.input_examples:
                continue
            self.assertIn(
                "调用示例",
                prompt,
                f"{name} 声明了示例，但 prompt 导出里没有示例标记",
            )
            break

        # 未过滤的全量导出：原断言是「不含示例标记」，那在没有任何工具声明示例时成立。
        # 现在有 10 个工具声明了，标记必然出现。改为断言它只出现在声明过的工具名附近，
        # 且出现次数不超过声明数 + 内联数 —— 防止渲染重复追加。
        full = self.registry.get_schemas_for_prompt()
        declared_count = sum(
            1 for schema in self.registry._schemas.values() if schema.input_examples
        )
        inline_count = sum(
            1
            for schema in self.registry._schemas.values()
            if not schema.input_examples and "调用示例" in schema.description
        )
        self.assertLessEqual(
            full.count("调用示例"),
            declared_count + inline_count,
            "示例标记出现次数超过声明数，渲染可能重复追加了",
        )


class ToolSchemaExamplesReachModelTests(unittest.TestCase):
    """有示例时，示例必须出现在模型能看到的每条导出路径上。"""

    EXAMPLES = (
        {"url": "https://example.com/a.mp4", "mode": "clip", "start_seconds": 3},
        {"mode": "frames", "max_frames": 4},
    )

    def setUp(self):
        self.registry = AgentToolRegistry()
        self.registry.register(
            ToolSchema(
                name="demo_tool",
                description="演示工具。",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "链接"},
                        "mode": {"type": "string", "description": "clip / frames"},
                        "start_seconds": {"type": "number", "description": "起始秒"},
                        "max_frames": {"type": "integer", "description": "帧数上限"},
                    },
                    "required": [],
                },
                category="media",
                input_examples=self.EXAMPLES,
            ),
            _noop_handler,
        )

    def test_examples_present_in_native_tool_description(self):
        native = self.registry.get_schemas_for_native_tools(["demo_tool"])
        description = native[0]["function"]["description"]
        self.assertIn("演示工具。", description)
        self.assertIn("调用示例", description)
        self.assertIn('"mode": "clip"', description)
        self.assertIn('"max_frames": 4', description)

    def test_examples_survive_json_serialization_of_payload(self):
        """原生 payload 是直接 json 进 HTTP body 的，示例必须可序列化。"""
        native = self.registry.get_schemas_for_native_tools(["demo_tool"])
        blob = json.dumps(native, ensure_ascii=False)
        self.assertIn("调用示例", blob)

    def test_examples_present_in_filtered_prompt_render(self):
        prompt = self.registry.get_schemas_for_prompt_filtered(["demo_tool"])
        self.assertIn("### demo_tool", prompt)
        self.assertIn("调用示例", prompt)
        self.assertIn('"start_seconds": 3', prompt)

    def test_examples_present_in_full_prompt_render(self):
        prompt = self.registry.get_schemas_for_prompt()
        self.assertIn("调用示例", prompt)

    def test_examples_present_in_get_schemas(self):
        schemas = self.registry.get_schemas()
        self.assertEqual(len(schemas), 1)
        self.assertIn("调用示例", schemas[0]["description"])

    def test_parameters_block_unchanged_by_examples(self):
        """示例只进 description，不得污染 JSON Schema 的 parameters。"""
        native = self.registry.get_schemas_for_native_tools(["demo_tool"])
        params = native[0]["function"]["parameters"]
        self.assertEqual(set(params["properties"].keys()), {"url", "mode", "start_seconds", "max_frames"})
        for prop in params["properties"].values():
            self.assertNotIn("examples", prop)

    def test_examples_do_not_break_arg_validation(self):
        """示例里出现的参数组合必须能通过 registry 自己的校验。"""
        for example in self.EXAMPLES:
            sanitized, error = self.registry._sanitize_and_validate_args("demo_tool", dict(example))
            self.assertEqual(error, "", f"示例 {example} 被自己的校验拒绝: {error}")
            self.assertEqual(set(sanitized.keys()), set(example.keys()))


class ToolSchemaExamplesRobustnessTests(unittest.TestCase):
    """脏数据与预算上限。"""

    def _register(self, examples) -> AgentToolRegistry:
        registry = AgentToolRegistry()
        registry.register(
            ToolSchema(
                name="demo_tool",
                description="演示工具。",
                parameters={"type": "object", "properties": {}, "required": []},
                input_examples=examples,
            ),
            _noop_handler,
        )
        return registry

    def test_example_count_is_capped(self):
        registry = self._register([{"i": n} for n in range(10)])
        description = registry.get_schemas_for_native_tools(["demo_tool"])[0]["function"]["description"]
        rendered = [line for line in description.splitlines() if line.startswith("- ")]
        self.assertEqual(len(rendered), AgentToolRegistry._MAX_INPUT_EXAMPLES)

    def test_empty_dict_examples_render_nothing(self):
        registry = self._register([{}, {}])
        description = registry.get_schemas_for_native_tools(["demo_tool"])[0]["function"]["description"]
        self.assertEqual(description, "演示工具。")

    def test_unserializable_example_does_not_break_export(self):
        registry = self._register([{"bad": object()}, {"good": 1}])
        native = registry.get_schemas_for_native_tools(["demo_tool"])
        description = native[0]["function"]["description"]
        self.assertIn('"good": 1', description)
        self.assertNotIn("object at 0x", description)
        json.dumps(native, ensure_ascii=False)  # 整份 payload 仍可序列化


if __name__ == "__main__":
    unittest.main(verbosity=2)
