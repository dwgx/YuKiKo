"""Phase 4b：回顾意图检测回归测试。

锁一件事（对应 docs/zh-CN/RECONSTRUCTION-BLUEPRINT.md §4.1（3）车道 2）：
只有「显式回溯意图」才升级到车道 2 检索。本模块识别该意图，纯结构正则。
"""
from __future__ import annotations

import unittest

from core.recall_intent import looks_like_recall_intent


class RecallIntentTests(unittest.TestCase):
    def test_chinese_recall_phrases_are_detected(self) -> None:
        phrases = [
            "我记得你说过这件事",
            "你之前说过的那个",
            "上次说好的方案",
            "以前你不是这么说的",
            "回想一下你上次说的",
            "回忆我们之前聊的",
        ]
        for text in phrases:
            with self.subTest(text=text):
                self.assertTrue(looks_like_recall_intent(text), text)

    def test_non_recall_texts_not_detected(self) -> None:
        phrases = [
            "今天天气不错",
            "帮我查一下",
            "哈哈哈",
            "吃了吗",
            "把这张图发群里",
        ]
        for text in phrases:
            with self.subTest(text=text):
                self.assertFalse(looks_like_recall_intent(text), text)

    def test_english_recall_phrases_are_detected(self) -> None:
        self.assertTrue(looks_like_recall_intent("remember what you said"))
        self.assertTrue(looks_like_recall_intent("earlier you mentioned"))
        self.assertTrue(looks_like_recall_intent("previously we discussed"))

    def test_empty_text_not_detected(self) -> None:
        self.assertFalse(looks_like_recall_intent(""))
        self.assertFalse(looks_like_recall_intent("   "))


if __name__ == "__main__":
    unittest.main()
