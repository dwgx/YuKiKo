"""滞后回复只在**真被打断**时丢弃 —— 不能因为对方多说了一句就整条丢掉。

## 实测数据（2026-08-06，业主的群，storage/logs/yukiko.log 15126 行）

```
实际发出的回复                 243
已生成但被判过期丢弃            87   <- 占生成量 26%
触发门直接忽略（不理人）        232
队列取消（新消息顶掉旧回合）    578
```

原来的丢弃条件是 `same_user_newer_turn or cancel_newer_turn` ——
**同一个人在 bot 思考期间又说了一句话，就丢掉整条已写好的回答。**

根因是两个时间尺度不匹配：

* 群里消息间隔 3-4 秒（日志实测 05:52:17 → :20 → :24）
* 回合 p50 27 秒

所以「思考期间对方又说了一句」几乎是必然事件，不是打断信号。
业主看到的现象就是「机器人老是不理人」—— 它明明决定要回、也写完了，
然后一个字没发。日志里能看到被丢掉的原话，例如 `text=帝王哩，这条…`。

## 改成什么

只保留 `cancel_newer_turn`：新消息里带明确的取消 / 更正意图
（`_looks_like_cancel_previous_request`：打断 / 更正 / 不是这个 / 重新回答 …）
才丢。其余照发，代价是偶尔会晚 20-30 秒回一句。

本文件两个方向都钉：该丢的要丢，不该丢的不能丢。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app_helpers import _looks_like_cancel_previous_request

_APP = Path("app.py")


def _drop_condition_source() -> str:
    """取出 `if stale_plain_reply and ...:` 那一行的判定表达式源码。"""

    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        if "stale_plain_reply" in test_src:
            return test_src
    return ""


class DropConditionShapeTests(unittest.TestCase):
    """用 AST 读判定条件本身 —— 这条策略没有别的可观测出口。"""

    def test_drop_condition_is_findable(self) -> None:
        self.assertTrue(
            _drop_condition_source(),
            "在 app.py 里找不到 stale_plain_reply 的判定分支 —— "
            "代码形状变了，本守卫需要跟着改",
        )

    def test_a_newer_message_from_the_same_user_alone_does_not_drop(self) -> None:
        """核心回归：`same_user_newer_turn` 不能再单独触发丢弃。

        它是「对方又说了一句」，在 3-4 秒消息间隔 + 27 秒回合的群里几乎恒为真，
        当丢弃条件用就等于「基本不发言」。
        """

        condition = _drop_condition_source()
        self.assertNotIn(
            "same_user_newer_turn",
            condition,
            "滞后丢弃仍然把「同一人又发了新消息」当作打断信号 —— "
            f"实测因此丢掉 87 条已写好的回复。当前条件: {condition}",
        )

    def test_explicit_cancel_still_drops(self) -> None:
        """反向：真被打断时仍要丢，别把这条也一起放开了。"""

        condition = _drop_condition_source()
        self.assertIn(
            "cancel_newer_turn",
            condition,
            f"取消 / 更正意图不再触发丢弃了，会答非所问。当前条件: {condition}",
        )


class CancelIntentDetectionTests(unittest.TestCase):
    """丢弃现在完全依赖这个判定，所以它的行为要钉住。"""

    def test_explicit_correction_is_detected(self) -> None:
        for text in (
            "不是这个",
            "更正一下",
            "重新回答",
            "我说的是另一首",
            "打断一下",
            "忽略上一条",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    _looks_like_cancel_previous_request(text),
                    f"{text!r} 应被识别为打断 —— 否则会答非所问",
                )

    def test_ordinary_follow_up_chatter_is_not_a_cancel(self) -> None:
        """这些是业主群里被丢掉的那类消息，必须判为「不是打断」。"""

        for text in (
            "他叫你碳基",
            "你骂他吧",
            "好吧",
            "可以可以",
            "那很厉害了",
            "怎么搞",
            "到时候丢个大哥大视频",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    _looks_like_cancel_previous_request(text),
                    f"{text!r} 被误判成打断 —— 上一条回复会被丢掉",
                )

    def test_empty_text_is_not_a_cancel(self) -> None:
        for text in ("", "   "):
            self.assertFalse(_looks_like_cancel_previous_request(text))


if __name__ == "__main__":
    unittest.main()
