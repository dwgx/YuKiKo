from __future__ import annotations

import unittest

from core.engine import YukikoEngine


class KaomojiModelOwnedRegressionTests(unittest.TestCase):
    """情绪表达归模型，代码不再改写。

    原实现在 kaomoji_enable=true 时对每条回复做三件事：
    `replace_emoji_with_kaomoji` 把模型写的 emoji 删掉换成 default_kaomoji、
    `normalize_kaomoji_style` 把所有颜文字删到只剩一个、
    `_enforce_kaomoji_allowlist` 按白名单再筛一遍并挪到句尾。
    净效果是模型写什么都被改写成以 QWQ 结尾 —— 冒烟测试里几乎每条回复都带 QWQ。
    """

    @staticmethod
    def _build_engine(*, enable: bool) -> YukikoEngine:
        engine = YukikoEngine.__new__(YukikoEngine)
        engine.kaomoji_enable = enable
        return engine

    def test_model_emoji_survives_untouched(self) -> None:
        engine = self._build_engine(enable=True)
        self.assertEqual(engine._apply_tone_guard("今天天气不错 😊"), "今天天气不错 😊")

    def test_model_may_use_any_kaomoji_not_just_an_allowlist(self) -> None:
        engine = self._build_engine(enable=True)
        for text in ("好耶 OwO", "唉 TAT", "什么 >_<", "收到 (｀・ω・´)"):
            with self.subTest(text):
                self.assertEqual(engine._apply_tone_guard(text), text)

    def test_model_may_use_more_than_one_and_place_them_freely(self) -> None:
        """旧实现强制「至多一个、且挪到句尾」，那是代码在定风格。"""

        engine = self._build_engine(enable=True)
        text = "QWQ 这个我真不会 AWA"
        self.assertEqual(engine._apply_tone_guard(text), text)

    def test_admin_disable_still_strips(self) -> None:
        """kaomoji_enable=false 是管理员显式关闭，属于该保留的配置门。"""

        engine = self._build_engine(enable=False)
        out = engine._apply_tone_guard("我想你了 QWQ AWA")
        self.assertNotIn("QWQ", out.upper())
        self.assertNotIn("AWA", out.upper())

    def test_excess_blank_lines_still_normalized(self) -> None:
        """排版归一是格式处理，不是情绪决定，保留。"""

        engine = self._build_engine(enable=True)
        self.assertEqual(engine._apply_tone_guard("第一行\n\n\n\n第二行"), "第一行\n\n第二行")

    def test_allowlist_machinery_is_gone(self) -> None:
        engine = self._build_engine(enable=True)
        for gone in ("kaomoji_allowlist", "default_kaomoji", "_enforce_kaomoji_allowlist"):
            self.assertFalse(hasattr(engine, gone), gone)

    def test_prompt_hint_no_longer_advertises_a_whitelist(self) -> None:
        on = self._build_engine(enable=True)._build_kaomoji_prompt_hint()
        self.assertNotIn("允许:", on)
        self.assertNotIn("白名单为空", on)

        off = self._build_engine(enable=False)._build_kaomoji_prompt_hint()
        self.assertIn("关闭", off)

    def test_dead_config_key_removed_from_both_truth_sources(self) -> None:
        import yaml

        from core.config_templates import _built_in_config_defaults

        defaults = _built_in_config_defaults()
        with open("config/templates/master.template.yml", encoding="utf-8") as fh:
            template = yaml.safe_load(fh)["config"]

        self.assertNotIn("kaomoji_allowlist", defaults["bot"])
        self.assertNotIn("kaomoji_allowlist", template["bot"])
        # 开关本身保留。
        self.assertIn("kaomoji_enable", defaults["bot"])
        self.assertIn("kaomoji_enable", template["bot"])


if __name__ == "__main__":
    unittest.main()
