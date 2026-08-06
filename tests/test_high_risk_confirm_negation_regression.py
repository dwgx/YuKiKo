"""高风险二次确认不能把「我不确认」读成同意 —— 那让闸门在拒绝时反而放行。

## 缺陷（2026-08-06 子 agent 审计发现，实测复现）

`_is_confirmation_text` 的判据是**无锚点子串匹配**：

```python
return any(cue in content for cue in self.high_risk_confirm_cues)
#            confirm_cues 默认 = ("确认", "确认执行", "继续执行", "确定执行", "yes")
```

而「我不确认」「别确认」「无法确认」「不要确认」**都包含「确认」**。
`cancel_cues`（"取消"/"算了"/"停止"/"不执行"/"撤销"）里没有这些词形，
所以 `_guard_high_risk_tool_call` 里「先判取消、再判确认」的顺序也拦不住。

实测四种说法全部走到执行分支：

```
文本         判为取消?  判为确认?  实际后果
我不确认      False     True      **执行封禁**
别确认        False     True      **执行封禁**
无法确认      False     True      **执行封禁**
不要确认      False     True      **执行封禁**
```

二次确认是破坏性操作（封禁 / 踢人 / 改配置）的最后一道闸门。它在用户
**明确说不**的时候放行，比没有这道闸门更危险 —— 用户以为自己拒绝了。

## 修法为什么不是往 cancel_cues 里加词

那是同一个脆弱模式再来一遍：漏一种说法就再反转一次。
改成判**语法否定**：中文否定词是封闭的小集合（不/别/勿/非/无法/未/没/莫），
且必须紧邻确认词（允许中间夹一个情态字：不**要**确认、不**能**确认）。

关键是「紧邻」这个约束 —— 它让「确认取消订单」仍然算确认
（「取消」在确认词之后，不是否定它），也让「我要确认」不受影响
（剥掉情态字「要」剩「我」，不是否定词）。
"""

from __future__ import annotations

import unittest

from core.agent import AgentLoop

_CONFIRM_CUES = ("确认", "确认执行", "继续执行", "确定执行", "yes")
_CANCEL_CUES = ("取消", "算了", "停止", "不执行", "撤销")


def _loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.high_risk_confirm_cues = _CONFIRM_CUES  # type: ignore[attr-defined]
    loop.high_risk_cancel_cues = _CANCEL_CUES  # type: ignore[attr-defined]
    return loop


class NegatedConfirmationIsNotConsentTests(unittest.TestCase):
    """审计报的原始四种说法，以及同族变体。"""

    def setUp(self) -> None:
        self.loop = _loop()

    def test_audit_reported_phrasings_are_not_consent(self) -> None:
        for text in ("我不确认", "别确认", "无法确认", "不要确认"):
            with self.subTest(text=text):
                self.assertFalse(
                    self.loop._is_confirmation_text(text, None),
                    f"{text!r} 被读成同意 —— 破坏性操作会在用户拒绝时执行",
                )

    def test_other_negation_forms_are_not_consent(self) -> None:
        for text in (
            "不能确认",
            "不用确认",
            "不准确认",
            "不可以确认",
            "未确认",
            "没确认",
            "勿确认",
            "莫确认",
            "非确认",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.loop._is_confirmation_text(text, None), text)

    def test_negated_other_cues_are_not_consent(self) -> None:
        """否定判定要对每个 cue 生效，不只是「确认」。"""

        for text in ("不要继续执行", "别确定执行", "不确认执行"):
            with self.subTest(text=text):
                self.assertFalse(self.loop._is_confirmation_text(text, None), text)


class GenuineConfirmationStillWorksTests(unittest.TestCase):
    """反向：不能为了堵否定把正常确认也堵死。

    这道闸门堵死的后果是「管理员永远封不了人」，同样是故障。
    """

    def setUp(self) -> None:
        self.loop = _loop()

    def test_plain_confirmations_pass(self) -> None:
        for text in ("确认", "确认执行", "继续执行", "确定执行", "yes"):
            with self.subTest(text=text):
                self.assertTrue(self.loop._is_confirmation_text(text, None), text)

    def test_confirmations_with_surrounding_words_pass(self) -> None:
        for text in ("好的确认", "确认，执行吧", "嗯确认", "行确认吧", "我要确认", "可以确认"):
            with self.subTest(text=text):
                self.assertTrue(self.loop._is_confirmation_text(text, None), text)

    def test_negation_after_the_cue_does_not_block(self) -> None:
        """「确认取消订单」里的「取消」在确认词之后，不否定它。

        这条钉住「紧邻」这个约束 —— 否定判定不能退化成「整句里有没有否定词」。
        """

        self.assertTrue(self.loop._is_confirmation_text("确认取消订单", None))

    def test_confirm_token_path_is_unaffected(self) -> None:
        """use_confirm_token 打开时走 token 比对，不经过否定判定。"""

        pending = {"confirm_token": "A7X9"}
        self.assertTrue(self.loop._is_confirmation_text("a7x9", pending))


class NegationHelperSemanticsTests(unittest.TestCase):
    """`_cue_is_negated` 的边界，防止后来的人改坏。"""

    def test_multiple_occurrences_need_all_negated(self) -> None:
        """一句里出现两次确认词，只要有一处没被否定就算确认。"""

        self.assertFalse(
            AgentLoop._cue_is_negated("不确认还是确认", "确认"),
            "第二处「确认」没被否定，整句应算确认",
        )

    def test_all_occurrences_negated_means_negated(self) -> None:
        self.assertTrue(AgentLoop._cue_is_negated("不确认也别确认", "确认"))

    def test_cue_at_string_start_is_not_negated(self) -> None:
        self.assertFalse(AgentLoop._cue_is_negated("确认执行", "确认"))

    def test_subject_before_cue_is_not_a_negator(self) -> None:
        """「我要确认」剥掉情态字剩「我」，不是否定词。"""

        self.assertFalse(AgentLoop._cue_is_negated("我要确认", "确认"))


class CancellationPathIsUnchangedTests(unittest.TestCase):
    """取消判定不在本次改动范围内，钉住它没被顺手改坏。"""

    def setUp(self) -> None:
        self.loop = _loop()

    def test_cancel_cues_still_cancel(self) -> None:
        for text in ("取消", "算了", "停止", "不执行", "撤销"):
            with self.subTest(text=text):
                self.assertTrue(self.loop._is_cancellation_text(text, None), text)

    def test_unrelated_text_is_neither(self) -> None:
        for text in ("在吗", "放首歌", "这是什么"):
            with self.subTest(text=text):
                self.assertFalse(self.loop._is_cancellation_text(text, None))
                self.assertFalse(self.loop._is_confirmation_text(text, None))


if __name__ == "__main__":
    unittest.main()
