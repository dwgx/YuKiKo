"""人格底稿有两处副本，实测已经漂移过两次 —— 这条守卫防第三次。

两处：
  core/personality.py 的 DEFAULT_PERSONA_TEXT 常量
  config/personas/yukiko.md            <- **运行时真正生效的就是这个**

`ensure_default_files()` 里的落盘分支是 `if not persona_path.exists()`，
所以 md 一旦存在，常量就永远不会覆盖它。改常量对已部署实例**零效果**。

实测踩过两次：
1. 上一轮拆「QWQ/owo 口癖」时只改了常量，md 里 `- 可以用颜文字但不过度：~、QWQ、owo、><`
   原样留着 —— 那次改动线上从未生效（运行时 persona_text 里能 grep 到 QWQ）。
2. 本轮收窄「被骂可以反击」的作用域时同样只改了常量。

所以本文件断言两处逐行相等，并单独钉住那两条容易被漏掉的边界。
"""

from __future__ import annotations

import difflib
import unittest
from pathlib import Path

from core.personality import DEFAULT_PERSONA_TEXT, PersonalityEngine

_PERSONA_MD = Path("config/personas/yukiko.md")
_PERSONALITY_YML = Path("config/personality.yml")


class PersonaFileDriftTests(unittest.TestCase):
    def test_markdown_and_constant_are_line_for_line_identical(self) -> None:
        self.assertTrue(_PERSONA_MD.is_file(), f"{_PERSONA_MD} 不存在")
        constant = DEFAULT_PERSONA_TEXT.strip().split("\n")
        on_disk = _PERSONA_MD.read_text(encoding="utf-8").strip().split("\n")
        if constant != on_disk:
            diff = "\n".join(
                difflib.unified_diff(
                    constant, on_disk, "DEFAULT_PERSONA_TEXT", str(_PERSONA_MD), lineterm="", n=1
                )
            )
            self.fail(
                "人格底稿两处副本已漂移。**运行时生效的是 md**，只改常量等于没改：\n" + diff
            )

    def test_runtime_persona_carries_the_scoped_retaliation_boundary(self) -> None:
        """走真实读取路径验证，而不是读常量 —— 前者才是模型真正看到的。"""

        persona = PersonalityEngine.from_file(_PERSONALITY_YML).persona_text
        self.assertIn("被直接骂", persona, "反击许可必须带「直接/当面」限定")
        self.assertIn("转述", persona, "必须说明转述/引用的辱骂不适用反击")
        self.assertNotIn(
            "被骂/攻击：可以反击",
            persona,
            "无限定的反击许可不能留在运行时人格底稿里",
        )

    def test_runtime_persona_keeps_the_anti_verbal_tic_guidance(self) -> None:
        """上一轮那条改动实测从未生效过，这里钉住它。"""

        persona = PersonalityEngine.from_file(_PERSONALITY_YML).persona_text
        self.assertIn("口癖", persona)
        self.assertNotIn("QWQ", persona, "固定颜文字表已被移除，不该回来")

    def test_runtime_persona_keeps_the_persona_edge(self) -> None:
        """反向保护：收窄作用域不等于改成客服腔。"""

        persona = PersonalityEngine.from_file(_PERSONALITY_YML).persona_text
        self.assertIn("不一味道歉", persona)
        self.assertIn("傲娇", persona)


if __name__ == "__main__":
    unittest.main()
