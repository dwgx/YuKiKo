from __future__ import annotations


import asyncio

from collections import Counter, defaultdict, deque

from dataclasses import dataclass, field

from datetime import datetime, timedelta, timezone

import logging

import re

from typing import Any


from utils.text import normalize_text, tokenize


_logger = logging.getLogger("yukiko.trigger")


# 结构事实模式：显式输入的命令令牌、链接、视频号、文件扩展名。
# 这些是"消息里客观存在什么"，不是"用户想干什么"，因此允许留在注意力门里。

_TYPED_COMMAND_PATTERN = re.compile(r"^[!/][a-z0-9_.:-]+", flags=re.IGNORECASE)

_QUESTION_MARKS = frozenset("?？")

_URL_PATTERN = re.compile(r"https?://", flags=re.IGNORECASE)

_VIDEO_ID_PATTERN = re.compile(r"\b(?:bv[a-z0-9]{10}|av\d{4,})\b", flags=re.IGNORECASE)

_FILE_EXTENSION_PATTERN = re.compile(
    r"\.(?:png|jpe?g|gif|webp|bmp|mp4|webm|mov|m4v|mp3|wav|flac|ogg|zip|7z|rar"
    r"|exe|apk|ipa|msi|pdf|docx?|xlsx?|pptx?)\b",
    flags=re.IGNORECASE,
)

# ── 多模态占位符解析（与 app_helpers.py `_build_multimodal_text()` 强耦合）──
#
# 裸媒体消息（只有图/视频/语音段、没有文字）本身没有文本，app.py:1484 会把它
# 拼成一行机器文本 `MULTIMODAL_EVENT user sent multimodal message: image:[image]`
# 再交给引擎，而 core/engine.py 在调用 trigger 之前会 normalize_text() 把换行压成
# 空格，所以这里只能按**单行**解析。
#
# 这是耦合点，不是巧合：一旦 `_build_multimodal_text()` 换前缀、换分隔符或换 token
# 形态，裸媒体门会**静默失效**（不会报错，只是又开始对着图片说话）。
# tests/test_trigger_attention_gate_overhaul.py 把这些字面量逐字钉住了，
# 改那边的格式会先在测试里红。engine 显式填 TriggerInput.media_types /
# has_user_text 时不走这条解析，那是更可靠的路。

_MULTIMODAL_MARKER_PATTERN = re.compile(
    r"^MULTIMODAL_EVENT(?:_AT)?\s+"
    r"(?:user mentioned bot and sent multimodal message:"
    r"|user sent multimodal message:)\s*",
    flags=re.IGNORECASE,
)

_MEDIA_SEGMENT_TYPES = frozenset({"image", "video", "record", "audio", "forward"})

_MEDIA_TOKEN_SEPARATOR = "|"

# 闭合形态的媒体值：`[image]` 占位、URL、本地绝对路径、Windows 盘符路径。
# 这些有明确右边界，边界之后的内容就是用户自己打的字。
# 不匹配这个模式的值只可能是 QQ 表情包/图片的自由文本 summary（含空格），
# 没有右边界可言，整段都算媒体附带信息 —— 宁可当成裸图沉默，也不要把
# 别人表情包上的配文当成有人在跟机器人说话。

_CLOSED_MEDIA_VALUE_PATTERN = re.compile(
    r"^(?:\[[^\]]*\]|https?://\S+|/\S+|[a-z]:[\\/]\S+)",
    flags=re.IGNORECASE,
)

# core/engine.py `_record_runtime_group_chat` 给每行群上下文拼的 `昵称(QQ:12345): ` 前缀。
# 这段是机器生成的，不能参与"群里在聊什么"的热词统计。

_CONTEXT_ROW_PREFIX_PATTERN = re.compile(r"^.{0,48}?\(QQ:\d{3,16}\)\s*:\s*")

# 旁听探测配额的滑动窗口。

_PROBE_BUDGET_WINDOW = timedelta(hours=1)

_TYPED_COMMAND_SCORE = 1.3

_STRUCTURAL_LOCATOR_SCORE = 0.7

_STRUCTURAL_SIGNAL_CAP = 3.0

# 两个以上结构定位符才够这个门（显式命令已在更早的分支短路掉）。

_STRUCTURAL_PROBE_THRESHOLD = 1.35


@dataclass(slots=True)
class TriggerInput:

    conversation_id: str

    user_id: str

    text: str

    mentioned: bool

    is_private: bool

    timestamp: datetime

    at_other_user_ids: list[str] = field(default_factory=list)

    reply_to_user_id: str = ""

    bot_id: str = ""

    # 本条消息带了哪些媒体段（image/video/record/audio/forward）。
    # engine 不填时由 `_split_multimodal_marker()` 从占位符文本里解析，
    # 填了就以这里为准 —— 显式事实优先于解析。
    media_types: list[str] = field(default_factory=list)

    # 用户自己是否打了字。None = 未声明，交给 trigger 自行推断。
    has_user_text: bool | None = None

    # 用户自己打的那段字（已剥掉 MULTIMODAL_EVENT 占位符）。None = 未声明。
    # 为什么需要它而不是只有 has_user_text：图片 summary 是**没有右边界的自由文本**
    # （日志里真实出现过 `image:哎呦，你干嘛～`），而 core/engine.py:1022 的
    # normalize_text 会把 app.py 拼的换行压掉，占位符与用户文本在 trigger 眼里连成一行。
    # 只给布尔的话，「表情包 + 用户喊 yuki」这一轮门是开了，但 user_text 仍是空、
    # 匹配不到别名，最后仍落 not_directed —— 实测过，直接违反「喊 yuki 就要回」。
    # engine 侧有现成的 _extract_multimodal_user_text()，让它把结果直接送过来。
    user_text: str | None = None

    # 结构化日志串联用，与 EngineMessage.trace_id 同一个值。
    trace_id: str = ""


@dataclass(slots=True)
class _MessageFacts:
    """本条消息的结构事实：带了哪些媒体段、用户自己打了什么字。

    只有客观事实，没有任何"这句话像在提需求"的语义判断。
    """

    media_types: list[str]

    # 剥掉机器占位符之后剩下的、用户自己输入的文本。
    # 语音转写 `[语音内容] xxx` 算用户文本 —— 那是用户说的话。
    user_text: str

    # 有媒体段且用户一个字都没打。
    is_media_only: bool


@dataclass(slots=True)
class TriggerResult:

    should_handle: bool

    reason: str

    active_session: bool = False

    followup_candidate: bool = False

    listen_probe: bool = False

    overload_active: bool = False

    busy_messages: int = 0

    busy_users: int = 0

    scene_hint: str = "chat"

    proactive: bool = False

    ai_gate: bool = True

    priority: int = 0


class TriggerEngine:
    """负责会话状态、节流与轻量触发语义判定。"""

    def __init__(
        self,
        trigger_config: dict[str, Any],
        bot_config: dict[str, Any],
        triggers_file_config: dict[str, Any] | None = None,
        sensitive_config: dict[str, Any] | None = None,
    ):

        _ = (triggers_file_config, sensitive_config)  # 兼容旧调用

        aliases = {normalize_text(str(bot_config.get("name", ""))).lower()}

        for item in bot_config.get("nicknames", []) or []:

            aliases.add(normalize_text(str(item)).lower())

        # 常用默认别名兜底，避免配置缺省时喊不醒。

        aliases.update({"yuki", "yukiko", "雪"})

        aliases.discard("")

        self.bot_aliases = aliases

        self.alias_patterns: list[tuple[str, re.Pattern | None]] = []
        for alias in self.bot_aliases:
            if len(alias) == 1 and "\u4e00" <= alias <= "\u9fff":
                pattern = re.compile(rf"(?<![a-z0-9\u4e00-\u9fff]){re.escape(alias)}(?![a-z0-9\u4e00-\u9fff])")
                self.alias_patterns.append((alias, pattern))
            else:
                self.alias_patterns.append((alias, None))

        self.session_timeout = timedelta(
            minutes=float(trigger_config.get("active_session_timeout_minutes", 8))
        )

        # active_session 不再是"命中即无条件放行"。只有这个短窗内算"正在对话"，
        # 超出之后它降级成证据：仍然填 TriggerResult.active_session 供下游读，
        # 并给旁听分加成，但不自己拍板放行。
        self.active_session_free_window = timedelta(
            seconds=max(0, int(trigger_config.get("active_session_free_window_seconds", 90)))
        )

        self.active_session_score_bonus = max(
            0.0, float(trigger_config.get("active_session_score_bonus", 0.6))
        )

        # 裸媒体（只有图/视频/语音段、没有文字）只在被点名时回应。
        self.media_only_requires_directed = bool(
            trigger_config.get("media_only_requires_directed", True)
        )

        self.media_only_allow_in_followup = bool(
            trigger_config.get("media_only_allow_in_followup", False)
        )

        self.followup_reply_window = timedelta(
            seconds=max(5, int(trigger_config.get("followup_reply_window_seconds", 20)))
        )

        self.followup_max_turns = max(
            1, int(trigger_config.get("followup_max_turns", 2))
        )

        self.busy_window = timedelta(
            seconds=max(15, int(trigger_config.get("busy_window_seconds", 60)))
        )

        # 默认不做全局旁听；由 control.undirected_policy 或显式 trigger 配置开启。

        self.ai_listen_enable = bool(trigger_config.get("ai_listen_enable", False))

        self.ai_listen_interval = timedelta(
            seconds=max(15, int(trigger_config.get("ai_listen_interval_seconds", 45)))
        )

        self.ai_listen_min_messages = max(
            1, int(trigger_config.get("ai_listen_min_messages", 8))
        )

        self.ai_listen_min_unique_users = max(
            1, int(trigger_config.get("ai_listen_min_unique_users", 3))
        )

        self.ai_listen_keyword_enable = bool(
            trigger_config.get("ai_listen_keyword_enable", True)
        )
        raw_keywords = trigger_config.get("ai_listen_keywords", [])
        keywords: list[str] = []
        if isinstance(raw_keywords, str):
            keywords = [
                normalize_text(item).lower()
                for item in re.split(r"[\s,，;；\n]+", raw_keywords)
                if normalize_text(item)
            ]
        elif isinstance(raw_keywords, list):
            keywords = [
                normalize_text(str(item)).lower()
                for item in raw_keywords
                if normalize_text(str(item))
            ]
        self.ai_listen_keywords = list(dict.fromkeys(keywords))
        self.ai_listen_min_keyword_hits = max(
            1, int(trigger_config.get("ai_listen_min_keyword_hits", 1))
        )

        self.ai_listen_min_score = max(
            0.5, float(trigger_config.get("ai_listen_min_score", 1.2))
        )

        # 关键词命中能否**单独**放行旁听。默认关：那条路绕过了 ai_listen_min_score，
        # 等于让一个词形否决整套阈值，也是"人机感"的直接来源。
        # 关掉之后 keyword_hits 只经 `_build_listen_score()` 加分。
        self.ai_listen_keyword_pass_enable = bool(
            trigger_config.get("ai_listen_keyword_pass_enable", False)
        )

        # 每会话每小时的旁听探测硬上限。只靠 ai_listen_interval_seconds 冷却时
        # 理论上限是 3600/interval（45s → 80 次/小时），provider 撑不住。
        # 0 表示完全关闭旁听探测。
        self.ai_listen_max_probes_per_hour = max(
            0, int(trigger_config.get("ai_listen_max_probes_per_hour", 20))
        )

        self.delegate_undirected_to_ai = bool(
            trigger_config.get("delegate_undirected_to_ai", False)
        )
        self.delegate_undirected_min_signal = max(
            0.0, float(trigger_config.get("delegate_undirected_min_signal", 1.0))
        )

        self.overload_enable = bool(trigger_config.get("overload_enable", True))

        self.overload_min_messages = max(
            1, int(trigger_config.get("overload_min_messages", 20))
        )

        self.overload_min_unique_users = max(
            1, int(trigger_config.get("overload_min_unique_users", 3))
        )

        self.overload_pause = timedelta(
            seconds=max(10, int(trigger_config.get("overload_pause_seconds", 45)))
        )

        self.overload_notice_cooldown = timedelta(
            seconds=max(
                10, int(trigger_config.get("overload_notice_cooldown_seconds", 90))
            )
        )

        self._active_sessions: dict[str, datetime] = {}

        self._recent_group_messages: dict[str, deque[tuple[datetime, str]]] = (
            defaultdict(deque)
        )

        self._last_reply_targets: dict[str, dict[str, dict[str, Any]]] = {}

        self._last_proactive_reply_at: dict[str, datetime] = {}

        self._overload_until: dict[str, datetime] = {}

        self._last_overload_notice_at: dict[str, datetime] = {}

        self._last_ai_probe_at: dict[str, datetime] = {}

        # 每会话的旁听探测时间戳，滑动一小时窗口计配额。
        self._ai_probe_history: dict[str, deque[datetime]] = defaultdict(deque)

        self._followup_lock = asyncio.Lock()

    def _session_key(self, conversation_id: str, user_id: str, is_private: bool) -> str:

        if is_private:

            return conversation_id

        return f"{conversation_id}:{user_id}"

    def activate_session(
        self,
        conversation_id: str,
        user_id: str,
        is_private: bool,
        now: datetime | None = None,
    ) -> None:

        ts = now or datetime.now(timezone.utc)

        self._active_sessions[
            self._session_key(conversation_id, user_id, is_private)
        ] = ts

    def close_session(
        self, conversation_id: str, user_id: str, is_private: bool
    ) -> None:

        self._active_sessions.pop(
            self._session_key(conversation_id, user_id, is_private), None
        )

        targets = self._last_reply_targets.get(conversation_id)

        if isinstance(targets, dict):

            targets.pop(str(user_id), None)

            if not targets:

                self._last_reply_targets.pop(conversation_id, None)

        self._last_proactive_reply_at.pop(conversation_id, None)

        self._overload_until.pop(conversation_id, None)

        self._last_overload_notice_at.pop(conversation_id, None)

        self._last_ai_probe_at.pop(conversation_id, None)

        self._ai_probe_history.pop(conversation_id, None)

    def mark_reply_target(
        self, conversation_id: str, user_id: str, now: datetime | None = None
    ) -> None:

        ts = now or datetime.now(timezone.utc)

        targets = self._last_reply_targets.setdefault(conversation_id, {})

        targets[str(user_id)] = {
            "ts": ts,
            "remaining_turns": self.followup_max_turns,
        }

    def mark_proactive_reply(
        self, conversation_id: str, now: datetime | None = None
    ) -> None:

        self._last_proactive_reply_at[conversation_id] = now or datetime.now(
            timezone.utc
        )

    def evaluate(
        self,
        payload: TriggerInput,
        recent_messages: list[str],
        memory_keywords: list[str] | None = None,
    ) -> TriggerResult:
        keyword_rows = (
            [normalize_text(str(item)) for item in (memory_keywords or []) if normalize_text(str(item))]
            if self.ai_listen_keyword_enable
            else []
        )

        now = payload.timestamp

        self._cleanup(now)

        facts = self._resolve_message_facts(payload)

        session_started_at = self._active_session_started_at(payload, now)

        active_session = session_started_at is not None

        # free window 内才算"正在对话"，超出只作证据。
        active_session_free = active_session and (
            now - session_started_at <= self.active_session_free_window
        )

        followup_candidate = self.peek_followup_candidate(
            payload.conversation_id, payload.user_id, now
        )

        # 别名只在用户自己打的字里认。占位符里的图片 summary（QQ 表情包配文）
        # 带上别名不等于有人在叫机器人。
        name_call = self._contains_alias(facts.user_text)

        busy_messages = 0

        busy_users = 0

        overload_active = False

        if not payload.is_private:

            self._record_group_activity(payload.conversation_id, payload.user_id, now)

            self._update_followup_state(payload.conversation_id, payload.user_id, now)

            busy_messages, busy_users = self._group_busy_stats(payload.conversation_id)

            overload_active = self._refresh_overload(
                payload.conversation_id, now, busy_messages, busy_users
            )

        if overload_active and self._can_send_overload_notice(
            payload.conversation_id, now
        ):

            return TriggerResult(
                should_handle=True,
                reason="overload_notice",
                active_session=active_session,
                followup_candidate=followup_candidate,
                listen_probe=False,
                overload_active=True,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=100,
            )

        if overload_active:

            return TriggerResult(
                should_handle=False,
                reason="overload_pause",
                active_session=active_session,
                followup_candidate=followup_candidate,
                listen_probe=False,
                overload_active=True,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=0,
            )

        if payload.is_private or payload.mentioned:

            return TriggerResult(
                should_handle=True,
                reason="directed",
                active_session=active_session,
                followup_candidate=True,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=90,
            )

        if name_call:

            return TriggerResult(
                should_handle=True,
                reason="name_call",
                active_session=active_session,
                followup_candidate=True,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=85,
            )

        media_only_blocked = facts.is_media_only and self.media_only_requires_directed

        if followup_candidate and (
            not media_only_blocked or self.media_only_allow_in_followup
        ):

            # followup 回合的消费延迟到 engine 路由确认后再执行，
            # 避免 router 低置信度拒绝时白白浪费 followup turn。

            return TriggerResult(
                should_handle=True,
                reason="followup_window",
                active_session=active_session,
                followup_candidate=True,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=70,
            )

        if media_only_blocked:

            # 一个人发一张图不是在跟机器人说话。这里直接沉默，不进旁听探测、
            # 不占探测配额。reason 刻意不用 "ai_router_candidate" ——
            # core/engine.py 会把那个 reason 升回 should_handle=True，等于没省。
            # 媒体本身仍在 trigger 之前就被 engine 记入媒体记忆
            # （core/engine.py `remember_incoming_media`），所以"先发图后问"不受影响。

            _logger.info(
                "trigger_gate_media_only | trace=%s | 会话=%s | 用户=%s | 媒体=%s",
                payload.trace_id or "-",
                payload.conversation_id,
                payload.user_id,
                ",".join(facts.media_types) or "-",
            )

            return TriggerResult(
                should_handle=False,
                reason="media_only_no_text",
                active_session=active_session,
                followup_candidate=followup_candidate,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=0,
            )

        if active_session_free:

            return TriggerResult(
                should_handle=True,
                reason="active_session",
                active_session=True,
                followup_candidate=False,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=60,
            )

        # 旁听探测放在 followup / 裸媒体 / active_session 之后才决定，
        # 冷却与配额也只在真的探测时记账。放在开头无条件执行时，
        # 那些分支会白吃掉冷却窗口，把真正的插话机会饿死。

        listen_probe_reason = ""

        if not payload.is_private:

            listen_probe_reason = self._decide_ai_probe_reason(
                payload,
                now,
                busy_messages,
                busy_users,
                recent_messages=recent_messages,
                memory_keywords=keyword_rows,
                user_text=facts.user_text,
                active_session_expired=active_session and not active_session_free,
            )

        if listen_probe_reason:

            return TriggerResult(
                should_handle=True,
                reason=listen_probe_reason,
                active_session=active_session,
                followup_candidate=False,
                listen_probe=True,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=20,
            )

        # 结构定位符只看用户自己给的内容：占位符里的媒体 URL / 本地路径
        # 是我们自己从 raw_segments 拼进去的，不是用户贴的链接。

        delegate_signal = self._structural_request_signal(facts.user_text)

        if (
            self.delegate_undirected_to_ai
            and delegate_signal >= self.delegate_undirected_min_signal
        ):

            return TriggerResult(
                # 仅作为候选进入 AI 评估，不直接放行回复。
                should_handle=False,
                reason="ai_router_candidate",
                active_session=active_session,
                followup_candidate=False,
                listen_probe=False,
                overload_active=False,
                busy_messages=busy_messages,
                busy_users=busy_users,
                ai_gate=True,
                priority=10,
            )

        return TriggerResult(
            should_handle=False,
            reason="not_directed",
            active_session=active_session,
            followup_candidate=False,
            listen_probe=False,
            overload_active=False,
            busy_messages=busy_messages,
            busy_users=busy_users,
            ai_gate=True,
            priority=0,
        )

    def _contains_alias(self, text: str) -> bool:

        content = normalize_text(text).lower()

        if not content:

            return False

        # 对单字符中文别名做严格匹配：

        # - 必须是独立出现（不能是 "下雪"、"雪花" 等词的一部分）

        # - 允许: "雪 你好"、"雪，帮我"、句首/句尾的 "雪"

        # 对多字符别名保持原有宽松匹配

        for alias, pattern in self.alias_patterns:
            if not alias:
                continue
            if pattern is not None:
                if pattern.search(content):
                    return True
                continue
            if alias in content:
                return True

        compacted = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", content)

        if compacted:

            for alias in self.bot_aliases:

                if not alias:

                    continue

                # 单字符中文别名不走 compacted 匹配（去掉标点后 "下雪" 仍然包含 "雪"）

                if len(alias) == 1 and "\u4e00" <= alias <= "\u9fff":

                    continue

                if alias in compacted:

                    return True

        for alias in self.bot_aliases:

            if not alias:

                continue

            if len(alias) == 1 and "\u4e00" <= alias <= "\u9fff":

                continue

            if re.fullmatch(r"[a-z0-9_]+", alias):

                pattern = rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])"

            else:

                pattern = re.escape(alias)

            if re.search(pattern, content):

                return True

        return False

    @staticmethod
    def _split_multimodal_marker(text: str) -> tuple[list[str], str]:
        """拆开 app_helpers `_build_multimodal_text()` 生成的占位符。

        返回（媒体段类型列表, 用户自己输入的文本）。纯结构解析，零词表：
        只认 `MULTIMODAL_EVENT[_AT] <前缀>: tok | tok` 这个机器格式。
        没有这个前缀时原样返回，媒体列表为空。
        """

        content = normalize_text(text)

        if not content:

            return [], ""

        match = _MULTIMODAL_MARKER_PATTERN.match(content)

        if not match:

            return [], content

        remainder = content[match.end() :]

        media_types: list[str] = []

        while remainder:

            remainder = remainder.lstrip()

            head, separator, tail = remainder.partition(_MEDIA_TOKEN_SEPARATOR)

            chunk = (head if separator else remainder).strip()

            first_token, _, text_after_token = chunk.partition(" ")

            seg_type, has_value, _ = first_token.partition(":")

            seg_type = seg_type.strip().lower()

            if seg_type not in _MEDIA_SEGMENT_TYPES:

                # 不是媒体 token 了，剩下的全算用户文本。

                break

            media_types.append(seg_type)

            if separator:

                remainder = tail

                continue

            if not has_value:

                # `forward` 这类无值 token，后面跟的就是用户输入。

                remainder = text_after_token

                continue

            # 最后一个 token 的值可能含空格（图片的自由文本 summary），
            # 所以按整段判断：闭合形态（`[image]` / URL / 路径）之后是用户输入，
            # 自由文本 summary 则整段都算媒体附带信息。

            _, _, full_value = chunk.partition(":")

            remainder = TriggerEngine._trailing_user_text(full_value)

        return media_types, normalize_text(remainder)

    @staticmethod
    def _trailing_user_text(value: str) -> str:
        """从最后一个媒体 token 的值里切出用户自己打的字。"""

        content = value.strip()

        if not content:

            return ""

        match = _CLOSED_MEDIA_VALUE_PATTERN.match(content)

        if not match:

            # 自由文本 summary 没有右边界，整段都算媒体附带信息。

            return ""

        return content[match.end() :].strip()

    def _resolve_message_facts(self, payload: TriggerInput) -> _MessageFacts:
        """确定本条消息的结构事实。engine 显式声明优先，缺省时回落占位符解析。"""

        parsed_types, parsed_text = self._split_multimodal_marker(payload.text)

        declared_types = [
            normalize_text(str(item)).lower()
            for item in (payload.media_types or [])
            if normalize_text(str(item))
        ]

        # 优先级：engine 显式给的 user_text > 占位符解析 > 空。
        # engine 那份是用 _extract_multimodal_user_text() 算的，比在这里按空格切准 ——
        # 图片 summary 没有右边界（`image:哎呦，你干嘛～`），解析必然误伤。
        if payload.user_text is not None:
            user_text = normalize_text(payload.user_text)
        elif payload.has_user_text is False:
            # engine 明确说用户没打字，那解析出来的东西一定是占位符残渣，丢掉。
            user_text = ""
        else:
            user_text = parsed_text

        # has_user_text 是"有没有"的事实。engine 显式声明优先；
        # 声明了有、却没给内容时只开门不造内容 —— 宁可让这轮进模型，
        # 也不要把机器拼的占位符当成用户说的话去匹配别名。
        if payload.has_user_text is not None:
            has_user_text = payload.has_user_text
        else:
            has_user_text = bool(user_text)

        media_types = declared_types or parsed_types

        return _MessageFacts(
            media_types=media_types,
            user_text=user_text,
            is_media_only=bool(media_types) and not has_user_text,
        )

    @classmethod
    def _strip_context_row_prefix(cls, row: str) -> str:
        """剥掉群上下文行里的机器生成部分，只留群友真正说的话。

        `昵称(QQ:12345): ` 是 core/engine.py `_record_runtime_group_chat` 拼的，
        后面还可能跟着多模态占位符。这两段都会被"近 48 行出现两次的 token
        自动升级成热词"那条规则变成触发词 —— 于是一堆裸图把 image / multimodal /
        sent / user 变成"大家在聊的话题"，说过两次话的人的昵称和 token qq 也一样。
        """

        content = normalize_text(row)

        if not content:

            return ""

        content = _CONTEXT_ROW_PREFIX_PATTERN.sub("", content, count=1)

        _, user_text = cls._split_multimodal_marker(content)

        return user_text

    def _active_session_started_at(
        self, payload: TriggerInput, now: datetime
    ) -> datetime | None:
        """返回该 (会话,用户) 的活跃时间戳，超时则 None。"""

        key = self._session_key(
            payload.conversation_id, payload.user_id, payload.is_private
        )

        ts = self._active_sessions.get(key)

        if not isinstance(ts, datetime):

            return None

        if now - ts > self.session_timeout:

            return None

        return ts

    def _probe_budget_ok(self, conversation_id: str, now: datetime) -> bool:
        """滑动一小时窗口内还有旁听探测配额吗。"""

        if self.ai_listen_max_probes_per_hour <= 0:

            return False

        history = self._ai_probe_history[conversation_id]

        while history and now - history[0] > _PROBE_BUDGET_WINDOW:

            history.popleft()

        return len(history) < self.ai_listen_max_probes_per_hour

    def _commit_probe(self, conversation_id: str, now: datetime) -> None:
        """真的探测了才记账 —— 冷却与配额在同一处提交。"""

        self._last_ai_probe_at[conversation_id] = now

        self._ai_probe_history[conversation_id].append(now)

        _logger.info(
            "trigger_probe_budget | 会话=%s | used=%d/%d",
            conversation_id,
            len(self._ai_probe_history[conversation_id]),
            self.ai_listen_max_probes_per_hour,
        )

    def _record_group_activity(
        self, conversation_id: str, user_id: str, now: datetime
    ) -> None:

        queue = self._recent_group_messages[conversation_id]

        queue.append((now, user_id))

        while queue and now - queue[0][0] > self.busy_window:

            queue.popleft()

    def _group_busy_stats(self, conversation_id: str) -> tuple[int, int]:

        queue = self._recent_group_messages.get(conversation_id, deque())

        message_count = len(queue)

        unique_users = len({item[1] for item in queue})

        return message_count, unique_users

    def _refresh_overload(
        self, conversation_id: str, now: datetime, message_count: int, unique_users: int
    ) -> bool:

        until = self._overload_until.get(conversation_id)

        if isinstance(until, datetime) and now < until:

            return True

        if isinstance(until, datetime) and now >= until:

            self._overload_until.pop(conversation_id, None)

        if not self.overload_enable:

            return False

        if (
            message_count >= self.overload_min_messages
            and unique_users >= self.overload_min_unique_users
        ):

            self._overload_until[conversation_id] = now + self.overload_pause

            return True

        return False

    def _can_send_overload_notice(self, conversation_id: str, now: datetime) -> bool:

        last = self._last_overload_notice_at.get(conversation_id)

        if isinstance(last, datetime) and now - last < self.overload_notice_cooldown:

            return False

        self._last_overload_notice_at[conversation_id] = now

        return True

    def _decide_ai_probe_reason(
        self,
        payload: TriggerInput,
        now: datetime,
        busy_messages: int,
        busy_users: int,
        *,
        recent_messages: list[str] | None = None,
        memory_keywords: list[str] | None = None,
        user_text: str | None = None,
        active_session_expired: bool = False,
    ) -> str:

        if not self.ai_listen_enable:

            return ""

        reason = self._decide_ai_probe_reason_by_stats(
            conversation_id=payload.conversation_id,
            now=now,
            busy_messages=busy_messages,
            busy_users=busy_users,
            text=payload.text if user_text is None else user_text,
            recent_messages=recent_messages or [],
            memory_keywords=memory_keywords or [],
            active_session_expired=active_session_expired,
        )

        return reason

    def _decide_ai_probe_reason_by_stats(
        self,
        conversation_id: str,
        now: datetime,
        busy_messages: int,
        busy_users: int,
        text: str = "",
        recent_messages: list[str] | None = None,
        memory_keywords: list[str] | None = None,
        active_session_expired: bool = False,
    ) -> str:

        if not self.ai_listen_enable:

            return ""

        last = self._last_ai_probe_at.get(conversation_id)

        if isinstance(last, datetime) and now - last < self.ai_listen_interval:

            return ""

        if not self._probe_budget_ok(conversation_id, now):

            return ""

        clean_text = normalize_text(text).lower()
        keyword_hits = self._match_memory_keywords(
            clean_text=clean_text,
            recent_messages=recent_messages or [],
            memory_keywords=memory_keywords or [],
        )

        # 群里几乎没人说话时，不走"监听探测"，直接交给正常路由链路处理。

        if busy_users <= 1 and busy_messages <= max(
            2, self.ai_listen_min_messages // 2
        ) and keyword_hits < self.ai_listen_min_keyword_hits:

            return ""

        # 显式输入命令令牌（/xxx、!xxx）时，不走"监听探测"分支，避免被低置信拦截。

        if self._has_typed_command_token(clean_text):

            return ""

        structural_signal = self._structural_request_signal(clean_text)

        heat_ok = (
            busy_messages >= self.ai_listen_min_messages
            and busy_users >= self.ai_listen_min_unique_users
        )

        score = self._build_listen_score(
            clean_text,
            busy_messages,
            busy_users,
            structural_signal=structural_signal,
            keyword_hits=keyword_hits,
            active_session_expired=active_session_expired,
        )

        if (
            self.ai_listen_keyword_pass_enable
            and keyword_hits >= self.ai_listen_min_keyword_hits
        ):

            # 旧行为：一个词形命中就开口，绕过 ai_listen_min_score。
            # 默认关闭，留开关是为了能一键回滚，不是为了继续用。

            self._commit_probe(conversation_id, now)

            return "ai_listen_probe_memory_keyword"

        if not heat_ok and score < self.ai_listen_min_score:

            return ""

        self._commit_probe(conversation_id, now)

        if structural_signal >= _STRUCTURAL_PROBE_THRESHOLD:

            return "ai_listen_probe_structural"

        if heat_ok:

            return "ai_listen_probe_heat"

        return "ai_listen_probe_score"

    @staticmethod
    def _has_typed_command_token(text: str) -> bool:
        """是否显式输入了命令令牌（/xxx、!xxx）。这是输入形式，不是语义猜测。"""

        return bool(_TYPED_COMMAND_PATTERN.search(normalize_text(text)))

    def _structural_request_signal(self, text: str) -> float:
        """按消息里客观存在的结构定位符打分，供注意力门使用。

        只读四类结构事实：显式命令令牌、URL、视频号、文件扩展名。
        不做任何"这句话像是在提需求"的语义判断 —— 意图由模型读 PromptNavigator
        菜单后自己选分区，trigger 不参与。
        """

        clean = normalize_text(text).lower()

        if not clean:

            return 0.0

        score = 0.0

        if _TYPED_COMMAND_PATTERN.search(clean):

            score += _TYPED_COMMAND_SCORE

        for pattern in (_URL_PATTERN, _VIDEO_ID_PATTERN, _FILE_EXTENSION_PATTERN):

            if pattern.search(clean):

                score += _STRUCTURAL_LOCATOR_SCORE

        return min(score, _STRUCTURAL_SIGNAL_CAP)

    def _build_listen_score(
        self,
        text: str,
        busy_messages: int,
        busy_users: int,
        *,
        structural_signal: float = 0.0,
        keyword_hits: int = 0,
        active_session_expired: bool = False,
    ) -> float:
        msg_ratio = busy_messages / max(1, self.ai_listen_min_messages)
        user_ratio = busy_users / max(1, self.ai_listen_min_unique_users)
        score = msg_ratio * 0.9 + user_ratio * 0.9

        # \u6807\u70b9\u4e0e\u547d\u4ee4\u4ee4\u724c\u662f\u8f93\u5165\u5f62\u5f0f\u4e0a\u7684\u9632\u566a\u4fe1\u53f7\uff0c\u4e0d\u53c2\u4e0e\u4efb\u52a1/\u5de5\u5177\u9009\u62e9\u3002
        if _QUESTION_MARKS.intersection(text) or _TYPED_COMMAND_PATTERN.search(text):
            score += 0.5
        score += min(1.6, structural_signal * 0.9)
        score += min(1.2, max(0, int(keyword_hits)) * 0.4)
        if active_session_expired:
            # \u8fc7\u671f active_session \u4e0d\u653e\u884c\uff0c\u4f46"\u8fd9\u4eba\u4e0d\u4e45\u524d\u521a\u8ddf\u6211\u8bf4\u8fc7\u8bdd"\u4ecd\u662f\u8bc1\u636e\u3002
            score += self.active_session_score_bonus
        return score

    @staticmethod
    def _is_keyword_token(token: str) -> bool:
        word = normalize_text(str(token)).lower()
        if not word:
            return False
        if re.fullmatch(r"\d+", word):
            return False
        if len(word) < 2:
            return False
        return True

    def _match_memory_keywords(
        self,
        *,
        clean_text: str,
        recent_messages: list[str],
        memory_keywords: list[str],
    ) -> int:

        if not self.ai_listen_keyword_enable:
            return 0

        user_tokens = {item for item in tokenize(clean_text) if self._is_keyword_token(item)}

        keyword_pool: set[str] = set()

        for raw in memory_keywords:
            word = normalize_text(str(raw)).lower()
            if self._is_keyword_token(word):
                keyword_pool.add(word)
            for token in tokenize(word):
                if self._is_keyword_token(token):
                    keyword_pool.add(token)

        for raw in self.ai_listen_keywords:
            word = normalize_text(str(raw)).lower()
            if self._is_keyword_token(word):
                keyword_pool.add(word)
            for token in tokenize(word):
                if self._is_keyword_token(token):
                    keyword_pool.add(token)

        recent_counter: Counter[str] = Counter()
        for raw in recent_messages[-48:]:
            # 先剥掉 `昵称(QQ:12345): ` 前缀与多模态占位符：那些是机器生成的，
            # 让它们参与热词统计等于让机器人自己的输出把自己叫醒。
            line = self._strip_context_row_prefix(str(raw)).lower()
            if not line:
                continue
            for token in tokenize(line):
                if self._is_keyword_token(token):
                    recent_counter[token] += 1

        for token, count in recent_counter.items():
            if count >= 2:
                keyword_pool.add(token)

        if not keyword_pool:
            return 0

        hits = 0
        for keyword in keyword_pool:
            if re.fullmatch(r"[a-z0-9_]{2,}", keyword):
                if keyword in user_tokens:
                    hits += 1
            elif keyword in clean_text or keyword in user_tokens:
                hits += 1

            if hits >= max(1, self.ai_listen_min_keyword_hits):
                break

        return hits

    def peek_followup_candidate(
        self, conversation_id: str, user_id: str, now: datetime
    ) -> bool:

        targets = self._last_reply_targets.get(conversation_id)

        if not isinstance(targets, dict):

            return False

        uid = str(user_id)

        state = targets.get(uid)

        if not isinstance(state, dict):

            return False

        ts = state.get("ts")

        if not isinstance(ts, datetime) or now - ts > self.followup_reply_window:

            targets.pop(uid, None)

            if not targets:

                self._last_reply_targets.pop(conversation_id, None)

            return False

        remaining = int(state.get("remaining_turns", 0))

        if remaining <= 0:

            targets.pop(uid, None)

            if not targets:

                self._last_reply_targets.pop(conversation_id, None)

            return False

        return True

    async def consume_followup_turn(
        self, conversation_id: str, user_id: str, now: datetime | None = None
    ) -> None:
        """在消息成功发出后消费一次 followup 回合。"""
        async with self._followup_lock:
            ts = now or datetime.now(timezone.utc)

            targets = self._last_reply_targets.get(conversation_id)

            if not isinstance(targets, dict):

                return

            uid = str(user_id)

            state = targets.get(uid)

            if not isinstance(state, dict):

                return

            last_ts = state.get("ts")

            if (
                not isinstance(last_ts, datetime)
                or ts - last_ts > self.followup_reply_window
            ):

                targets.pop(uid, None)

                if not targets:

                    self._last_reply_targets.pop(conversation_id, None)

                return

            remaining = int(state.get("remaining_turns", 0))

            if remaining <= 0:

                targets.pop(uid, None)

                if not targets:

                    self._last_reply_targets.pop(conversation_id, None)

                return

            state["remaining_turns"] = remaining - 1

            state["ts"] = ts

            if int(state.get("remaining_turns", 0)) <= 0:

                targets.pop(uid, None)

            else:

                targets[uid] = state

            if not targets:

                self._last_reply_targets.pop(conversation_id, None)

    def _update_followup_state(
        self, conversation_id: str, user_id: str, now: datetime
    ) -> None:

        _ = user_id

        targets = self._last_reply_targets.get(conversation_id)

        if not isinstance(targets, dict):

            return

        expired: list[str] = []

        for uid, state in targets.items():

            ts = state.get("ts") if isinstance(state, dict) else None

            if not isinstance(ts, datetime) or now - ts > self.followup_reply_window:

                expired.append(uid)

        for uid in expired:

            targets.pop(uid, None)

        if not targets:

            self._last_reply_targets.pop(conversation_id, None)

    def _cleanup(self, now: datetime) -> None:

        expired_sessions = [
            key
            for key, ts in self._active_sessions.items()
            if not isinstance(ts, datetime) or now - ts > self.session_timeout
        ]

        for key in expired_sessions:

            self._active_sessions.pop(key, None)

        for cid, targets in list(self._last_reply_targets.items()):

            if not isinstance(targets, dict):

                self._last_reply_targets.pop(cid, None)

                continue

            expired_users: list[str] = []

            for uid, state in targets.items():

                ts = state.get("ts") if isinstance(state, dict) else None

                if (
                    not isinstance(ts, datetime)
                    or now - ts > self.followup_reply_window
                ):

                    expired_users.append(uid)

            for uid in expired_users:

                targets.pop(uid, None)

            if not targets:

                self._last_reply_targets.pop(cid, None)

        expired_overload = [
            cid
            for cid, until in self._overload_until.items()
            if not isinstance(until, datetime) or now >= until
        ]

        for cid in expired_overload:

            self._overload_until.pop(cid, None)

        for cid, queue in list(self._recent_group_messages.items()):

            while queue and now - queue[0][0] > self.busy_window:

                queue.popleft()

            if not queue:

                self._recent_group_messages.pop(cid, None)

        for cid, history in list(self._ai_probe_history.items()):

            while history and now - history[0] > _PROBE_BUDGET_WINDOW:

                history.popleft()

            if not history:

                self._ai_probe_history.pop(cid, None)
