"""旁听探测配额必须够用 —— 否则「让模型自己判断该不该开口」形同虚设。

## 实测（2026-08-06，业主的群，storage/logs/yukiko.log 15126 行）

```
not_directed 忽略          185 条   <- 模型完全没看见
旁听探测实际发生            53 次
ai_listen_max_probes_per_hour = 6
```

业主的诉求是「用提示词让 AI 自己理解，感觉叫我了就对了」。但
`_structural_request_signal` 只给四类结构事实打分（命令令牌 / URL / 视频号 /
文件扩展名），所以「他叫你碳基」「你骂他吧」这类**明明在说机器人**的消息拿 0 分，
低于 `delegate_undirected_min_signal`，唯一还能把它们送进模型的通路就是旁听探测。

配额 6 次/小时时那条通路基本是关着的：提示词写得再准，消息到不了模型面前。

本文件钉两件事：配额够用，以及三处真相源同步（CLAUDE.md 的硬要求 ——
只改一处会导致「本机对、新装错」或反之）。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import yaml
from core.config_templates import _built_in_config_defaults
from core.trigger import TriggerEngine

_MIN_USABLE_BUDGET = 12
_BOT_CONFIG: dict = {"name": "yukiko", "aliases": ["yuki"]}


def _engine(trigger_config: dict) -> TriggerEngine:
    return TriggerEngine(trigger_config, _BOT_CONFIG)


def _template_trigger_config() -> dict:
    raw = yaml.safe_load(
        Path("config/templates/master.template.yml").read_text(encoding="utf-8")
    )
    return raw["config"]["trigger"]


class ProbeBudgetIsLargeEnoughToBeUsefulTests(unittest.TestCase):
    def test_code_fallback_allows_enough_probes(self) -> None:
        engine = _engine({})
        self.assertGreaterEqual(
            engine.ai_listen_max_probes_per_hour,
            _MIN_USABLE_BUDGET,
            "代码兜底配额太小 —— 没有 config.yml 的安装会继续「不理人」",
        )

    def test_template_allows_enough_probes(self) -> None:
        value = int(_template_trigger_config()["ai_listen_max_probes_per_hour"])
        self.assertGreaterEqual(value, _MIN_USABLE_BUDGET, "模板配额太小")

    def test_builtin_defaults_allow_enough_probes(self) -> None:
        value = int(
            _built_in_config_defaults()["trigger"]["ai_listen_max_probes_per_hour"]
        )
        self.assertGreaterEqual(value, _MIN_USABLE_BUDGET, "内置默认值配额太小")

    def test_all_three_sources_agree(self) -> None:
        """CLAUDE.md：模板运行时优先于 Python，只改一处会造成两边行为不一致。"""

        code = _engine({}).ai_listen_max_probes_per_hour
        template = int(_template_trigger_config()["ai_listen_max_probes_per_hour"])
        builtin = int(
            _built_in_config_defaults()["trigger"]["ai_listen_max_probes_per_hour"]
        )
        self.assertEqual(
            {code, template, builtin},
            {code},
            f"三处真相源不一致: code={code} template={template} builtin={builtin}",
        )


class BudgetStillHasAHardCeilingTests(unittest.TestCase):
    """放开不等于放飞 —— 上限必须还在，否则 provider 会被打满。"""

    def test_budget_is_still_bounded(self) -> None:
        engine = _engine({})
        self.assertLessEqual(
            engine.ai_listen_max_probes_per_hour,
            40,
            "配额上限过大，等于取消了硬上限保护",
        )

    def test_budget_is_exhausted_after_its_quota(self) -> None:
        """配额用尽后必须真的停下来。"""

        engine = _engine({"ai_listen_max_probes_per_hour": 3})
        now = datetime.now(UTC)
        for _ in range(3):
            self.assertTrue(engine._probe_budget_ok("group:1", now))
            engine._commit_probe("group:1", now)
        self.assertFalse(
            engine._probe_budget_ok("group:1", now), "配额用尽后还在放行探测"
        )

    def test_budget_recovers_after_the_window(self) -> None:
        engine = _engine({"ai_listen_max_probes_per_hour": 2})
        now = datetime.now(UTC)
        for _ in range(2):
            engine._commit_probe("group:1", now)
        self.assertFalse(engine._probe_budget_ok("group:1", now))
        self.assertTrue(
            engine._probe_budget_ok("group:1", now + timedelta(hours=1, minutes=1)),
            "滑动窗口过后配额没恢复",
        )

    def test_zero_still_disables_probing_entirely(self) -> None:
        """0 = 关闭旁听，这个语义不能被改动破坏。"""

        engine = _engine({"ai_listen_max_probes_per_hour": 0})
        self.assertFalse(engine._probe_budget_ok("group:1", datetime.now(UTC)))

    def test_budget_is_per_conversation(self) -> None:
        """一个群用满不该影响另一个群。"""

        engine = _engine({"ai_listen_max_probes_per_hour": 1})
        now = datetime.now(UTC)
        engine._commit_probe("group:1", now)
        self.assertFalse(engine._probe_budget_ok("group:1", now))
        self.assertTrue(engine._probe_budget_ok("group:2", now))


class TalkingAboutTheBotGuidanceReachesAllPromptSourcesTests(unittest.TestCase):
    """「这句话在说你就该接」那段提示词必须在三处真相源里都有。

    仓库已有三条守卫（test_extract_subtitle / test_general_chat_silence_scope /
    test_persona_no_internal_leak）钉住三处**逐字节一致**，本测试额外钉住
    这段话真的存在 —— 前者只保证一致，不保证内容在。
    """

    ANCHOR = "第五种同样不该沉默的情形"

    def test_python_payload_has_the_guidance(self) -> None:
        src = Path("core/prompt_navigator_data.yml").read_text(encoding="utf-8")
        self.assertIn(self.ANCHOR, src, "数据文件缺这段指引")

    def test_template_has_the_guidance(self) -> None:
        src = Path("config/templates/master.template.yml").read_text(encoding="utf-8")
        self.assertIn(
            self.ANCHOR,
            src,
            "模板缺这段指引 —— 模板运行时优先于 Python，缺了等于改动不生效",
        )

    def test_prompts_file_has_the_guidance(self) -> None:
        src = Path("config/prompts.yml").read_text(encoding="utf-8")
        self.assertIn(self.ANCHOR, src, "config/prompts.yml 缺这段指引")

    def test_guidance_still_tells_the_model_to_stay_silent_about_others(self) -> None:
        """反向：不能变成「什么都接」。谈论别人时仍要沉默。"""

        src = Path("core/prompt_navigator_data.yml").read_text(encoding="utf-8")
        self.assertIn(
            "话题里的人不是你",
            src,
            "只写了「该接」没写「该沉默的边界」—— 会变成群里什么都插嘴",
        )


if __name__ == "__main__":
    unittest.main()
