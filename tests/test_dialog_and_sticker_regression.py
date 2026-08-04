from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from core.agent import AgentContext, AgentLoop
from core.agent_tools_registry import AgentToolRegistry
from core.agent_tools_utility import (
    _handle_send_emoji,
    _handle_send_face,
    register_sticker_tools,
)
from core.engine import EngineMessage, YukikoEngine
from core.prompt_navigator import PromptNavigator, default_prompt_navigator_payload


class _DummyStickerManager:
    face_count = 0
    emoji_count = 3
    # turn_goal=send 放行后会走到查库路径（core/agent_tools_utility.py:340/384）。
    # learned_count 在真实类里是「已学会」的子集（core/sticker.py:1049），这里三张全算已学会。
    learned_count = 3

    def find_emoji(self, query: str, strict: bool = False) -> list:
        # 本测试只验「本地否决不再触发」，放行后查不到匹配即可证明；
        # 返回空表让工具落到「没有找到匹配的表情包」，而不是 wrong_tool_for_manage_goal。
        return []

    def last_learned_emoji(self, source_user: str = ""):
        if source_user == "10001":
            return (
                "add/10001/demo.png",
                SimpleNamespace(
                    description="猫猫震惊",
                    category="反应",
                    tags=["猫猫", "震惊", "meme"],
                ),
            )
        return (
            "add/99999/global.png",
            SimpleNamespace(
                description="全局最新",
                category="搞笑",
                tags=["最新"],
            ),
        )

    def face_list_for_prompt(self) -> str:
        return ""


class _DummyMemory:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def add_message(self, **kwargs):  # type: ignore[no-untyped-def]
        self.rows.append(kwargs)


class DialogAndStickerRegressionTests(unittest.TestCase):
    def test_user_message_starts_with_explicit_current_speaker_anchor(self) -> None:
        loop = AgentLoop.__new__(AgentLoop)
        loop._rebuild_query_with_context = lambda text, ctx: text  # type: ignore[attr-defined]
        loop._build_napcat_event_anchor = lambda ctx: ""  # type: ignore[attr-defined]

        ctx = AgentContext(
            conversation_id="group:1:user:10001",
            user_id="10001",
            user_name="妈妈",
            group_id=1,
            bot_id="bot",
            is_private=False,
            mentioned=True,
            message_text="@妈妈 meme更新了吗",
            sender_role="admin",
            reply_to_user_id="20002",
            reply_to_user_name="风风",
            reply_to_text="刚才那张学会了吗",
            sticker_manager=_DummyStickerManager(),
        )

        payload = AgentLoop._build_user_message(loop, ctx)

        self.assertIn("[当前说话人: 妈妈(QQ:10001) | role=admin]", payload)
        self.assertIn("[用户在回复: 风风(QQ:20002)", payload)

    def test_sticker_management_turn_is_not_treated_as_explicit_send(self) -> None:
        """管理轮不发表情这个契约保留，但改由模型显式声明 turn_goal，不再由词表读原文推翻模型。"""
        management_text = "meme更新了吗，刚学的那个表情包现在有了吗"
        send_text = "把刚学的表情包发出来看看"

        # 契约一：模型声明本轮是在维护/查询表情库时，发送类工具拒绝执行并指回管理工具。
        for handler in (_handle_send_face, _handle_send_emoji):
            result = asyncio.run(
                handler(
                    {"query": "开心", "turn_goal": "manage"},
                    {
                        "message_text": management_text,
                        "sticker_manager": _DummyStickerManager(),
                    },
                )
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "wrong_tool_for_manage_goal")
            self.assertIn("learn_sticker", result.display)

        # 契约二：不声明 turn_goal 时工具要求补参数，而不是自己猜。
        missing = asyncio.run(
            _handle_send_emoji(
                {"query": "开心"},
                {"message_text": send_text, "sticker_manager": _DummyStickerManager()},
            )
        )
        self.assertFalse(missing.ok)
        self.assertEqual(missing.error, "missing_arg:turn_goal")

        # 反转的部分：原始用户文本不再影响判定。同一句管理类原文，
        # 只要模型声明 turn_goal=send，就不再被本地否决（放行到后续查库逻辑）。
        allowed = asyncio.run(
            _handle_send_emoji(
                {"query": "开心", "turn_goal": "send"},
                {
                    "original_message_text": management_text,
                    "message_text": management_text,
                    "sticker_manager": _DummyStickerManager(),
                },
            )
        )
        self.assertNotEqual(allowed.error, "wrong_tool_for_manage_goal")
        self.assertNotIn("当前是在学习或查询表情包状态", allowed.display)

    def test_sticker_send_tools_require_model_declared_turn_goal(self) -> None:
        """send-vs-manage 的区分下沉成 schema 必填参数，模型在菜单里看得到、可申诉。"""
        registry = AgentToolRegistry()
        register_sticker_tools(registry, model_client=None)

        for tool in ("send_face", "send_emoji", "send_sticker"):
            schema = registry._schemas[tool]
            params = schema.parameters["properties"]
            self.assertIn("turn_goal", params, f"{tool} 缺少 turn_goal 参数")
            self.assertEqual(params["turn_goal"]["enum"], ["send", "manage"])
            self.assertIn("turn_goal", schema.parameters["required"])

            _, err = registry._sanitize_and_validate_args(tool, {"query": "开心"})
            self.assertEqual(err, "missing_required_args:turn_goal")

        # 分区同时承载两类目标，模型才有可能在这两者之间做选择。
        nav = PromptNavigator.from_payload(default_prompt_navigator_payload())
        section_tools = nav.config.sections["sticker_emoji"].tools
        for tool in ("send_face", "send_emoji", "send_sticker"):
            self.assertIn(tool, section_tools)
        for tool in ("learn_sticker", "correct_sticker", "list_emojis"):
            self.assertIn(tool, section_tools)

    def test_agent_side_effect_memory_keeps_sticker_description(self) -> None:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.config = {"bot": {"allow_memory": True, "name": "YuKiKo"}}
        engine.memory = _DummyMemory()

        message = EngineMessage(
            conversation_id="group:1:user:10001",
            user_id="10001",
            user_name="妈妈",
            text="学一下这张表情",
            timestamp=datetime.now(timezone.utc),
        )
        agent_result = SimpleNamespace(
            steps=[
                {
                    "tool": "learn_sticker",
                    "display": "",
                    "data": {"description": "猫猫震惊", "key": "add/10001/demo.png"},
                },
                {
                    "tool": "send_emoji",
                    "display": "",
                    "data": {"desc": "猫猫震惊", "key": "add/10001/demo.png"},
                },
            ]
        )

        YukikoEngine._record_agent_side_effects(engine, message, agent_result)

        self.assertEqual(len(engine.memory.rows), 1)
        content = engine.memory.rows[0]["content"]
        self.assertIn("学习了表情包", content)
        self.assertIn("发送了表情包", content)
        self.assertIn("猫猫震惊", content)


if __name__ == "__main__":
    unittest.main()
