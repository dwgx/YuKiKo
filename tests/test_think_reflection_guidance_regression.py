from __future__ import annotations

import unittest

from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_utility import _register_utility_tools


class ThinkReflectionGuidanceRegressionTests(unittest.TestCase):
    """`think` 原本只说「分析复杂问题时使用」，没告诉模型该检查什么。

    实测里反复出现两个可以靠自查避免的形态：
    - 同一工具带同一参数连调多次，直到重复守卫熔断
      （parse_video ×3、get_group_msg_history ×3、music_play ×5）
    - 带缺失必填参数直接调用（search_zhihu 缺 mode）

    加的是**检查角度的建议**，不是要填的表格 —— 代码不强制结构，
    想不想想、怎么想仍由模型定。真正的兜底仍在代码侧（重复守卫、
    schema 必填校验），这段 prompt 只是让模型有机会自己先发现。
    """

    def _think_description(self) -> str:
        registry = AgentToolRegistry()
        _register_utility_tools(registry)
        schema = registry.get_schema("think")
        self.assertIsNotNone(schema)
        return schema.description

    def test_suggests_checking_required_args_before_calling(self) -> None:
        text = self._think_description()
        self.assertIn("必填参数", text)

    def test_suggests_noticing_repeated_identical_calls(self) -> None:
        text = self._think_description()
        self.assertIn("同样的工具带同样的参数", text)

    def test_suggests_distinguishing_real_result_from_status_string(self) -> None:
        """实测「获取群 xxx 历史消息成功」这类只回状态、不带内容的返回
        会被模型当成拿到了结果。"""

        text = self._think_description()
        self.assertIn("只回了一句状态", text)

    def test_stays_advisory_not_a_mandatory_form(self) -> None:
        """不能变成代码替模型规定的填表流程 —— 那违背「完全信任模型」。"""

        text = self._think_description()
        self.assertIn("由你定", text)
        self.assertIn("不是要填的表格", text)

    def test_still_states_it_has_no_external_effect(self) -> None:
        """原描述里这条信息必须保留，否则模型会以为 think 会产生副作用。"""

        self.assertIn("不产生任何外部效果", self._think_description())

    def test_schema_shape_unchanged(self) -> None:
        """只改描述，参数契约不能动 —— 动了会让既有调用失效。"""

        registry = AgentToolRegistry()
        _register_utility_tools(registry)
        schema = registry.get_schema("think")
        self.assertEqual(schema.parameters["required"], ["thought"])
        self.assertEqual(sorted(schema.parameters["properties"]), ["thought"])

    def test_description_cost_stays_modest(self) -> None:
        """think 出现在每个分区、每一回合，描述膨胀是全局成本。

        实测单回合最大分区总计约 2700 token，这段控制在 ~60 token 以内。
        """

        self.assertLess(len(self._think_description()), 320)


if __name__ == "__main__":
    unittest.main()
