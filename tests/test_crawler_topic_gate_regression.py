"""外部抓取回来的时政内容必须在**进模型之前**丢掉，不能等转述完靠词替换兜。

## 缺口（2026-08-06 实测）

`get_hot_trends` 此前零过滤。实测知乎热榜前三条里就有
「如何看待国家这一次的扫黑除恶专项行动？」。这条内容三层防护都覆盖不到：

* `SafetyEngine.evaluate` 的时政回避只作用在**用户消息**上，不看工具结果
* `filter_output` 只能替换词表里的词 —— 「扫黑除恶」当时不在表内
* 人格/硬约束是提示词，模型可能照转

更糟的是 `_handle_get_hot_trends` 的无平台分支会把标题**写进知识库持久化**
（`kb.add("trend", item.title, ...)`），之后还能通过 `search_knowledge` 再浮出来。
一次转述变成长期留存。

## 修法

`AgentContext.topic_gate` → `_build_tool_context` → handler 逐条判定后丢弃。
顺序要紧：**先过门，再格式化，再入库** —— 格式化之后就只是一段文本，
逐条判定的机会没了。

门坏了按**拦截**处理（fail closed），因为这是封群风险点，宁可少说。
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from typing import Any

from core.agent import AgentContext, AgentLoop
from core.agent_tools_knowledge import _handle_get_hot_trends
from core.safety import SafetyEngine


@dataclass
class _Row:
    title: str
    snippet: str = ""
    heat: str = ""
    url: str = ""


class _StubTrends:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    async def weibo_hot(self, limit: int = 10) -> list[_Row]:
        return self._rows[:limit]

    # handler 构造 method_map 时会访问全部四个平台属性，桩必须都提供，
    # 否则 AttributeError 会被 except 吞成 trends_error，测试看起来像过滤过头。
    async def bilibili_hot(self, limit: int = 10) -> list[_Row]:
        return self._rows[:limit]

    async def douyin_hot(self, limit: int = 10) -> list[_Row]:
        return self._rows[:limit]

    async def baidu_hot(self, limit: int = 10) -> list[_Row]:
        return self._rows[:limit]


class _StubHub:
    def __init__(self, rows: list[_Row]) -> None:
        self.trends = _StubTrends(rows)
        self._rows = rows

    async def get_trends_cached(self) -> dict[str, list[_Row]]:
        return {"weibo": list(self._rows)}

    @staticmethod
    def format_trends_text(trends: dict[str, list[_Row]], limit: int = 10) -> str:
        out = []
        for plat, rows in trends.items():
            for row in rows[:limit]:
                out.append(f"{plat}: {row.title}")
        return "\n".join(out)


class _RecordingKB:
    def __init__(self) -> None:
        self.added: list[str] = []

    def add(self, _kind: str, title: str, _body: str, **_kw: Any) -> None:
        self.added.append(title)


_POLITICAL = "如何看待国家这一次的扫黑除恶专项行动？"
_BENIGN = "台风白海豚大概率登陆浙江"


def _context(rows: list[_Row], *, kb: Any = None, gate: Any = None) -> dict[str, Any]:
    safety = SafetyEngine({})
    return {
        "crawler_hub": _StubHub(rows),
        "topic_gate": safety.is_political_topic if gate is None else gate,
        "knowledge_base": kb,
    }


class PoliticalHotTrendRowsAreDroppedTests(unittest.TestCase):
    def test_platform_path_drops_political_rows(self) -> None:
        ctx = _context([_Row(_POLITICAL), _Row(_BENIGN)])
        result = asyncio.run(_handle_get_hot_trends({"platform": "weibo"}, ctx))
        self.assertTrue(result.ok)
        self.assertNotIn("扫黑除恶", result.display, "时政热搜条目进了模型上下文")
        self.assertIn("台风", result.display, "正常条目被一起丢了")

    def test_aggregate_path_drops_political_rows(self) -> None:
        ctx = _context([_Row(_POLITICAL), _Row(_BENIGN)])
        result = asyncio.run(_handle_get_hot_trends({}, ctx))
        self.assertTrue(result.ok)
        self.assertNotIn("扫黑除恶", result.display)
        self.assertIn("台风", result.display)

    def test_political_rows_never_reach_the_knowledge_base(self) -> None:
        """最重要的一条：入库是持久化，比一次性转述严重。"""

        kb = _RecordingKB()
        ctx = _context([_Row(_POLITICAL), _Row(_BENIGN)], kb=kb)
        asyncio.run(_handle_get_hot_trends({}, ctx))
        self.assertNotIn(_POLITICAL, kb.added, "时政标题被写进知识库持久化了")
        self.assertIn(_BENIGN, kb.added, "正常条目没入库 —— 过滤过头了")

    def test_snippet_is_also_examined(self) -> None:
        """标题干净但摘要带时政的条目也要丢。"""

        ctx = _context([_Row(title="某个话题", snippet="讨论习近平的讲话")])
        result = asyncio.run(_handle_get_hot_trends({"platform": "weibo"}, ctx))
        self.assertNotIn("某个话题", result.display)


class GateFailuresAreFailClosedTests(unittest.TestCase):
    def test_gate_exception_drops_the_row(self) -> None:
        """门抛异常时按拦截处理 —— 这是封群风险点，宁可少说。"""

        def boom(_: str) -> bool:
            raise RuntimeError("gate exploded")

        ctx = _context([_Row(_BENIGN)], gate=boom)
        result = asyncio.run(_handle_get_hot_trends({"platform": "weibo"}, ctx))
        self.assertTrue(result.ok)
        self.assertNotIn("台风", result.display, "门坏了却放行了内容")

    def test_missing_gate_keeps_rows(self) -> None:
        """没注入门时原样返回 —— WebUI 测试台等非 QQ 场景的正常情况。"""

        ctx = _context([_Row(_BENIGN)])
        ctx["topic_gate"] = None
        result = asyncio.run(_handle_get_hot_trends({"platform": "weibo"}, ctx))
        self.assertIn("台风", result.display)


class GateIsWiredThroughTheAgentTests(unittest.TestCase):
    """光有 handler 逻辑不够 —— 门必须真的传到工具层。"""

    def test_agent_context_carries_topic_gate(self) -> None:
        ctx = AgentContext("c", "u", "n", 0, "b", False, False, "")
        self.assertTrue(hasattr(ctx, "topic_gate"))

    def test_tool_context_exposes_topic_gate(self) -> None:
        marker = object()
        ctx = AgentContext("c", "u", "n", 0, "b", False, False, "")
        ctx.topic_gate = marker  # type: ignore[assignment]
        loop = AgentLoop.__new__(AgentLoop)
        loop.config = {}  # type: ignore[attr-defined]
        built = AgentLoop._build_tool_context(loop, ctx, "user")
        self.assertIs(built.get("topic_gate"), marker)

    def test_engine_injects_the_gate(self) -> None:
        from pathlib import Path

        src = Path("core/engine.py").read_text(encoding="utf-8")
        self.assertIn(
            "topic_gate=self.safety.is_political_topic",
            src,
            "engine 没注入 topic_gate —— 抓取内容全程无时政过滤",
        )


class TermCoverageAndFalsePositivesTests(unittest.TestCase):
    """词表覆盖与误伤的双向钉子。

    词表方法的固有短板是覆盖与误伤此消彼长。这里只收**专有名词**，
    不收「国家」「政府」「专项行动」这类通用词 —— 后者在正常聊天里太常见。
    """

    def setUp(self) -> None:
        self.safety = SafetyEngine({})

    def test_state_campaign_names_are_flagged(self) -> None:
        for text in ("扫黑除恶专项行动", "反腐进展", "维稳工作", "计划生育政策", "社会信用体系"):
            with self.subTest(text=text):
                self.assertTrue(self.safety.is_political_topic(text), text)

    def test_benign_hot_search_titles_survive(self) -> None:
        """真实热搜标题，全部必须放行。"""

        for text in (
            "梅姨真实姓名首曝光",
            "预拨3.3亿元支持8省市抢险救灾",
            "台风白海豚大概率登陆浙江",
            "美国禁止进口中国机器人",
            "王楚钦谈亚运会期许",
            "李亚鹏向地铁吐血女孩捐99999元",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.safety.is_political_topic(text), text)

    def test_technical_and_chat_text_survives(self) -> None:
        for text in ("政策模式怎么配", "行政区划查询", "户籍所在地怎么填", "这个游戏的党争剧情"):
            with self.subTest(text=text):
                self.assertFalse(self.safety.is_political_topic(text), text)

    def test_gate_respects_the_disable_switch(self) -> None:
        safety = SafetyEngine({"political_deflect_enable": False})
        self.assertFalse(safety.is_political_topic("习近平"))


if __name__ == "__main__":
    unittest.main()
