from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path

from core.admin import AdminEngine
from core.engine import YukikoEngine
from core.engine_types import EngineMessage
from core.prompt_navigator import (
    PromptNavigator,
    default_prompt_navigator_payload,
)
from core.trigger import TriggerEngine


class _Ctx:
    """PromptNavigator._preselect 只读 ctx 的结构字段。"""

    def __init__(self, message_text: str = "") -> None:
        self.message_text = message_text
        self.raw_segments: list[dict[str, object]] = []
        self.reply_media_segments: list[dict[str, object]] = []
        self.recent_media_artifact: dict[str, object] | None = None
        self.mentioned = False
        self.is_private = False
        self.at_other_user_ids: list[str] = []
        self.reply_to_user_id = ""


class EngineBotStrategyDirectiveTests(unittest.TestCase):
    """行为模式（闭嘴/安静/活跃）不再由本地词表旁路，改由模型经 admin_command 决定。

    被删除的 `_detect_bot_strategy_directive` 用三张中文词表在 `handle_message` 里
    抢在正常路由之前 return，模型完全看不到这轮消息。实测它既漏判同义说法
    （「你能不能歇会儿」「你话太多了，收敛一下」全部漏），又误命中转述句
    （「刚才张三让李四闭嘴」被判成 cold 并真的切换全局行为模式）。

    契约不变：超级管理员要求切换行为模式时，运行时策略必须真的改变。
    改变的只是**由谁做判断** —— 现在由模型读 bot_selfconfig 分区后调用 admin_command。
    """

    def _engine(
        self,
        *,
        super_users: list[str] | None = None,
        non_whitelist_mode: str = "silent",
    ) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {
            "bot": {"name": "YuKiKo", "nicknames": ["30秒"]},
            "admin": {
                "enable": True,
                "super_users": super_users or ["100"],
                "non_whitelist_mode": non_whitelist_mode,
            },
            "trigger": {"followup_reply_window_seconds": 30, "followup_max_turns": 2},
            "routing": {},
            "control": {},
        }
        engine.logger = logging.getLogger("test.yukiko.engine")
        engine._recent_directed_hints = {}
        engine.directed_grace_seconds = 90
        engine._async_init_done = True
        engine._seen_message_ids = OrderedDict()
        engine._seen_message_ids_max = 1024
        engine.trigger = TriggerEngine(
            trigger_config=engine.config["trigger"],
            bot_config=engine.config["bot"],
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine.admin = AdminEngine(engine.config, Path(tmp.name))

        def refresh_runtime_policy_components(*, reason: str = "") -> None:
            engine.refresh_reason = reason
            engine.trigger = TriggerEngine(
                trigger_config=engine.config["trigger"],
                bot_config=engine.config["bot"],
            )

        engine.refresh_runtime_policy_components = refresh_runtime_policy_components
        return engine

    def test_detects_directed_silence_control(self) -> None:
        """原断言：「闭嘴」命中词表返回 cold。现断言：本地词表检测器已不存在。

        同一场景（被 @ 的「闭嘴」）现在必须一路走到模型，由模型自己决定调不调
        admin_command，而不是在 handle_message 里被词表拦下。
        """

        engine = self._engine()

        self.assertFalse(hasattr(engine, "_detect_bot_strategy_directive"))
        self.assertFalse(hasattr(engine, "_handle_bot_strategy_directive"))

        # 「闭嘴」不是打字命令契约，因此不会被 admin 层当命令截走，只能交给模型。
        self.assertFalse(engine.admin.is_admin_command("闭嘴"))
        self.assertFalse(engine.admin.is_admin_command("你少说话"))

    def test_bot_strategy_capability_stays_reachable_through_menu(self) -> None:
        """删掉词表后，能力必须仍然可达：菜单里有 admin_command 且给出合法 arg 取值。"""

        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        section = nav.config.sections["bot_selfconfig"]

        self.assertIn("admin_command", section.tools)
        self.assertIn('command="behavior"', section.instructions)
        for arg in ("冷漠", "安静", "活跃", "默认"):
            self.assertIn(arg, section.instructions)

    def test_silence_request_is_model_routed_not_preselected(self) -> None:
        """「闭嘴」不得由本地打分直接落到管理分区 —— 分区选择必须 100% 由模型做。"""

        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        state = nav.initial_state(
            _Ctx("@YuKiKo 闭嘴一下"),
            ["think", "final_answer", "navigate_section", "admin_command"],
        )

        self.assertEqual(state.active_section, "general_chat")
        self.assertNotIn("bot_strategy_request", state.evidence)
        # 工具被硬门挡在分区外，模型只能先 navigate_section 才能拿到它。
        self.assertNotIn("admin_command", nav.scoped_tools(state))
        self.assertIn("bot_selfconfig", nav.render_system_block(state, nav.scoped_tools(state)))

    def test_super_admin_silence_control_updates_runtime_policy(self) -> None:
        """契约保留：超管切冷漠模式必须真的改运行时策略。

        改的只是入口 —— 以前是词表命中后 engine 自己拼命令，现在是模型调
        admin_command(command="behavior", arg="冷漠")，该工具内部拼出
        `/yuki behavior 冷漠`（core/agent_tools_admin.py:207）交给同一个 admin 层。
        这里直接断言那条命令串的效果，即工具落地后的真实结果。
        """

        engine = self._engine()
        engine.trigger.activate_session("group:1", "100", False)

        reply = asyncio.run(
            engine.admin.handle_command(
                text="/yuki behavior 冷漠",
                user_id="100",
                group_id=1,
                sender_role="member",
                engine=engine,
                api_call=None,
            )
        )

        self.assertIn("冷漠", reply)
        self.assertFalse(engine.config["trigger"]["ai_listen_enable"])
        self.assertFalse(engine.config["trigger"]["delegate_undirected_to_ai"])
        self.assertEqual(engine.refresh_reason, "behavior_mode:cold")

    def test_non_admin_silence_control_is_refused_by_permission_layer(self) -> None:
        """原名 ..._only_closes_current_session。

        旧行为：非超管说「闭嘴」时，engine 先 close_session 再静默丢弃，
        用户看不到任何反馈。新行为：权限在 admin 层判定并**明确告知**，
        运行时策略不变。close_session 这个副作用随词表一起消失 —— 见报告「风险」一节。
        """

        engine = self._engine(super_users=["999"])

        reply = asyncio.run(
            engine.admin.handle_command(
                text="/yuki behavior 冷漠",
                user_id="100",
                group_id=1,
                sender_role="member",
                engine=engine,
                api_call=None,
            )
        )

        self.assertIn("权限不足", reply)
        self.assertNotIn("ai_listen_enable", engine.config["trigger"])

    def test_directed_silence_control_no_longer_bypasses_whitelist_gate(self) -> None:
        """原断言：未加白群里「闭嘴」抢在白名单闸门之前生效（词表后门）。

        现断言：未加白 + silent 模式的群一律 ignore，没有任何词表后门。
        这不造成能力损失：silent 模式下机器人本来就完全不出声，没有「话多」可关；
        非 silent 模式且被 @ 时，打字命令 `/yuki behavior 冷漠` 与模型经
        admin_command 两条路都通。
        """

        engine = self._engine()
        message = EngineMessage(
            conversation_id="group:901738883",
            user_id="100",
            text="@30秒 闭嘴",
            mentioned=True,
            group_id=901738883,
            bot_id="200",
            message_id="m-1",
        )

        response = asyncio.run(engine.handle_message(message))

        self.assertEqual(response.action, "ignore")
        self.assertEqual(response.reason, "group_not_whitelisted")
        self.assertNotIn("ai_listen_enable", engine.config["trigger"])

    def test_typed_behavior_command_still_works_in_unwhitelisted_group(self) -> None:
        """未加白群的兜底是**打字命令契约**（用户主动敲的显式命令），它必须继续可用。"""

        engine = self._engine(non_whitelist_mode="hint")

        self.assertTrue(engine.admin.is_admin_command("/yuki behavior 冷漠"))
        reply = asyncio.run(
            engine.admin.handle_command(
                text="/yuki behavior 冷漠",
                user_id="100",
                group_id=901738883,
                sender_role="member",
                engine=engine,
                api_call=None,
            )
        )

        self.assertIn("冷漠", reply)
        self.assertFalse(engine.config["trigger"]["ai_listen_enable"])


class EngineAgentReplyDeliveryTests(unittest.TestCase):
    """承接被删的 `_should_block_undirected_agent_plain_reply` 的两条契约。

    原测试 `test_blocks_undirected_agent_plain_reply_from_listen_probe` 断言
    listen_probe 场景下模型产出的纯文本回复被**丢弃**。该后置否决已删除：实测它与
    回复内容无关（同场景换任何文本都照丢），属于代码事后否决模型。语义改由模型读
    general_chat 分区说明后用空文本 final_answer 表达，因此那条断言不再成立，
    对应的「群里别乱插话」契约转由分区说明承担（见下方 menu 断言）。

    原测试 `test_keeps_directed_or_artifact_agent_results` 断言被 @、回复 bot、
    带 artifact 三种情况必须放行 —— **这条契约仍然有效，而且现在更强**：
    没有任何闸门能丢掉回复，所以它从「闸门放行」升级为「不存在闸门」。
    """

    def test_no_code_path_can_discard_a_model_reply(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)

        self.assertFalse(
            hasattr(engine, "_should_block_undirected_agent_plain_reply")
        )

        source = Path("core/engine.py").read_text(encoding="utf-8")
        # 该 ignore 分支的 reason 字面值必须从生产代码里彻底消失（注释里提及不算）。
        code_lines = [
            line
            for line in source.splitlines()
            if not line.lstrip().startswith("#")
        ]
        self.assertNotIn(
            "agent_undirected_plain_reply_block", "\n".join(code_lines)
        )

    def test_group_silence_semantics_live_in_the_menu(self) -> None:
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        instructions = nav.config.sections["general_chat"].instructions

        # 「该沉默时用空文本 final_answer 收场」必须在分区说明里教过，
        # 否则删掉闸门就等于纯粹放开话痨。
        self.assertIn("final_answer", instructions)
        self.assertIn("空", instructions)


if __name__ == "__main__":
    unittest.main()
