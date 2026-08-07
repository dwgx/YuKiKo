"""Auto-split from core/agent_tools.py — 知识库 + 爬虫工具"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from core.agent_tools_types import PromptHint, ToolCallResult, ToolSchema
from core.agent_tools_registry import AgentToolRegistry
from core.napcat_compat import call_napcat_api
from core.recalled_messages import (
    build_conversation_id as _build_recall_conversation_id,
    record_recalled_message as _record_recalled_message,
)
from utils.learning_guard import assess_preferred_name_learning
from utils.text import clip_text, normalize_matching_text, normalize_text, tokenize

_log = logging.getLogger("yukiko.agent_tools")


# 百科抓取结果的置信度：是可核查的外部来源，但不是用户亲口说的，
# 所以低于 learn_knowledge 的默认 0.7 —— 用户明确纠正时能压过它。
_WIKI_CONFIDENCE = 0.6

# 模型自己声明的安全判定取值。判据归 prompt 措辞，不归本文件的词表。
_SAFETY_REVIEW_REJECT = frozenset({"unsafe", "harmful", "abusive", "reject", "block", "unsafe_content"})
_SAFETY_REVIEW_PASS = frozenset({"safe", "ok", "clean", "benign"})

# learn_knowledge 的 kind 取值。**与 core/knowledge_updater.py 抽取器提示里的那套枚举
# 逐字一致**（fact|preference|music_preference|preferred_name|correction）——
# 两条入库路径对同一个词的理解必须相同，否则模型在两处学到的用法会互相矛盾。
# 这不是词表：它是模型声明意图的取值域，不是「消息含某词 → 做某事」。
_LEARN_KNOWLEDGE_KINDS = frozenset({"fact", "preference", "music_preference", "preferred_name", "correction"})


def _kb_record_audit(kb: Any, event: str, **fields: Any) -> None:
    """把知识库写入决策记进 knowledge 审计流。

    kb 可能是测试里的 stub 或未注入 AuditTrail 的实例 —— 两种情况都静默跳过。
    KnowledgeBase.record_audit 自身不抛异常（AuditTrail.write 不抛）。
    """
    recorder = getattr(kb, "record_audit", None)
    if not callable(recorder):
        return
    try:
        recorder(event, **fields)
    except Exception:
        _log.warning("knowledge_audit_record_failed | event=%s", event, exc_info=True)


def _resolve_safety_review(args: dict[str, Any]) -> str:
    """读取模型声明的 safety_review。

    这里刻意**不做任何内容判断**：原先 _looks_like_harmful_knowledge_payload 用一张
    8 词脏词表 + 「以后你叫」/「叫他」组合来猜有害意图，实测把
    「大便颜色异常可能提示消化道问题」「智障儿童的正式称呼已改为智力障碍」
    「我不喜欢被叫废物」这类正常知识全部误判为有害（12 条样本里 11 条命中，
    连「滚石唱片」「鼠标滚轮」都因为含「滚」被拦）。
    判据交给模型读分区措辞后自己声明；拒绝**机制**保留，见调用点。
    """
    raw = normalize_text(str(args.get("safety_review", ""))).lower()
    if raw in _SAFETY_REVIEW_REJECT:
        return "unsafe"
    if raw in _SAFETY_REVIEW_PASS:
        return "safe"
    # 没声明不等于放行成功：写入照做，但审计流里明确记成 unreviewed，可事后追。
    return "unreviewed"


def _has_cross_user_profile_access(context: dict[str, Any]) -> bool:
    level = normalize_text(str(context.get("permission_level", ""))).lower()
    return level == "super_admin"


def _resolve_profile_target_user(
    args: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, ToolCallResult | None]:
    requested_user_id = normalize_text(str(args.get("user_id", "")))
    current_user_id = normalize_text(str(context.get("user_id", "")))
    if _has_cross_user_profile_access(context):
        target_user_id = requested_user_id or current_user_id
        if not target_user_id:
            return "", ToolCallResult(ok=False, error="missing_user_id")
        return target_user_id, None
    if not current_user_id:
        return "", ToolCallResult(ok=False, error="missing_user_id")
    if requested_user_id and requested_user_id != current_user_id:
        return "", ToolCallResult(
            ok=False,
            error="permission_denied:user_scope",
            display="普通用户只能读取或写入自己的画像与事实记忆。",
        )
    return current_user_id, None

def _register_crawler_tools(registry: AgentToolRegistry) -> None:
    """注册知乎/百科/热搜/知识库工具。"""

    registry.register(
        ToolSchema(
            name="get_hot_trends",
            description=(
                "获取全网热搜热榜: 微博热搜、B站热门、抖音热榜、百度热搜。\n"
                "可指定平台(weibo/bilibili/douyin/baidu)或不指定获取全部。\n"
                "使用场景: 用户问最近有什么热点/新闻/热搜时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "description": "平台(可选): weibo/bilibili/douyin/baidu，不填获取全部",
                    },
                    "limit": {"type": "integer", "description": "每个平台返回条数(默认10)"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_get_hot_trends,
    )

    registry.register(
        ToolSchema(
            name="search_zhihu",
            description=(
                "搜索知乎内容或获取知乎热榜。\n"
                "mode=hot 获取热榜（无需其它参数）；mode=search 搜索内容（必须提供 query）；"
                "mode=answers 获取问题高赞回答（必须提供 question_id）。\n"
                "使用场景: 用户问知乎相关问题、想了解某个话题的讨论时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["hot", "search", "answers"],
                        "description": "模式: hot(热榜)/search(搜索，需 query)/answers(回答，需 question_id)",
                    },
                    "query": {"type": "string", "description": "搜索关键词(search模式必填)"},
                    "question_id": {"type": "string", "description": "知乎问题ID(answers模式必填)"},
                },
                "required": ["mode"],
            },
            category="search",
        ),
        _handle_search_zhihu,
    )

    registry.register(
        ToolSchema(
            name="lookup_wiki",
            description=(
                "查询百科知识: 同时搜索百度百科和维基百科。\n"
                "使用场景: 用户问某个概念/人物/事件的定义或背景知识时使用。\n"
                "返回百度百科和维基百科的摘要。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要查询的关键词"},
                },
                "required": ["keyword"],
            },
            category="search",
        ),
        _handle_lookup_wiki,
    )

    registry.register(
        ToolSchema(
            name="search_knowledge",
            description=(
                "搜索知识库: 查找已学习的知识、热梗、百科、事实。\n"
                "知识库独立于对话记忆，存储持久化知识。\n"
                "category可选: fact(事实)/meme(热梗)/wiki(百科)/trend(热搜)/learned(学习)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "category": {"type": "string", "description": "分类(可选)"},
                },
                "required": ["query"],
            },
            category="search",
        ),
        _handle_search_knowledge,
    )

    registry.register(
        ToolSchema(
            name="learn_knowledge",
            description=(
                "学习新知识: 将信息存入知识库。\n"
                "使用场景: 用户教你新知识、新梗、新概念时使用。\n"
                "category: fact(事实)/meme(热梗)/learned(学习到的)"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "知识标题/名称"},
                    "content": {"type": "string", "description": "知识内容"},
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "preference", "music_preference", "preferred_name", "correction"],
                        "description": (
                            "这条要记的是什么性质，由你判断后声明，代码不再猜：\n"
                            "fact=客观事实（默认）；preference=用户的偏好；"
                            "music_preference=音乐口味；\n"
                            "preferred_name=用户要求你改口怎么称呼他本人 —— "
                            "只有这个值会去改称呼，content 直接写称呼本身（如「阿背」），不要写整句话；\n"
                            "correction=对同标题旧值的更正。\n"
                            "注意：「我是程序员」「我是女生」这类是 fact，不是 preferred_name。"
                        ),
                    },
                    "category": {"type": "string", "description": "分类: fact/meme/learned"},
                    "tags": {"type": "string", "description": "标签(逗号分隔)"},
                    "confidence": {
                        "type": "number",
                        "description": "你对这条知识的把握程度 0..1。写入时参与去重与矛盾裁决：同标题已有更高把握的旧值时不会被低把握的新值覆盖。不填按 0.7 计。",
                    },
                    "safety_review": {
                        "type": "string",
                        "description": "你对这条内容的安全判定: safe 或 unsafe。判定为 unsafe 时本次写入被拒绝。不填会作为 unreviewed 记入审计流。",
                    },
                    "is_correction": {
                        "type": "boolean",
                        "description": "这是对同标题旧值的明确更正吗。为 true 时允许低把握覆盖高把握旧值。",
                    },
                },
                "required": ["title", "content"],
            },
            category="search",
            # 两个枚举 + confidence 浮点范围 + tags 逗号格式，示例把它们一次演示齐。
            input_examples=[
                {
                    "title": "群主生日",
                    "content": "群主生日是 3 月 5 日",
                    "kind": "fact",
                    "category": "fact",
                    "tags": "群主,生日",
                    "confidence": 0.9,
                    "safety_review": "safe",
                },
                # 改称呼长什么样：content 只写称呼本身。这条示例是 kind 语义的主要载体 ——
                # 「我是程序员」这类事实必须走上面的 fact，不能落到这里。
                {
                    "title": "用户称呼偏好",
                    "content": "阿背",
                    "kind": "preferred_name",
                    "safety_review": "safe",
                },
                {
                    "title": "顶不住了",
                    "content": "群里的梗，表示扛不住",
                    "category": "meme",
                    "safety_review": "safe",
                    "is_correction": True,
                },
            ],
        ),
        _handle_learn_knowledge,
    )

    async def _handle_remember_user_fact(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        user_id, denied = _resolve_profile_target_user(args, context)
        if denied is not None:
            return denied
        fact = normalize_text(str(args.get("fact", "")))
        if not user_id or not fact:
            return ToolCallResult(ok=False, error="missing_user_id_or_fact")
        conversation_id = normalize_text(str(context.get("conversation_id", "")))
        ok = memory.add_user_fact(user_id, fact, conversation_id)
        if ok:
            return ToolCallResult(ok=True, display=f"已记住: {fact[:80]}", data={"user_id": user_id, "fact": fact})
        return ToolCallResult(ok=False, error="save_failed")

    registry.register(
        ToolSchema(
            name="remember_user_fact",
            description=(
                "记住关于用户的事实信息。\n"
                "使用场景: 从图片分析、对话中学到用户身份/偏好/特征时主动存储。\n"
                "例如: 用户的用户名、常用工具、职业、兴趣等。\n"
                "存储后下次对话可直接回忆，无需重新分析。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户ID（留空则用当前对话用户）"},
                    "fact": {"type": "string", "description": "要记住的事实（如: Claude用户名=dwgx1337）"},
                },
                "required": ["fact"],
            },
            category="search",
        ),
        _handle_remember_user_fact,
    )

    async def _handle_recall_about_user(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        """综合回忆关于用户的所有已知信息。"""
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        user_id, denied = _resolve_profile_target_user(args, context)
        if denied is not None:
            return denied

        lines: list[str] = []

        # 1. 用户画像
        profile_summary = memory.get_user_profile_summary(user_id)
        if profile_summary:
            lines.append(f"[画像] {profile_summary}")

        # 2. 显式记忆事实
        facts = memory.get_explicit_facts(user_id, limit=10)
        if facts:
            lines.append(f"[记忆事实] " + "；".join(f[:60] for f in facts))

        # 3. 知识库中关于此用户的记录
        kb = context.get("knowledge_base")
        if kb is not None:
            try:
                # 用 search_by_tag 精确取 user:<id> 标签，避免 kb.search 的 LIKE 子串
                # 匹配串号（user:10001 命中 user:1000102 的条目，跨用户泄漏）。
                search_fn = getattr(kb, "search_by_tag", None)
                if callable(search_fn):
                    kb_results = search_fn(f"user:{user_id}", category="learned", limit=8)
                else:
                    kb_results = kb.search(f"user:{user_id}", category="learned", limit=8)
                if kb_results:
                    kb_items = []
                    for entry in kb_results:
                        title = normalize_text(str(getattr(entry, "title", "")))
                        content = normalize_text(str(getattr(entry, "content", "")))
                        if content:
                            kb_items.append(f"{title}: {content[:60]}" if title else content[:60])
                    if kb_items:
                        lines.append(f"[知识库] " + "；".join(kb_items))
            except Exception:
                pass

        # 4. 知识图谱 (knowledge_store)
        if hasattr(memory, "knowledge_get_user_summary"):
            try:
                ks_summary = memory.knowledge_get_user_summary(user_id, limit=10)
                if ks_summary:
                    lines.append(f"[知识图谱] {ks_summary}")
            except Exception:
                pass

        # 5. Agent policies
        policies = memory.get_agent_policies(user_id) if hasattr(memory, "get_agent_policies") else []
        if policies:
            lines.append(f"[偏好指令] " + "；".join(str(p)[:40] for p in policies[:5]))

        if not lines:
            return ToolCallResult(ok=True, display=f"暂无关于用户 {user_id} 的记录", data={"user_id": user_id})

        display = "\n".join(lines)
        return ToolCallResult(ok=True, display=display, data={"user_id": user_id, "items": len(lines)})

    registry.register(
        ToolSchema(
            name="recall_about_user",
            description=(
                "回忆关于某用户的所有已知信息。\n"
                "综合查询: 用户画像、记忆事实、知识库记录、偏好指令。\n"
                "当用户问'你记得我吗'、'你知道我是谁'时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户ID（留空则用当前对话用户）"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_recall_about_user,
    )

    async def _handle_summarize_conversation(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
        """主动生成当前对话摘要并存入归档。"""
        memory = context.get("memory_engine")
        if memory is None:
            return ToolCallResult(ok=False, error="memory_engine_unavailable")
        conversation_id = normalize_text(str(context.get("conversation_id", "")))
        if not conversation_id:
            return ToolCallResult(ok=False, error="no_conversation")
        limit = max(10, min(50, int(args.get("message_count", 20) or 20)))
        recent = memory.get_recent_texts(conversation_id, limit=limit)
        if not recent:
            return ToolCallResult(ok=True, display="对话为空，无需摘要")
        excerpt = "\n".join(recent)[:2000]
        summary = normalize_text(str(args.get("summary", "")))
        if not summary:
            summary = f"最近 {len(recent)} 条消息摘要（用户可通过对话补充）"
        key_facts = [normalize_text(str(f)) for f in (args.get("key_facts", []) or []) if normalize_text(str(f))]
        record_id = memory.save_conversation_summary(
            conversation_id, summary, key_facts=key_facts, message_range=f"last_{limit}"
        )
        if record_id:
            return ToolCallResult(ok=True, display=f"已保存对话摘要 (#{record_id})", data={"id": record_id})
        return ToolCallResult(ok=False, error="save_failed")

    registry.register(
        ToolSchema(
            name="summarize_conversation",
            description=(
                "生成并保存当前对话的摘要。\n"
                "用于长对话中保留关键信息，防止上下文丢失。\n"
                "摘要会在后续对话中作为历史背景注入。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "手动摘要文本（留空则自动标注）"},
                    "key_facts": {"type": "array", "items": {"type": "string"}, "description": "关键事实列表"},
                    "message_count": {"type": "integer", "description": "要摘要的消息数（默认20）"},
                },
                "required": [],
            },
            category="search",
        ),
        _handle_summarize_conversation,
    )


async def _handle_get_hot_trends(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable", display="爬虫模块未初始化")

    platform = str(args.get("platform", "")).strip().lower()
    limit = min(20, max(3, int(args.get("limit", 10) or 10)))

    # 时政条目在进模型之前就丢掉。
    #
    # 中文热搜天然带时政 —— 2026-08-06 实测知乎热榜前三条里就有
    # 「如何看待国家这一次的扫黑除恶专项行动？」。而这条内容此前完全无过滤：
    # 输入门只看用户消息，filter_output 只能替换词表内的词（"扫黑除恶" 不在表内），
    # 更糟的是下面还会把标题写进知识库**持久化**，之后能通过 search_knowledge 再浮出来。
    topic_gate = context.get("topic_gate")

    def _drop_political(rows: list[Any]) -> tuple[list[Any], int]:
        if not callable(topic_gate):
            return rows, 0
        kept = []
        for row in rows:
            probe = f"{getattr(row, 'title', '')} {getattr(row, 'snippet', '')}"
            try:
                blocked = bool(topic_gate(probe))
            except Exception as exc:  # 门坏了不能让内容直接过 —— 这是封群风险点
                _log.warning("topic_gate_failed | err=%s | 按拦截处理", exc)
                blocked = True
            if not blocked:
                kept.append(row)
        return kept, len(rows) - len(kept)

    try:
        if platform:
            method_map = {
                "weibo": crawler_hub.trends.weibo_hot,
                "bilibili": crawler_hub.trends.bilibili_hot,
                "douyin": crawler_hub.trends.douyin_hot,
                "baidu": crawler_hub.trends.baidu_hot,
            }
            func = method_map.get(platform)
            if not func:
                return ToolCallResult(ok=False, error=f"unknown_platform: {platform}")
            items = await func(limit)
            items, dropped = _drop_political(items)
            if dropped:
                _log.info(
                    "hot_trends_political_dropped | platform=%s | dropped=%d | kept=%d",
                    platform, dropped, len(items),
                )
            lines = [f"【{platform}热搜 Top{len(items)}】"]
            for i, item in enumerate(items, 1):
                heat = f" ({item.heat})" if item.heat else ""
                lines.append(f"{i}. {item.title}{heat}")
            return ToolCallResult(ok=True, data={"platform": platform, "count": len(items)},
                                display="\n".join(lines))
        else:
            trends = await crawler_hub.get_trends_cached()
            # 先过门再格式化、再入库 —— 顺序要紧：格式化后就只是一段文本，
            # 逐条判定的机会没了；入库更是把时政标题持久化。
            total_dropped = 0
            filtered_trends: dict[str, Any] = {}
            for plat, rows in trends.items():
                kept, dropped = _drop_political(list(rows))
                total_dropped += dropped
                filtered_trends[plat] = kept
            if total_dropped:
                _log.info(
                    "hot_trends_political_dropped | platform=all | dropped=%d",
                    total_dropped,
                )
            trends = filtered_trends
            text = crawler_hub.format_trends_text(trends, limit=limit)
            # 同时存入知识库
            kb = context.get("knowledge_base")
            if kb:
                for plat, items in trends.items():
                    for item in items[:limit]:
                        kb.add("trend", item.title, item.snippet or "", source=plat,
                                tags=[plat], extra={"heat": item.heat, "url": item.url})
            return ToolCallResult(ok=True, data={"platforms": list(trends.keys())}, display=text)
    except Exception as e:
        _log.warning("get_hot_trends_error | %s", e)
        return ToolCallResult(ok=False, error=f"trends_error: {e}")


async def _handle_search_zhihu(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable")

    mode = str(args.get("mode", "hot")).strip().lower()
    query = str(args.get("query", "")).strip()
    question_id = str(args.get("question_id", "")).strip()
    # registry 的 string 强转会把 null 变成 "None"，视为缺参而不是真的去搜 "None"。
    if query.lower() in {"none", "null"}:
        query = ""
    if question_id.lower() in {"none", "null"}:
        question_id = ""

    if mode not in {"hot", "search", "answers"}:
        return ToolCallResult(
            ok=False,
            error="invalid_mode",
            display="search_zhihu 的 mode 只支持 hot/search/answers，请重试。",
        )
    if mode == "search" and not query:
        return ToolCallResult(
            ok=False,
            error="missing_query",
            display="search_zhihu 的 search 模式需要 query 参数。",
        )
    if mode == "answers" and not question_id:
        return ToolCallResult(
            ok=False,
            error="missing_question_id",
            display="search_zhihu 的 answers 模式需要 question_id 参数。",
        )

    try:
        if mode == "hot":
            items = await crawler_hub.zhihu.hot_list(limit=15)
            lines = ["【知乎热榜】"]
            for i, item in enumerate(items, 1):
                heat = f" ({item.heat})" if item.heat else ""
                lines.append(f"{i}. {item.title}{heat}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        elif mode == "search" and query:
            items = await crawler_hub.zhihu.search(query, limit=8)
            lines = [f"【知乎搜索: {query}】"]
            for i, item in enumerate(items, 1):
                lines.append(f"{i}. {item.title}")
                if item.snippet:
                    lines.append(f"   {clip_text(item.snippet, 100)}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        elif mode == "answers" and question_id:
            items = await crawler_hub.zhihu.get_top_answers(question_id, limit=3)
            lines = [f"【知乎问题 {question_id} 高赞回答】"]
            for i, item in enumerate(items, 1):
                lines.append(f"{i}. {item.title}")
                lines.append(f"   {clip_text(item.snippet, 300)}")
            return ToolCallResult(ok=True, data={"count": len(items)}, display="\n".join(lines))

        return ToolCallResult(ok=False, error="invalid mode or missing query")
    except Exception as e:
        _log.warning("search_zhihu_error | %s", e)
        return ToolCallResult(ok=False, error=f"zhihu_error: {e}")


async def _handle_lookup_wiki(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    crawler_hub = context.get("crawler_hub")
    if not crawler_hub:
        return ToolCallResult(ok=False, error="crawler_unavailable")

    keyword = str(args.get("keyword", "")).strip()
    if not keyword:
        return ToolCallResult(ok=False, error="missing keyword")

    try:
        results = await crawler_hub.wiki.lookup(keyword)
        if not results:
            return ToolCallResult(ok=False, error="not_found", display=f"未找到 '{keyword}' 的百科信息")

        lines: list[str] = []
        for r in results:
            source_name = "百度百科" if r.source == "baike" else "维基百科"
            lines.append(f"【{source_name}: {r.title}】")
            lines.append(clip_text(r.snippet, 400))
            if r.url:
                lines.append(f"来源: {r.url}")
            lines.append("")

        # 存入知识库。原来是裸 kb.add：无置信度、无去重、无矛盾比对，
        # 抓到几条写几条。改走同一个带质量门的写入入口。
        kb = context.get("knowledge_base")
        if kb:
            for r in results:
                title = normalize_text(str(r.title or ""))
                snippet = normalize_text(str(r.snippet or ""))
                if not title or not snippet:
                    continue
                _write_knowledge_entry(
                    kb,
                    category="wiki",
                    title=title,
                    content=snippet,
                    tags=[keyword],
                    confidence=_WIKI_CONFIDENCE,
                    source=str(r.source or ""),
                    update_mode="wiki_lookup",
                )

        return ToolCallResult(ok=True, data={"results": len(results)}, display="\n".join(lines))
    except Exception as e:
        _log.warning("lookup_wiki_error | %s", e)
        return ToolCallResult(ok=False, error=f"wiki_error: {e}")


async def _handle_search_knowledge(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    kb = context.get("knowledge_base")
    if not kb:
        return ToolCallResult(ok=False, error="knowledge_base_unavailable")

    query = str(args.get("query", "")).strip()
    category = str(args.get("category", "")).strip()
    if not query:
        return ToolCallResult(ok=False, error="missing query")
    current_user_id = normalize_text(str(context.get("user_id", "")))
    current_conversation_id = normalize_text(str(context.get("conversation_id", "")))
    current_group_id = normalize_text(str(context.get("group_id", "")))
    is_cross_scope_reader = _has_cross_user_profile_access(context)

    def _build_query_variants(raw_query: str) -> list[str]:
        base = normalize_text(raw_query)
        if not base:
            return []

        variants: list[str] = []
        seen: set[str] = set()

        def _add(item: str) -> None:
            text = normalize_text(item)
            if not text:
                return
            key = text.lower()
            if key in seen:
                return
            seen.add(key)
            variants.append(text)

        _add(base)

        # 只做结构归一化：把标点换成空格。哪些词「重要」是语义判断，
        # 原来那张 22 词中文 stop_words 表（喜欢/音乐/查一下/什么/…）已删除 ——
        # 检索关键词由模型调用时自己填 query，它本来就能填。
        compact = re.sub(r"[，。！？!?,.;；:：\"'“”‘’（）()【】\[\]<>]+", " ", base)
        compact = normalize_text(compact)
        _add(compact)

        # `user:<id>` **不再**混进语义变体队列。
        # 原来无条件把它追加到末尾：语义变体在 FTS 上中文零命中，而 `user:<id>` 含冒号
        # 会走 tags 匹配，于是唯一能命中的变体永远是「本人画像」——
        # 实测 8 条中文 query（抑郁症 / 哈尔滨暴雨 / 图灵机是什么 / 量子力学…）
        # 8/8 返回同一条 music_preference，字面相关度 0，而模型收到 ok=True 会据此发言。
        # 作用域检索改走 kb.search_by_tag() 的显式接口，见下面的 _fetch_profile_entries。
        return variants[:10]

    def _fetch_profile_entries() -> list[Any]:
        """当前用户的画像类条目，按标签精确取，与 query 相关度无关。"""
        if not current_user_id:
            return []
        fetch = getattr(kb, "search_by_tag", None)
        if not callable(fetch):
            return []
        try:
            return list(fetch(f"user:{current_user_id}", category=category or "", limit=4) or [])
        except Exception:
            _log.warning("knowledge_profile_fetch_failed | user=%s", current_user_id, exc_info=True)
            return []

    def _normalize_entry_tags(entry: Any) -> set[str]:
        raw_tags = getattr(entry, "tags", [])
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]
        if not isinstance(raw_tags, list):
            return set()
        out: set[str] = set()
        for raw in raw_tags:
            text = normalize_text(str(raw)).lower()
            if text:
                out.add(text)
        return out

    def _scope_score(entry: Any) -> int:
        tags = _normalize_entry_tags(entry)
        score = 0
        if current_user_id and f"user:{current_user_id}".lower() in tags:
            score += 100
        if current_conversation_id and f"conversation:{current_conversation_id}".lower() in tags:
            score += 40
        if current_group_id and f"group:{current_group_id}".lower() in tags:
            score += 20
        return score

    def _is_readable_scope(entry: Any) -> bool:
        """作用域标签是硬边界，不是排序权重。

        原实现只在 _scope_score 里给 user:/conversation:/group: 加分（+100/+40/+20），
        排完照样把全部条目返回给模型 —— 实测 permission_level='user' 查
        `user:999888777` 拿到别人的「我有抑郁症在吃舍曲林」「以后叫我小柯」。
        标签是写入时打的结构事实，这里按结构事实**丢弃**越界条目，不是降权。
        super_admin 例外，与 remember_user_fact / recall_about_user 用的
        _has_cross_user_profile_access 同一判据。
        """
        if is_cross_scope_reader:
            return True
        tags = _normalize_entry_tags(entry)
        owner_tags = {tag for tag in tags if tag.startswith("user:")}
        if owner_tags and (not current_user_id or f"user:{current_user_id}".lower() not in owner_tags):
            return False
        conv_tags = {tag for tag in tags if tag.startswith("conversation:")}
        if conv_tags and (
            not current_conversation_id or f"conversation:{current_conversation_id}".lower() not in conv_tags
        ):
            return False
        group_tags = {tag for tag in tags if tag.startswith("group:")}
        if group_tags and (not current_group_id or f"group:{current_group_id}".lower() not in group_tags):
            return False
        return True

    try:
        query_variants = _build_query_variants(query)
        category_variants: list[str] = [category] if category else [""]
        if category:
            category_variants.append("")  # category 限制命不中时自动放宽到全库

        entries: list[Any] = []
        seen_ids: set[int] = set()
        dropped_out_of_scope = 0
        for cat in category_variants:
            for q in query_variants:
                try:
                    rows = kb.search(q, category=cat, limit=8)
                except Exception:
                    rows = []
                for row in rows:
                    rid = int(getattr(row, "id", 0) or 0)
                    if rid and rid in seen_ids:
                        continue
                    # 越界条目在去重登记之前就丢掉，且**不占** limit 名额。
                    if not _is_readable_scope(row):
                        dropped_out_of_scope += 1
                        continue
                    if rid:
                        seen_ids.add(rid)
                    entries.append(row)
                if len(entries) >= 8:
                    break
            if len(entries) >= 8:
                break

        # 画像条目单独取，不与 query 相关度混算，见 _fetch_profile_entries 注释。
        profile_ids: set[int] = set()
        for row in _fetch_profile_entries():
            rid = int(getattr(row, "id", 0) or 0)
            if rid and rid in seen_ids:
                profile_ids.add(rid)
                continue
            if rid:
                seen_ids.add(rid)
                profile_ids.add(rid)
            entries.append(row)

        if dropped_out_of_scope:
            _log.info(
                "knowledge_search_scope_filtered | user=%s | dropped=%d | kept=%d",
                current_user_id, dropped_out_of_scope, len(entries),
            )

        if entries:
            entries = sorted(
                entries,
                key=lambda row: (
                    _scope_score(row),
                    float(getattr(row, "created_at", 0.0) or 0.0),
                ),
                reverse=True,
            )

        if not entries:
            return ToolCallResult(
                ok=True,
                data={
                    "count": 0,
                    "query_variants": query_variants,
                    "dropped_out_of_scope": dropped_out_of_scope,
                },
                display=f"知识库中未找到 '{query}' 相关内容",
            )

        lines = [f"【知识库搜索: {query}】"]
        result_rows: list[dict[str, Any]] = []
        scoped_hits = 0
        query_matched = 0
        for e in entries:
            cat_tag = f"[{e.category}]" if e.category else ""
            scope_score = _scope_score(e)
            if scope_score >= 100:
                scoped_hits += 1
            entry_id = int(getattr(e, "id", 0) or 0)
            # 画像条目是按 user: 标签取的，与 query 无关。标出来源，否则模型会把
            # 「你最喜欢宋岳庭-上帝为何要这样」当成「抑郁症」这个 query 的检索命中。
            from_profile = bool(entry_id and entry_id in profile_ids)
            if not from_profile:
                query_matched += 1
            scope_tag = " (当前用户)" if scope_score >= 100 else ""
            origin_tag = " (本人画像，与本次查询无关)" if from_profile else ""
            lines.append(f"- {cat_tag} {e.title}{scope_tag}{origin_tag}")
            if e.content:
                lines.append(f"  {clip_text(e.content, 200)}")
            tags = [normalize_text(str(item)) for item in (getattr(e, "tags", []) or []) if normalize_text(str(item))]
            result_rows.append(
                {
                    "id": entry_id,
                    "category": normalize_text(str(getattr(e, "category", ""))),
                    "title": normalize_text(str(getattr(e, "title", ""))),
                    "content": normalize_text(str(getattr(e, "content", ""))),
                    "source": normalize_text(str(getattr(e, "source", ""))),
                    "tags": tags,
                    "from_profile": from_profile,
                }
            )
        if not query_matched:
            lines.insert(1, f"（没有与 '{query}' 字面相关的条目，以下只是本人画像记录）")
        return ToolCallResult(
            ok=True,
            data={
                "count": len(entries),
                "results": result_rows,
                "query_variants": query_variants,
                "scoped_hits": scoped_hits,
                "query_matched": query_matched,
                "profile_only": query_matched == 0,
                "dropped_out_of_scope": dropped_out_of_scope,
            },
            display="\n".join(lines),
        )
    except Exception as e:
        _log.warning("search_knowledge_error | %s", e)
        return ToolCallResult(ok=False, error=f"knowledge_error: {e}")


async def _handle_learn_knowledge(args: dict[str, Any], context: dict[str, Any]) -> ToolCallResult:
    kb = context.get("knowledge_base")
    if not kb:
        return ToolCallResult(ok=False, error="knowledge_base_unavailable")

    def _infer_title_from_content(text: str) -> str:
        body = normalize_text(text)
        if not body:
            return ""
        m = re.match(r"([^，。！？\n:：]{1,40})(?:是|指|叫|一般是|通常是)", body)
        if m:
            return normalize_text(m.group(1))[:40]
        m2 = re.match(r"([^，。！？\n]{1,40})[:：]", body)
        if m2:
            return normalize_text(m2.group(1))[:40]
        fallback = normalize_text(body.split("，", 1)[0].split("。", 1)[0])
        return fallback[:40]

    title = normalize_text(str(args.get("title", "")))
    content = normalize_text(str(args.get("content", "")))
    if not content:
        content = normalize_text(str(args.get("text", "")))
    # kind 是模型声明的性质，不填按 fact 走普通入库 —— 任何未声明的调用都不会被改道去改名。
    kind = normalize_text(str(args.get("kind", ""))).lower() or "fact"
    if kind not in _LEARN_KNOWLEDGE_KINDS:
        kind = "fact"
    category = str(args.get("category", "learned")).strip()
    tags_value = args.get("tags", "")
    tags: list[str] = []
    if isinstance(tags_value, str):
        tags = [t.strip() for t in tags_value.split(",") if t.strip()]
    elif isinstance(tags_value, list):
        tags = [normalize_text(str(t)) for t in tags_value if normalize_text(str(t))]

    if not title and content:
        title = _infer_title_from_content(content)
        if title:
            tags = list(dict.fromkeys(tags + ["auto_title"]))

    if not title:
        return ToolCallResult(ok=False, error="missing title")
    if not content:
        return ToolCallResult(ok=False, error="missing content")
    # 意图由模型声明，不再嗅探内容。原来这里是
    # `if looks_like_preferred_name_knowledge(title, content, tags):` ——
    # 一张字面正则表，实测把「我是程序员」「我叫小柯」「我是这个群的群主」「我是女生」
    # 这类事实全部劫持成改名请求：知识条目**静默丢弃**、用户被当面改口叫「程序员」，
    # 而模型收到 ok=True「已更新用户偏好称呼」，于是对用户说「记住了」却查不到。
    # 枚举与 core/knowledge_updater.py 的抽取器同一套（fact/preference/
    # music_preference/preferred_name/correction）。
    if kind == "preferred_name":
        cfg = context.get("config", {})
        bot_cfg = cfg.get("bot", {}) if isinstance(cfg, dict) and isinstance(cfg.get("bot"), dict) else {}
        bot_aliases = [bot_cfg.get("name", ""), *(bot_cfg.get("nicknames", []) or []), "yuki", "yukiko", "雪"]
        source_text = normalize_text(str(context.get("message_text", ""))) or normalize_text(
            str(context.get("original_message_text", ""))
        )
        decision = assess_preferred_name_learning(
            source_text or content,
            is_private=bool(context.get("is_private", False)),
            mentioned=bool(context.get("mentioned", False)),
            explicit_bot_addressed=bool(context.get("explicit_bot_addressed", False)),
            bot_aliases=bot_aliases,
            at_other_user_ids=context.get("at_other_user_ids", []) or [],
            reply_to_user_id=normalize_text(str(context.get("reply_to_user_id", ""))),
            bot_id=normalize_text(str(context.get("bot_id", ""))),
            # 称呼取模型写进 content 的声明值，不再对原文跑正则：
            # 正则要求整句以「叫我X」收尾，实测带标点、带「大家」、带「666」就抽不出来，
            # 用户只会收到「群聊称呼学习需要明确点名我…」这句与真实原因无关的文案。
            declared_name=content,
        )
        if not decision.allow:
            return ToolCallResult(
                ok=False,
                error=f"preferred_name_guard:{decision.reason}",
                display="群聊称呼学习需要明确点名我、明确声明，并且不能在起哄语境里。",
            )
        memory = context.get("memory_engine")
        if memory is None or not hasattr(memory, "set_preferred_name"):
            return ToolCallResult(ok=False, error="memory_engine_unavailable", display="称呼记忆模块未初始化")
        ok, message, payload = memory.set_preferred_name(
            target_user_id=normalize_text(str(context.get("user_id", ""))),
            preferred_name=decision.candidate,
            actor="agent.learn_knowledge",
            conversation_id=normalize_text(str(context.get("conversation_id", ""))),
            note="Agent 显式学习用户偏好称呼",
            reason="agent_learn_preferred_name",
        )
        if not ok:
            return ToolCallResult(ok=False, error="preferred_name_update_failed", data=payload or {}, display=message)
        preferred_name = normalize_text(str(payload.get("preferred_name", decision.candidate)))
        return ToolCallResult(
            ok=True,
            data=payload or {},
            display=f"已更新用户偏好称呼: {preferred_name or decision.candidate}",
        )
    # 安全门：拒绝机制保留，判据换成模型声明的 safety_review（原来是硬编码脏词表）。
    safety_review = _resolve_safety_review(args)
    if safety_review == "unsafe":
        _kb_record_audit(
            kb,
            "knowledge_write_rejected",
            title=title,
            category=category,
            reason="safety_review_unsafe",
            declared_by="model",
        )
        return ToolCallResult(
            ok=False,
            error="unsafe_knowledge_content",
            display="按你自己的安全判定，这条内容不写入知识库。",
        )
    if category not in ("fact", "meme", "learned"):
        category = "learned"

    normalized_tags: list[str] = []
    seen_tags: set[str] = set()

    def _append_tag(raw: str) -> None:
        tag = normalize_text(str(raw))
        if not tag:
            return
        key = tag.lower()
        if key in seen_tags:
            return
        seen_tags.add(key)
        normalized_tags.append(tag)

    for item in tags:
        _append_tag(item)
    current_user_id = normalize_text(str(context.get("user_id", "")))
    current_conversation_id = normalize_text(str(context.get("conversation_id", "")))
    current_group_id = int(context.get("group_id", 0) or 0)
    if current_user_id:
        _append_tag(f"user:{current_user_id}")
    if current_conversation_id:
        _append_tag(f"conversation:{current_conversation_id}")
    if current_group_id > 0:
        _append_tag(f"group:{current_group_id}")
    normalized_tags = normalized_tags[:20]

    try:
        confidence = _parse_declared_confidence(args.get("confidence"))
        # 走 upsert_conflict_checked 而不是裸 kb.add：后者不写 confidence / update_mode /
        # is_correction，导致 Agent 主动学到的知识在检索重排里永远垫底，
        # 而且完全绕过去重与矛盾裁决（vision-knowledge-base.md G4.1）。
        result = _write_knowledge_entry(
            kb,
            category=category,
            title=title,
            content=content,
            tags=normalized_tags,
            confidence=confidence,
            # kind='correction' 与 is_correction=True 等价，与 core/knowledge_updater.py
            # 的 `is_correction = bool(item.get("is_correction") or kind == "correction")` 对齐，
            # 否则模型声明了 correction 却仍被当成低把握新值而覆盖不了旧值。
            is_correction=bool(args.get("is_correction", False)) or kind == "correction",
        )
        action = result.get("action") if isinstance(result, dict) else None
        entry_id = result.get("id") if isinstance(result, dict) else None
        _kb_record_audit(
            kb,
            "knowledge_write_accepted",
            knowledge_id=entry_id,
            title=title,
            category=category,
            action=str(action or "unknown"),
            confidence=confidence,
            safety_review=safety_review,
            tags=normalized_tags,
        )
        if action == "disputed":
            return ToolCallResult(
                ok=True,
                data={"id": entry_id, "category": category, "action": action, "tags": normalized_tags},
                display=(
                    f"[{category}] {title} 已存在把握更高的旧值，这次的新值记为待裁决而没有覆盖。"
                    "确定要改的话把 is_correction 设为 true 再写一次。"
                ),
            )
        if action == "duplicate":
            return ToolCallResult(
                ok=True,
                data={"id": entry_id, "category": category, "action": action, "tags": normalized_tags},
                display=f"[{category}] 库里已有同样内容的条目，这次只把「{title}」记成它的别名。",
            )
        return ToolCallResult(
            ok=True,
            data={"id": entry_id, "category": category, "action": action, "tags": normalized_tags},
            display=f"已学习: [{category}] {title}",
        )
    except Exception as e:
        _log.warning("learn_knowledge_error | %s", e)
        return ToolCallResult(ok=False, error=f"learn_error: {e}")


def _parse_declared_confidence(raw: Any, default: float = 0.7) -> float:
    """模型声明的把握程度。非法值回落到默认，不猜。"""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return max(0.0, min(1.0, value))


def _write_knowledge_entry(
    kb: Any,
    *,
    category: str,
    title: str,
    content: str,
    tags: list[str],
    confidence: float,
    source: str = "chat",
    update_mode: str = "agent",
    is_correction: bool = False,
) -> dict[str, Any]:
    """统一的知识写入入口：带质量门的 upsert，kb 不支持时回落到裸 add。

    回落分支是为 stub / 老 kb 实现留的兼容路径，不是双写。
    """
    upsert = getattr(kb, "upsert_conflict_checked", None)
    if not callable(upsert):
        return {"ok": True, "action": "inserted", "id": kb.add(
            category=category, title=title, content=content, source=source, tags=tags
        )}
    result = upsert(
        category=category,
        title=title,
        content=content,
        source=source,
        tags=tags,
        confidence=confidence,
        update_mode=update_mode,
        mark_correction=is_correction,
    )
    return result if isinstance(result, dict) else {"ok": True, "action": "inserted", "id": None}


# ─────────────────────────────────────────────
# Daily Report & User Portrait tools
# ─────────────────────────────────────────────

