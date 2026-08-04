from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from core.prompt_navigator import default_prompt_navigator_payload

_REPO = Path(__file__).resolve().parent.parent


def _general_chat_instructions(payload: dict) -> str:
    return payload["sections"]["general_chat"]["instructions"]


class GeneralChatSilenceScopeRegressionTests(unittest.TestCase):
    """`general_chat` 的沉默指令原先没有作用域。

    「这是本区最常见也最正确的一种输出」「不确定就选择沉默」是无条件的，
    于是被 @ 直接问「你是谁」时模型也交空 final_answer，再被 agent 改写成
    「这次没拿到有效结果」兜底文案 —— 实测 3/3 全失败。
    同一分区的 `when_to_use` 本来就写了身份类问题该直接答，是 instructions
    的沉默偏压压过了它。
    """

    def test_silence_guidance_is_scoped_to_undirected_messages(self) -> None:
        ins = _general_chat_instructions(default_prompt_navigator_payload())

        # 沉默指令必须先声明作用域，再给出具体条目。
        self.assertIn("以下三句只在", ins)
        scope_at = ins.index("以下三句只在")
        for scoped in (
            "非指向场景下沉默是常见且正确的输出",
            "选择沉默而不是赌一把",
            "当成不要开口的信号",
        ):
            self.assertIn(scoped, ins)
            self.assertGreater(ins.index(scoped), scope_at, scoped)

        # 原来那句无条件断言不能再出现。
        self.assertNotIn("这是本区最常见也最正确的一种输出", ins)

    def test_directed_messages_forbid_silence_and_dodging(self) -> None:
        ins = _general_chat_instructions(default_prompt_navigator_payload())

        self.assertIn("这四种情形里沉默永远是错的", ins)
        self.assertIn("不是沉默的理由，而是反问一句的理由", ins)
        self.assertIn("必须正面回答", ins)
        # 实测它就是用这几句搪塞的，明确列为禁止。
        self.assertIn("脑子卡了", ins)

    def test_instructions_carry_no_stray_yaml_indentation(self) -> None:
        """两个 YAML 源是折行双引号标量，漏掉续行符 `\\` 会把缩进变成 prompt 正文。

        第一版补丁正是这么错的，靠解析比对才发现。
        """

        for payload in _iter_three_sources():
            ins = _general_chat_instructions(payload)
            offenders = [ln for ln in ins.split("\n") if ln.startswith("   ")]
            self.assertEqual(offenders, [], offenders[:3])

    def test_three_prompt_sources_stay_byte_identical(self) -> None:
        """Python 载荷 / master.template.yml / prompts.yml 三处真相必须一致。

        `_merge_with_defaults` 只回填缺失键、从不覆盖已有键，所以只改 Python
        默认值不会传播到已存在的 prompts.yml —— 三处都得写。
        """

        python_payload, template_payload, prompts_payload = _iter_three_sources()
        self.assertEqual(python_payload, template_payload)
        self.assertEqual(python_payload, prompts_payload)


def _iter_three_sources() -> tuple[dict, dict, dict]:
    python_payload = default_prompt_navigator_payload()

    with open(_REPO / "config/templates/master.template.yml", encoding="utf-8") as fh:
        template_payload = yaml.safe_load(fh)["prompts"]["prompt_navigator"]

    with open(_REPO / "config/prompts.yml", encoding="utf-8") as fh:
        prompts_payload = yaml.safe_load(fh)["prompt_navigator"]

    return python_payload, template_payload, prompts_payload


if __name__ == "__main__":
    unittest.main()
