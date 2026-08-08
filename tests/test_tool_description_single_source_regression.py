"""架构收敛 D8：工具描述双源回归测试。

评审实锤：同一能力（如 fetch_webpage / parse_video / analyze_image）在 agent
工具层（ToolSchema.description）和 router 侧公开函数（core/tools_*.py 的
docstring）各写一份描述，容易漂移。收敛方案：描述文本只以 core/tools_*.py 的
模块级 `*_DESCRIPTION` 常量为单一真相源，agent_tools_*.py 的 ToolSchema 引用它，
公开函数 docstring 只引用常量名而不是复制全文。

本测试断言：
1. 每个 call-through 工具的 ToolSchema.description 与对应常量完全一致。
2. 底层公开函数的 docstring 引用该常量名（而非另写一份描述）。
"""
from __future__ import annotations

import unittest

from core.agent_tools_media import _register_media_tools
from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_web import _register_ai_method_tools

# (agent 工具名, 描述常量名)。常量与函数都从 core/tools_*.py 取，schema 从注册表取。
PAIRS: tuple[tuple[str, str], ...] = (
    ("fetch_webpage", "FETCH_WEBPAGE_DESCRIPTION"),
    ("github_search", "GITHUB_SEARCH_DESCRIPTION"),
    ("github_readme", "GITHUB_README_DESCRIPTION"),
    ("douyin_search", "DOUYIN_SEARCH_DESCRIPTION"),
    ("parse_video", "PARSE_VIDEO_DESCRIPTION"),
    ("analyze_video", "ANALYZE_VIDEO_DESCRIPTION"),
    ("analyze_image", "ANALYZE_IMAGE_DESCRIPTION"),
)

_CONSTANT_IMPORTS: dict[str, tuple[str, str]] = {
    "FETCH_WEBPAGE_DESCRIPTION": ("core.tools_search", "browser_fetch_url"),
    "GITHUB_SEARCH_DESCRIPTION": ("core.tools_github", "browser_github_search"),
    "GITHUB_README_DESCRIPTION": ("core.tools_github", "browser_github_readme"),
    "PARSE_VIDEO_DESCRIPTION": ("core.tools_video", "browser_resolve_video"),
    "DOUYIN_SEARCH_DESCRIPTION": ("core.tools_video", "douyin_search_video"),
    "ANALYZE_VIDEO_DESCRIPTION": ("core.tools_video", "video_analyze"),
    "ANALYZE_IMAGE_DESCRIPTION": ("core.tools_vision", "media_analyze_image"),
}


class ToolDescriptionSingleSourceRegressionTests(unittest.TestCase):
    """工具描述单一真相源：schema 与 router 侧 docstring 必须同源。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = AgentToolRegistry()
        _register_ai_method_tools(
            cls.registry,
            {"search": {"tool_interface": {"github_enable": True}}},
        )
        _register_media_tools(cls.registry, None, {})

    def test_schema_description_equals_single_source_constant(self) -> None:
        """每个 call-through 工具的 ToolSchema.description == 底层模块常量。"""
        import importlib

        for tool_name, const_name in PAIRS:
            module_name, _func_name = _CONSTANT_IMPORTS[const_name]
            constant = getattr(importlib.import_module(module_name), const_name)
            schema = self.registry._schemas.get(tool_name)
            with self.subTest(tool=tool_name):
                self.assertIsNotNone(schema, f"{tool_name} 未注册")
                self.assertTrue(constant.strip(), f"{const_name} 为空")
                self.assertEqual(schema.description, constant)

    def test_underlying_function_docstring_references_constant(self) -> None:
        """router 侧公开函数 docstring 引用同一常量名，而非复制一份描述。"""
        import importlib

        for _tool_name, const_name in PAIRS:
            module_name, func_name = _CONSTANT_IMPORTS[const_name]
            func = getattr(importlib.import_module(module_name), func_name)
            doc = func.__doc__ or ""
            with self.subTest(func=f"{module_name}.{func_name}"):
                self.assertIn(const_name, doc, "docstring 应引用常量名保持单一真相源")

    def test_registry_contains_all_call_through_tools(self) -> None:
        """这 7 个工具都必须注册，防止收敛时把注册误删。"""
        registered = set(self.registry._schemas)
        for tool_name, _const_name in PAIRS:
            with self.subTest(tool=tool_name):
                self.assertIn(tool_name, registered)


if __name__ == "__main__":
    unittest.main()
