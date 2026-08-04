"""严格模式下 router 不得改写模型给出的 action（MIGRATION_TODO A4）。

`_parse_decision` 里有两条覆盖分支：看到图片段就把 action 强制成 search、
并硬塞 `method="media.analyze_image"`。它们靠结构信号触发（不是关键词），
但**替模型决定了工具**。

严格模式下这件事已经由 PromptNavigator 承担：`_preselect` 读同一个 raw_segments
图片段，把起始分区落在 multimodal_media、把 analyze_image 放进可见工具，并把
`image_url` / `message_or_reply_media` 作为结构信号交给模型判断——模型可以否决它。
在 router 里强制改写等于把刚从 router prompt 删掉的工具选择又加回来，且模型无法申诉。

关掉严格模式时旧行为必须回来，否则没有 Navigator 的部署会失去这条兜底。
"""
from __future__ import annotations

import unittest

from core.router import RouterDecision, RouterEngine, RouterInput
from core.system_prompts import SystemPromptRelay


class _EnabledModelClient:
    """模型可用，所以不会进 no-model 兜底；本测试只关心 _parse_decision 的覆盖分支。"""

    enabled = True


def _build_router() -> RouterEngine:
    return RouterEngine(
        config={"routing": {"mode": "ai_full"}, "bot": {"name": "YuKiKo", "nicknames": []}},
        personality=object(),
        model_client=_EnabledModelClient(),
    )


def _payload(**overrides: object) -> RouterInput:
    base = dict(
        text=(
            "MULTIMODAL_EVENT_AT user mentioned bot and sent multimodal message: "
            "image:[image]\n这是什么"
        ),
        conversation_id="group:10001",
        user_id="10086",
        user_name="tester",
        trace_id="trace-router-override-scope",
        mentioned=True,
        is_private=False,
        media_summary=["image:[image]"],
    )
    base.update(overrides)
    return RouterInput(**base)


def _fallback() -> RouterDecision:
    return RouterDecision(
        should_handle=True, action="reply", reason="test_fallback", confidence=0.5
    )


# 模型返回「接，但只是普通回复」——严格模式下这就是它被允许输出的全部。
_MODEL_SAYS_REPLY = {
    "should_handle": True,
    "action": "reply",
    "reason": "model_decided_reply",
    "confidence": 0.8,
    "reply_style": "short",
}


class RouterOverrideScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = _build_router()
        self.assertTrue(
            SystemPromptRelay._strict_navigator_enabled(),
            "本仓库默认 strict_tool_routing=true；若被改则本测试前提不成立",
        )

    def test_strict_mode_keeps_model_action(self) -> None:
        decision = self.router._parse_decision(
            dict(_MODEL_SAYS_REPLY), _fallback(), [], [], _payload()
        )

        self.assertEqual(decision.action, "reply", "严格模式下不应把 reply 改写成 search")
        self.assertNotEqual(decision.tool_args.get("method"), "media.analyze_image")

    def test_strict_mode_does_not_inject_a_tool(self) -> None:
        """不只是 action 不变，也不该悄悄塞进 method / method_args。"""
        decision = self.router._parse_decision(
            dict(_MODEL_SAYS_REPLY), _fallback(), [], [], _payload()
        )

        args = decision.tool_args if isinstance(decision.tool_args, dict) else {}
        method = str(args.get("method", "") or "")
        self.assertEqual(method, "", f"严格模式下 tool_args 不该带工具指定，实际={method!r}")

    def test_model_ignore_is_respected_with_image_present(self) -> None:
        """有图但模型判定不该接：结构信号不是命令，不能把 ignore 抬成 search。"""
        data = dict(_MODEL_SAYS_REPLY)
        data["should_handle"] = False
        data["action"] = "ignore"

        decision = self.router._parse_decision(data, _fallback(), [], [], _payload())

        self.assertEqual(decision.action, "ignore")
        self.assertFalse(decision.should_handle)

    def test_legacy_override_returns_when_strict_mode_is_off(self) -> None:
        """关掉严格模式时旧兜底必须回来，否则无 Navigator 的部署会失去它。"""
        original = SystemPromptRelay._strict_navigator_enabled
        try:
            SystemPromptRelay._strict_navigator_enabled = staticmethod(lambda: False)
            decision = self.router._parse_decision(
                dict(_MODEL_SAYS_REPLY), _fallback(), [], [], _payload()
            )
            self.assertEqual(decision.action, "search")
            self.assertEqual(decision.tool_args.get("method"), "media.analyze_image")
        finally:
            SystemPromptRelay._strict_navigator_enabled = original


if __name__ == "__main__":
    unittest.main()
