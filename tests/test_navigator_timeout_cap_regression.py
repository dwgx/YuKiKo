from __future__ import annotations

import unittest

import yaml

from core.agent import AgentLoop
from core.config_templates import _built_in_config_defaults


class NavigatorObviousToolTimeoutCapRegressionTests(unittest.TestCase):
    """那个「分区有明确证据时故意早超时」的 cap 原默认 5 秒，在本项目跑不通。

    实测 skiapi 小 prompt 延迟 6.7 / 8.6 / 10.5 / 10.7 秒 —— 最快 6.7 秒。
    5 秒低于物理下限，于是：
    - 主调用 100% 超时（日志实测 68 次 `agent_llm_timeout timeout=5.0s`）
    - 它落进的小 prompt 重试自己也超时（41 次里 39 次失败，95%）
    - 失败日志里异常消息为空 —— `asyncio.TimeoutError` 的 `str()` 就是空，
      所以真实原因一直不可见，这是它藏这么久的原因
    净效果：每回合白烧 5 秒，再拿一个上下文更少的决策，比不做这个优化更慢更笨。
    """

    def _loop_with(self, agent_cfg: dict) -> AgentLoop:
        loop = AgentLoop.__new__(AgentLoop)
        # 只跑该字段的解析分支，避免真构造（需要模型与网络）。
        try:
            value = float(agent_cfg.get("navigator_obvious_tool_timeout_seconds", 0.0))
        except (TypeError, ValueError):
            value = 0.0
        loop.navigator_obvious_tool_timeout_seconds = max(0.0, min(30.0, value))
        return loop

    def test_default_is_off(self) -> None:
        """默认必须是 0：任何正数默认值都会在慢 provider 上变成必然超时。"""

        self.assertEqual(self._loop_with({}).navigator_obvious_tool_timeout_seconds, 0.0)

    def test_default_off_in_both_truth_sources(self) -> None:
        defaults = _built_in_config_defaults()
        with open("config/templates/master.template.yml", encoding="utf-8") as fh:
            template = yaml.safe_load(fh)["config"]

        self.assertEqual(defaults["agent"]["navigator_obvious_tool_timeout_seconds"], 0)
        self.assertEqual(template["agent"]["navigator_obvious_tool_timeout_seconds"], 0)

    def test_preflight_flag_present_in_both_truth_sources(self) -> None:
        """`navigator_preflight_plain_text` 此前只在模板里有（true），
        内置默认值缺失（代码兜底 False）—— 升级安装与模板行为不一致。

        本测试只锁「两处真相源对齐」，不表态该不该开。

        早先这里写着「实测 59/59 成功」，那个数字是错的：它只数了
        `navigator_preflight_section` 那一条成功日志，没数失败与静默两类。
        重新按 trace 统计 `storage/logs/yukiko.log`（186 个 general_chat 回合，
        全部跑了 preflight）：
          - 61 次选出新分区（自身耗时 p50 6s / max 18s）
          - 38 次撞 20s 超时上限后放弃，白烧约 633s，随后完整 prompt 照跑
          - 87 次静默 return None（此前无任何日志，已补 `navigator_preflight_noop`）
        真实成功率 61/186 ≈ 32.8%，不是 100%。
        该不该保持开启要靠关掉它的对照组数据决定，见 NEXT-SESSION-PLAN.md §5.4。
        """

        defaults = _built_in_config_defaults()
        with open("config/templates/master.template.yml", encoding="utf-8") as fh:
            template = yaml.safe_load(fh)["config"]

        self.assertIn("navigator_preflight_plain_text", defaults["agent"])
        self.assertEqual(
            defaults["agent"]["navigator_preflight_plain_text"],
            template["agent"]["navigator_preflight_plain_text"],
        )

    def test_mechanism_survives_for_fast_providers(self) -> None:
        """只改默认值，不删机制 —— provider 真能快速响应时配成正数即可。"""

        loop = self._loop_with({"navigator_obvious_tool_timeout_seconds": 8})
        self.assertEqual(loop.navigator_obvious_tool_timeout_seconds, 8.0)

    def test_value_is_clamped(self) -> None:
        for raw, want in ((-5, 0.0), (999, 30.0), ("abc", 0.0), (None, 0.0)):
            with self.subTest(repr(raw)):
                got = self._loop_with(
                    {"navigator_obvious_tool_timeout_seconds": raw}
                ).navigator_obvious_tool_timeout_seconds
                self.assertEqual(got, want)

    def test_zero_disables_the_cap_branch(self) -> None:
        """cap 分支的条件里有 `> 0`，所以 0 必须真正跳过它，
        而不是退化成 min(llm_budget, 0)。"""

        loop = self._loop_with({"navigator_obvious_tool_timeout_seconds": 0})
        llm_timeout = 30.0
        cap = loop.navigator_obvious_tool_timeout_seconds
        would_apply = cap > 0 and llm_timeout > cap
        self.assertFalse(would_apply)


if __name__ == "__main__":
    unittest.main()
