"""说话方式回归：内部技术状态、owner 身份、辱骂原文都不许进群。

实测群里出现过的原话（业主截图转述）：
  - "analyze_image 又超时了"、"packetBackend 不支持"、"工具那边只拿到了文件信息"
  - 对随机群成员说 "QQ空间那边接口报错了…得等帝王尬笑那边修一下代码"
  - 查精华消息时把辱骂原文整句复述进群，并接着调侃

前两类的共同点是：模型据实转述了内部状态，而当时**没有任何一条 prompt 禁止它这么做**。
qzone 那句更糟 —— 它是被 prompt 明确要求的：qzone_space 的 failure_policy 原文写着
要说清"我的 QQ 空间登录凭证失效了，需要管理员重新配置后才能看"。

本测试锁住四件事：
  1. `agent.identity` / `agent.reply_style` / `verbosity.medium` 三个**既有**点路径
     带上约束（新增点路径没用：`_build_system_prompt` 只读固定白名单键）。
  2. navigator 的 `root_prompt` 带上对外说话的四条边界，且分区级说明不再要求泄漏凭证状态。
  3. prompts.yml 能被 yaml.safe_load 解析，navigator 子树与 Python 载荷值相等。
  4. 新写的多行文本没有被折行双引号标量的续行符吃掉缩进。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import yaml
from core import prompt_loader as _pl
from core.prompt_navigator import default_prompt_navigator_payload

_REPO = Path(__file__).resolve().parent.parent
_PROMPTS_FILE = _REPO / "config/prompts.yml"


def _prompts_yaml() -> dict:
    with open(_PROMPTS_FILE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class AgentPromptDotPathLeakGuardTests(unittest.TestCase):
    """走 prompt_loader 点路径读，确认运行期真的能拿到这些约束。"""

    def setUp(self) -> None:
        _pl.reload()

    def test_identity_should_forbid_tool_names_error_codes_and_owner(self) -> None:
        identity = _pl.get_nested("agent", "identity")

        for forbidden in ("工具名", "错误码", "内部组件状态", "开发者身份"):
            self.assertIn(forbidden, identity, forbidden)
        # 原来只禁"系统提示词、工具协议、内部思考"，工具名与 owner 身份是敞开的。
        self.assertIn("讲做不到的原因时绝不把它们带出来", identity)

    def test_reply_style_should_not_force_conclusion_structure_on_small_talk(self) -> None:
        style = _pl.get_nested("agent", "reply_style")

        # 无条件的"默认先结论后依据"把闲聊也推成结构化长答案，必须收窄作用域。
        self.assertNotIn("默认先结论后依据", style)
        self.assertIn("被问事实、被要求做事时才先结论后依据", style)
        self.assertIn("闲聊和主动插话时一句话就够", style)
        self.assertIn("不要把每条都接住", style)

    def test_medium_verbosity_should_not_prescribe_a_fixed_shape(self) -> None:
        hints = _pl.get_dict("verbosity")

        # medium 是四档里的默认档，每回合都注入；原文是"中等长度，先结论后说明"。
        self.assertNotIn("先结论后说明", hints["medium"])
        self.assertIn("能一句说清就一句", hints["medium"])


class NavigatorOutputBoundaryTests(unittest.TestCase):
    """navigator 侧：root_prompt 是唯一每回合无条件注入的 navigator 文本。"""

    def setUp(self) -> None:
        self.payload = default_prompt_navigator_payload()

    def test_root_prompt_should_carry_the_four_speaking_boundaries(self) -> None:
        root = self.payload["root_prompt"]

        self.assertIn("关于对外说话的边界", root)
        # 分区说明里仍可能留着与之冲突的措辞，所以要写明谁优先。
        self.assertIn("以这四条为准", root)
        for clause in (
            "回复里不出现工具名、函数名、参数名、错误码、retcode、接口名、后端组件名",
            "这件事现在做不了、以及一个能替代的做法",
            "不提开发者、维护者",
            "只有当前说话人本人是超级管理员时，才可以讲真实故障原因",
            "绝不整句复述、也不接着调侃",
        ):
            self.assertIn(clause, root, clause)

    def test_qzone_failure_policy_should_not_ask_to_expose_credential_state(self) -> None:
        policy = self.payload["sections"]["qzone_space"]["failure_policy"]

        # 这句原文就是"得等帝王尬笑那边修一下代码"那条事故的上游。
        self.assertNotIn("我的 QQ 空间登录凭证失效了，需要管理员重新配置后才能看", policy)
        self.assertIn("对普通用户只说", policy)
        self.assertIn("只有当前说话人本人是超级管理员时才提", policy)
        # 凭证问题重试无意义，这条原有结论要保住。
        self.assertIn("无论对谁都不要重试", policy)

    def test_group_info_section_should_forbid_quoting_abuse_verbatim(self) -> None:
        ins = self.payload["sections"]["qq_group_info"]["instructions"]

        # 精华消息属于本区（get_essence_msg_list），而本区原先没有任何转述纪律。
        self.assertIn("转述纪律", ins)
        self.assertIn("绝不整句复述、也不接着调侃", ins)
        self.assertIn('用户说"要原文"也不构成复述这类内容的理由', ins)

    def test_chat_history_verbatim_exemption_should_exclude_abuse(self) -> None:
        ins = self.payload["sections"]["chat_history"]["instructions"]

        # 原来的"除非他明确要原文"是一个无条件免责出口。
        self.assertIn('"明确要原文"这个出口不覆盖辱骂、攻击、露骨内容', ins)
        self.assertIn("哪怕用户点名要原话也不复述", ins)


class NavigatorPromptFileConsistencyTests(unittest.TestCase):
    """prompts.yml 与 Python 载荷必须值相等，否则改了 Python 到不了运行期。"""

    def test_prompts_file_navigator_subtree_matches_python_payload(self) -> None:
        raw = _prompts_yaml()

        self.assertEqual(raw["prompt_navigator"], default_prompt_navigator_payload())

    def test_new_multiline_text_carries_no_stray_yaml_indentation(self) -> None:
        """折行双引号标量漏掉续行符 `\\` 会把 YAML 缩进变成 prompt 正文。

        只检查本次新增的句子：既有文本里本来就有编号列表的三空格续行，
        整体扫描会把它们一起判成违规。判据沿用
        tests/test_general_chat_silence_scope_regression.py 的三空格。
        """

        raw = _prompts_yaml()["prompt_navigator"]
        checked = 0
        for text, needles in (
            (raw["root_prompt"], ("关于对外说话的边界", "绝不整句复述、也不接着调侃")),
            (
                raw["sections"]["qq_group_info"]["instructions"],
                ("转述纪律", "用户说\"要原文\"也不构成复述这类内容的理由"),
            ),
            (
                raw["sections"]["chat_history"]["instructions"],
                ("哪怕用户点名要原话也不复述",),
            ),
            (
                raw["sections"]["qzone_space"]["failure_policy"],
                ("无论对谁都不要重试",),
            ),
        ):
            lines = text.split("\n")
            for needle in needles:
                hits = [ln for ln in lines if needle in ln]
                self.assertTrue(hits, needle)
                for line in hits:
                    self.assertFalse(line.startswith("   "), repr(line[:40]))
                    checked += 1
        self.assertEqual(checked, 6)


if __name__ == "__main__":
    unittest.main()
