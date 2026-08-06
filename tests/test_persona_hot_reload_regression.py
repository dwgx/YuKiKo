"""`/yukibot` 热重载必须把新人格底稿送到 agent 路。

`persona_text` 在 core/engine.py:183 是**按值**传进 AgentLoop 构造函数的，
而 `reload_config()` 不重建 AgentLoop、只调 `refresh_runtime_config()`。
原先那个方法不碰 persona_text，于是热重载之后：
  - router 路、thinking 路拿到了新人格（它们每次从 self.personality 读）
  - **agent 路（线上每回合都走的那条）还在用进程启动时那份旧稿**

后果是改 config/personas/yukiko.md 后日志报「配置热重载完成」，
看起来生效了，实际对主路径零效果 —— 上一轮的反口癖改动和本轮的
「被骂反击作用域收窄」都会静默失效。这条之前没有任何测试守着。
"""

from __future__ import annotations

import inspect
import unittest

from core.agent import AgentLoop
from core.engine import YukikoEngine


def _bare_loop(persona: str) -> AgentLoop:
    """手搭一个 AgentLoop —— 真构造要模型和网络（本仓惯例，见 CLAUDE.md）。

    refresh_runtime_config 里会调 _cleanup_pending_high_risk，
    所以那几个属性必须先备好，否则测的是脚手架而不是行为。
    """

    loop = AgentLoop.__new__(AgentLoop)
    loop.persona_text = persona
    loop.config = {}
    loop.super_users = set()
    loop._pending_high_risk_actions = {}
    loop._pending_high_risk_key = None
    return loop


class RefreshRuntimeConfigCarriesPersonaTests(unittest.TestCase):
    def test_accepts_a_persona_text_argument(self) -> None:
        params = inspect.signature(AgentLoop.refresh_runtime_config).parameters
        self.assertIn(
            "persona_text",
            params,
            "reload 没法把新人格送进 agent 路，改人格底稿对主路径无效",
        )

    def test_new_persona_replaces_the_old_one(self) -> None:
        loop = _bare_loop("旧稿\n- 被骂/攻击：可以反击、傲娇、装委屈，不一味道歉")
        loop.refresh_runtime_config(
            {"agent": {}},
            persona_text="新稿\n- 被直接骂/当面攻击你本人：可以反击",
        )
        self.assertIn("被直接骂", loop.persona_text)
        self.assertNotIn("被骂/攻击：可以反击", loop.persona_text)

    def test_omitting_persona_keeps_the_current_one(self) -> None:
        """只改数值配置的调用方不该被迫传人格。"""

        loop = _bare_loop("保持不变的稿")
        loop.refresh_runtime_config({"agent": {}})
        self.assertEqual(loop.persona_text, "保持不变的稿")

    def test_explicit_empty_string_clears_it(self) -> None:
        """None = 不改；空串 = 真的清空。两者语义必须分开。"""

        loop = _bare_loop("有稿")
        loop.refresh_runtime_config({"agent": {}}, persona_text="")
        self.assertEqual(loop.persona_text, "")


class ReloadConfigWiringTests(unittest.TestCase):
    def test_reload_config_passes_persona_text_to_the_agent(self) -> None:
        """读 reload_config 的源码确认它真的传了 —— 不传就是静默失效，没有任何报错。"""

        source = inspect.getsource(YukikoEngine.reload_config)
        self.assertIn("refresh_runtime_config", source)
        self.assertIn(
            "persona_text",
            source,
            "reload_config 调 refresh_runtime_config 时没传 persona_text",
        )

    def test_agent_system_prompt_reads_the_instance_copy(self) -> None:
        """确认 agent 的系统提示词读的是 self.persona_text 这份副本 ——
        如果哪天改成每次从 engine 读，本文件这套断言就该跟着简化。"""

        source = inspect.getsource(AgentLoop._build_system_prompt)
        self.assertIn("self.persona_text", source)


if __name__ == "__main__":
    unittest.main()
