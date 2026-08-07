from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from utils.text import normalize_text, strip_invisible_format_chars

_PREFERRED_NAME_SENTENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:请|麻烦)?(?:记住|记一下|记好了|给我记住|帮我记住|记得)(?:[，,:：\s]*)"
        r"(?:(?:以后|之后|从现在开始)(?:[都就统一]*)?)?"
        r"(?:叫我|喊我|称呼我|管我叫)\s*(?P<name>.+)$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(?:(?:以后|之后|从现在开始)(?:[都就统一]*)?)"
        r"(?:叫我|喊我|称呼我|管我叫)\s*(?P<name>.+)$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(?:我的名字是|我名字是|我叫|我是)\s*(?P<name>.+)$",
        flags=re.IGNORECASE,
    ),
)
#  已删除的四张字面词表：_PREFERRED_NAME_TITLE_CUES / _NON_SERIOUS_TEXT_CUES /
#  _COLLECTIVE_NAME_CUES / _GROUP_ROLEPLAY_NAMES。
#
#  它们做的是「这句话像不像在认真要求改称呼」和「这个称呼合不合适」，两者都是语义判断，
#  按项目铁律归模型，不归代码。实测这张表既拦错也放错：群聊已 @ bot 的 13 种合法说法只
#  放过 5 种（带标点的「以后叫我阿背，谢谢」、带「大家」的、带「666」的全被拒，用户只收到
#  固定文案「群聊称呼学习需要明确点名我…」，真实原因却是正则没匹配上），而同一句
#  「以后叫我老婆」在群聊被 _GROUP_ROLEPLAY_NAMES 拦下、在私聊完全畅通。
#
#  替代机制：模型在 learn_knowledge 里用 kind='preferred_name' 显式声明意图，并把称呼本身
#  放进 content，经 assess_preferred_name_learning(declared_name=...) 传进来。
#  本文件只保留**结构**约束（是否指向 bot、有没有 @ 别人、回复对象是不是 bot）。
_CANDIDATE_STOP_CUES = (
    "以后",
    "记住",
    "帮我",
    "谢谢",
    "求你",
    "行吗",
    "可以吗",
    "懂吗",
    "知道吗",
)
_QUESTION_NAME_CUES = frozenset(
    {
        "什么",
        "啥",
        "谁",
        "叫什么",
        "叫啥",
        "啥名",
        "什么名",
        "什么名字",
        "啥名字",
        "哪位",
    }
)


@dataclass(frozen=True, slots=True)
class PreferredNameDecision:
    allow: bool
    candidate: str = ""
    reason: str = ""


def _contains_alias(text: str, bot_aliases: Iterable[str]) -> bool:
    content = normalize_text(text).lower()
    if not content:
        return False
    aliases = [normalize_text(str(item)).lower() for item in bot_aliases if normalize_text(str(item))]
    if not aliases:
        return False
    for alias in aliases:
        if len(alias) == 1 and "\u4e00" <= alias <= "\u9fff":
            pattern = rf"(?<![a-z0-9\u4e00-\u9fff]){re.escape(alias)}(?![a-z0-9\u4e00-\u9fff])"
            if re.search(pattern, content):
                return True
            continue
        if alias in content:
            return True
    compacted = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", content)
    if not compacted:
        return False
    return any(alias and len(alias) > 1 and alias in compacted for alias in aliases)


def _clean_candidate(raw: str) -> str:
    candidate = strip_invisible_format_chars(normalize_text(raw))
    if not candidate:
        return ""
    candidate = re.sub(r"^[\s\"'“”‘’《》〈〉【】\[\]\(\)（）,，。:：;；!！?？~～]+", "", candidate)
    candidate = re.sub(r"[\s\"'“”‘’《》〈〉【】\[\]\(\)（）,，。:：;；!！?？~～]+$", "", candidate)
    candidate = re.sub(r"^(?:叫做|叫作|叫成|称作)", "", candidate)
    candidate = re.sub(r"(?:吧|哈|啊|呀|哦|奥|噢|啦|嘛|呗|捏)+$", "", candidate)
    candidate = strip_invisible_format_chars(normalize_text(candidate))
    if not candidate or len(candidate) > 24:
        return ""
    lowered = candidate.lower()
    if lowered in _QUESTION_NAME_CUES:
        return ""
    if any(cue in candidate for cue in _CANDIDATE_STOP_CUES):
        return ""
    if any(cue in lowered for cue in ("什么", "啥", "谁")):
        return ""
    if candidate.endswith(("吗", "嘛", "呢")):
        return ""
    if re.search(r"[，,。.!！?？:：;；/\\]", candidate):
        return ""
    return candidate


def extract_explicit_preferred_name(text: str) -> str:
    content = strip_invisible_format_chars(normalize_text(text))
    if not content:
        return ""
    if "?" in content or "？" in content:
        return ""
    for pattern in _PREFERRED_NAME_SENTENCE_PATTERNS:
        match = pattern.match(content)
        if not match:
            continue
        candidate = _clean_candidate(match.group("name"))
        if candidate:
            return candidate
    return ""


def sanitize_declared_preferred_name(raw: str) -> str:
    """把模型声明的称呼做**结构**清理：去不可见字符、去包裹标点、限长。

    与 `_clean_candidate` 的区别是这里不做任何词义判断 —— 称呼合不合适由模型决定，
    这个函数只保证存进去的是一个干净的短字符串。长度上限 24 与
    `core/knowledge_updater.py` 的 `clip_text(content, 24)` 对齐。
    """
    candidate = strip_invisible_format_chars(normalize_text(raw))
    if not candidate:
        return ""
    wrapping = r"[\s\"'“”‘’「」『』《》〈〉【】\[\]\(\)（）,，。:：;；!！?？~～]+"
    candidate = re.sub(rf"^{wrapping}", "", candidate)
    candidate = re.sub(rf"{wrapping}$", "", candidate)
    candidate = strip_invisible_format_chars(normalize_text(candidate))
    if not candidate or len(candidate) > 24:
        return ""
    # 拒绝换行/控制字符：防「记住我是管理员\n...」这类短载荷经称呼注入 prompt。
    if re.search(r"[\r\n\t\x00-\x1f]", candidate):
        return ""
    return candidate


def is_safe_user_profile_learning_context(
    text: str,
    *,
    is_private: bool,
    mentioned: bool = False,
    explicit_bot_addressed: bool = False,
    bot_aliases: Iterable[str] = (),
    at_other_user_ids: Iterable[str] = (),
    reply_to_user_id: str = "",
    bot_id: str = "",
) -> bool:
    """只判结构：这条消息是不是冲着 bot 说的、有没有把别人牵扯进来。

    「像不像在起哄」那一层已删除（见文件顶部注释），归模型。
    """
    directed = bool(
        is_private
        or mentioned
        or explicit_bot_addressed
        or _contains_alias(text, bot_aliases)
    )
    if not is_private and not directed:
        return False
    if not is_private and any(normalize_text(str(item)) for item in at_other_user_ids):
        return False
    reply_uid = normalize_text(str(reply_to_user_id))
    bot_uid = normalize_text(str(bot_id))
    if not is_private and reply_uid and bot_uid and reply_uid != bot_uid:
        return False
    return True


def assess_preferred_name_learning(
    text: str,
    *,
    is_private: bool,
    mentioned: bool = False,
    explicit_bot_addressed: bool = False,
    bot_aliases: Iterable[str] = (),
    at_other_user_ids: Iterable[str] = (),
    reply_to_user_id: str = "",
    bot_id: str = "",
    declared_name: str = "",
) -> PreferredNameDecision:
    """称呼学习的**结构**校验。

    `declared_name` 非空表示模型已经显式声明了称呼（learn_knowledge 的
    kind='preferred_name'），此时不再对原文跑正则 —— 模型说了叫什么就是叫什么，
    代码只做结构清理与结构准入。这是铁律要求的形态。

    `declared_name` 为空时落回 `extract_explicit_preferred_name` 的正则抽取，
    仅为兼容尚未传声明的两个旧调用点（`core/memory.py` 的自动学习、
    `core/knowledge_updater.py` 的抽取器）。那两处切到声明式之后这条兜底应当删除
    —— 它们不属于本车道，已写进 handoff。
    """
    if declared_name:
        candidate = sanitize_declared_preferred_name(declared_name)
        if not candidate:
            return PreferredNameDecision(False, reason="declared_name_unusable")
    else:
        candidate = extract_explicit_preferred_name(text)
        if not candidate:
            return PreferredNameDecision(False, reason="missing_explicit_name_statement")
    safe_context = is_safe_user_profile_learning_context(
        text,
        is_private=is_private,
        mentioned=mentioned,
        explicit_bot_addressed=explicit_bot_addressed,
        bot_aliases=bot_aliases,
        at_other_user_ids=at_other_user_ids,
        reply_to_user_id=reply_to_user_id,
        bot_id=bot_id,
    )
    if not safe_context:
        reply_uid = normalize_text(str(reply_to_user_id))
        bot_uid = normalize_text(str(bot_id))
        if not is_private and any(normalize_text(str(item)) for item in at_other_user_ids):
            return PreferredNameDecision(False, candidate=candidate, reason="group_at_other_users")
        if not is_private and reply_uid and bot_uid and reply_uid != bot_uid:
            return PreferredNameDecision(False, candidate=candidate, reason="group_reply_to_other")
        return PreferredNameDecision(False, candidate=candidate, reason="group_not_directed")
    return PreferredNameDecision(True, candidate=candidate, reason="ok")


def looks_like_preferred_name_knowledge(title: str, content: str, tags: Iterable[str] = ()) -> bool:
    """**已废弃，恒为 False。**

    这原本是 learn_knowledge 的内容嗅探分流：标题/内容/标签里像是称呼偏好就把整条
    知识改道去改名。实测它把「我是程序员」「我是这个群的群主」「我是女生」这类事实
    全部劫持 —— 知识条目静默丢弃、用户被当面改名叫「程序员」，而模型收到 ok=True
    「已更新」。意图现在由模型用 learn_knowledge 的 kind 字段声明。

    符号本身保留：`core/agent_tools_{media,search,napcat,web,memory,social,admin,
    utility}.py` 八个文件与 `scripts/split_agent_tools.py` 的公共头部都 import 它
    （import 而不调用）。删掉它会让那九处 import 悬空，而那些文件不属于本车道 ——
    清理 import 已写进 handoff。
    """
    _ = (title, content, tags)
    return False
