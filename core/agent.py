"""Agent 循环核心 — 多步推理 + 工具调用。

Agent 接收用户消息后，进入 think → act → observe 循环：
1. LLM 分析当前状态，决定调用哪个工具（或直接回复）
2. 执行工具，获取结果
3. 把结果喂回 LLM，继续循环
4. 当 LLM 调用 final_answer 时，循环结束
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime
import json
import logging
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from core.agent_checkpoint import AgentTurnCheckpoint
from core.agent_tools import AgentToolRegistry, ToolCallResult
from core import prompt_loader as _pl
from core.prompt_navigator import NAVIGATE_SECTION_TOOL, NavigatorState, PromptNavigator
from core.prompt_policy import PromptPolicy
from core.loop_guard import LoopGuard, ToolCallRecord, hash_call, hash_result
from core import media_utils
from core.tool_call_repair import repair_tool_call
from services.model_client import ModelClient
from utils.intent import (
    looks_like_qq_profile_analysis_request as _shared_qq_profile_request,
)
from utils.text import clip_text, normalize_text

_log = logging.getLogger("yukiko.agent")

# ---------------------------------------------------------------------------
# Pre-compiled regex patterns (constant patterns used across the module)
# ---------------------------------------------------------------------------
_RE_WHITESPACE = re.compile(r"\s+")
_RE_WHITESPACE_2PLUS = re.compile(r"\s{2,}")
_RE_URL_EXTRACT = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_RE_URL_STRIP = re.compile(r"https?://\S+", re.IGNORECASE)
_RE_URL_SCHEME = re.compile(r"https?://")
_RE_BARE_WEB_HOST = re.compile(
    r"(?<![@A-Za-z0-9_.-])"
    r"((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|net|org|dev|io|ai|app|site|xyz|me|co|cn|jp|tv|gg|cc|info|wiki|top)"
    r"(?::\d{2,5})?(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?)",
    re.IGNORECASE,
)
_RE_QQ_NUMBER = re.compile(r"(?<!\d)([1-9]\d{5,11})(?!\d)")
# 弱模型防护用的宽范围 CJK 标点匹配（见下方 L59）
# 注意: 旧的窄范围定义已移除，统一使用下方宽范围版本
_RE_SLASH_IMAGE = re.compile(r"(?:^|\s)/image(?:\s|$)")
_RE_SLASH_VIDEO = re.compile(r"(?:^|\s)/(?:video|vid)(?:\s|$)")
_RE_DOWNLOAD_EXT = re.compile(r"\.(apk|exe|msi|zip|7z|rar|ipa|dmg)(?:\?|#|$)")
_RE_THINKING_TAG = re.compile(r"</?thinking>", re.IGNORECASE)
_RE_THINKING_BLOCK = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)
_RE_TOOL_CALL_TAG = re.compile(r"</?tool_call>", re.IGNORECASE)
_RE_TOOL_USE_TAG = re.compile(r"</?tool_use>", re.IGNORECASE)
_RE_CODE_BLOCK = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)
_RE_FINAL_ANSWER_KEY = re.compile(r'"(?:tool|name)"\s*:\s*"final_answer"')
_RE_TEXT_KEY = re.compile(r'"text"\s*:\s*"')
_RE_ASCII_LETTER = re.compile(r"[A-Za-z]")
_RE_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")
# \u673a\u5668\u6807\u8bc6\u7b26\uff1a\u5e26\u4e0b\u5212\u7ebf\u7684 ASCII \u6807\u8bb0\uff08\u5de5\u5177\u540d analyze_image\u3001\u9519\u8bef\u7801
# qzone_api_error\u3001tool_timeout:parse_video \u8fd9\u7c7b\uff09\u3002\u5224\u5b9a\u6761\u4ef6\u662f"\u542b\u4e0b\u5212\u7ebf"\u8fd9\u4e2a
# \u7ed3\u6784\u7279\u5f81\uff0c\u4e0d\u662f\u8bcd\u8868 \u2014\u2014 \u4e2d\u6587\u6563\u6587\u548c\u666e\u901a\u82f1\u6587\u5355\u8bcd\u90fd\u4e0d\u542b\u4e0b\u5212\u7ebf\uff0cURL \u5355\u72ec\u6392\u9664\u3002
_RE_MACHINE_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?::[A-Za-z0-9_.-]+)?")
# retcode=-5503022 / status=503 \u8fd9\u7c7b key=value \u673a\u5668\u72b6\u6001\u3002
_RE_MACHINE_KEY_VALUE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*\s*=\s*-?[A-Za-z0-9_.:-]+")
# 剥掉机器标识符后至少还要剩这么多汉字，才算得上一句能发给用户的话。
_MIN_CJK_FOR_USER_FACING_FAILURE = 6

# 内部编排步：不是模型对外做的事，只是循环自己记的账。
# `policy_guard` 是本地策略拦截，`think` 是模型自言自语，`navigate_section` 是换分区。
# 判「本回合有没有真的工具失败」时必须排除它们，否则 policy_guard 每次拦一下
# 就会被算成一次失败，空 final_answer 永远走不到「模型主动沉默」那条路。
_INTERNAL_STEP_TOOLS = frozenset({"policy_guard", "think", NAVIGATE_SECTION_TOOL})
# 弱模型防护: 匹配所有标点/符号/空白 (用于检测纯标点垃圾)
_RE_PUNCTUATION_CJK = re.compile(
    r"[\s\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
    r"\u2000-\u206f\u2e00-\u2e7f!-/:-@\[-`{-~。，、；：？！…—·''""〈〉《》「」『』【】〔〕〖〗]+"
)
_RE_LOCAL_FILE_REF = re.compile(
    r"(?i)(?:file://)?(?:"
    r"[A-Z]:[\\/][^\s`'\"，。；、]+"
    r"|/(?:Users|home|tmp|private|var|mnt|Volumes)/[^\s`'\"，。；、]+"
    r")"
)
_RE_SYNTHETIC_USER_PREFIX = re.compile(r"^\s*用户\d{2,12}\s*[，,、:：]\s*")

# 布尔型必填参数：模型填 False 是一次明确声明，不能当成「参数缺失」。
# 只对这些字段放行 False，其它必填参数（query/url/…）填 False 仍然算缺。
_DECLARED_FLAG_ARGS = frozenset({"upload"})

# 必填参数缺失时回喂给模型的补充说明。通用文案只说「缺 X」，
# 对这种「必须自己表态」的参数不够，模型需要知道两个取值各代表什么动作。
_MISSING_ARG_HINTS: dict[str, str] = {
    "upload": (
        "upload 必填：用户要你把文件发到聊天里就填 true，"
        "只是让你取内容/校验/看信息就填 false。不要因为消息里出现「发」字就填 true。"
    ),
}


@dataclass(slots=True)
class AgentContext:
    """Agent 单次运行的上下文。"""

    conversation_id: str
    user_id: str
    user_name: str
    group_id: int
    bot_id: str
    is_private: bool
    mentioned: bool
    message_text: str
    original_message_text: str = ""
    explicit_bot_addressed: bool = False
    # 本回合是不是「有人在跟机器人说话」。纯结构事实，engine 侧应显式传
    # `explicit_bot_addressed or trigger.followup_candidate`。
    # None = engine 没告诉我们 → `AgentLoop._is_directed_turn()` 用
    # is_private / mentioned / explicit_bot_addressed / 引用了机器人的消息
    # 这四个已有结构事实推断。
    #
    # 为什么需要它：旁听探测轮（群友互相聊天）里模型判定不该插话，紧接着
    # LLM 超时，兜底就把「这次没拿到有效结果，你补充一点信息我再来」发进群 ——
    # 群友根本没跟机器人说话。非指向轮的兜底一律不外发。
    was_directed: bool | None = None
    message_id: str = ""
    reply_to_message_id: str = ""
    raw_segments: list[dict[str, Any]] = field(default_factory=list)
    reply_media_segments: list[dict[str, Any]] = field(default_factory=list)
    reply_to_user_id: str = ""
    reply_to_user_name: str = ""
    reply_to_text: str = ""
    api_call: Any = None
    admin_handler: Any = None  # async fn(text, user_id, group_id) -> str|None
    config_patch_handler: Any = (
        None  # async fn(patch, actor_user_id, reason, dry_run) -> tuple[bool, str, dict]
    )
    sticker_manager: Any = None  # StickerManager instance
    tool_executor: Any = None  # ToolExecutor instance (for video parsing etc.)
    crawler_hub: Any = None  # CrawlerHub instance
    knowledge_base: Any = None  # KnowledgeBase instance
    memory_engine: Any = None  # MemoryEngine instance（兼容 engine 注入）
    stream_callback: Any = None  # WebUI 思考流回调
    # 话题门（SafetyEngine._is_political_topic）。用于把**外部抓取回来的内容**
    # 里的时政条目丢掉，而不是等模型转述完再靠词替换兜。
    # 实测 2026-08-06：知乎热榜返回「如何看待国家这一次的扫黑除恶专项行动？」，
    # 而 get_hot_trends 零过滤，且会把标题写进知识库持久化。
    # 输入门只看用户消息，filter_output 只能换词表内的词，这一层都覆盖不到。
    topic_gate: Any = None  # fn(str) -> bool，True = 该丢弃
    # 出站文本敏感词过滤（SafetyEngine.filter_output）。
    # 必须注入：final_answer 走 engine 的 _try_agent_path 后处理时会过一遍
    # filter_output，但 send_group_message 这类工具是**直接调 NapCat API**，
    # 完全绕开那段后处理。漏了这个注入，工具直发的文本就是零过滤出群。
    output_filter: Any = None  # fn(str) -> str
    trace_id: str = ""
    memory_context: list[str] = field(default_factory=list)
    related_memories: list[str] = field(default_factory=list)
    native_tools: list[str] = field(default_factory=list)
    navigator_state: NavigatorState | None = None
    navigator_pending_tool_retry: tuple[str, dict[str, Any]] | None = None
    user_profile_summary: str = ""
    preferred_name: str = ""
    recent_speakers: list[tuple[str, str, str]] = field(default_factory=list)
    compat_context: str = ""
    user_policies: dict[str, Any] = field(default_factory=dict)
    user_directives: list[str] = field(default_factory=list)
    thread_state: dict[str, Any] = field(default_factory=dict)
    runtime_group_context: list[str] = field(default_factory=list)
    runtime_admin_policy: dict[str, Any] = field(default_factory=dict)
    media_summary: list[str] = field(default_factory=list)
    reply_media_summary: list[str] = field(default_factory=list)
    recent_media_artifact: dict[str, Any] = field(default_factory=dict)
    at_other_user_ids: list[str] = field(default_factory=list)
    at_other_user_names: dict[str, str] = field(default_factory=dict)  # {qq_id: name}
    verbosity: str = "medium"  # verbose / medium / brief / minimal
    output_style_instruction: str = ""  # 额外输出风格指令（可按群覆盖）
    sender_role: str = ""  # "owner" / "admin" / "member" — QQ群内角色
    event_payload: dict[str, Any] = field(
        default_factory=dict
    )  # 原始 OneBot/NapCat 事件快照
    is_whitelisted_group: bool = False  # 当前群是否在白名单中
    bot_mood: str = ""  # 当前 bot 心情状态（happy/neutral/tired/...）
    affinity_hint: str = ""  # 用户好感度提示
    mood_hint: str = ""  # bot 心情提示


@dataclass(slots=True)
class AgentResult:
    """Agent 循环的最终输出。"""

    reply_text: str = ""
    image_url: str = ""
    image_urls: list[str] = field(default_factory=list)
    video_url: str = ""
    audio_file: str = ""
    cover_url: str = ""
    action: str = "reply"
    reason: str = ""
    tool_calls_made: int = 0
    total_time_ms: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    """核心 Agent 循环引擎。

    流程:
    1. 构建 system prompt（含工具列表）
    2. 发送用户消息给 LLM
    3. LLM 返回 tool_call JSON → 执行工具 → 结果追加到对话
    4. 重复直到 LLM 调用 final_answer 或达到 max_steps
    """

    # 畸形 JSON 参数「无损恢复」的累计次数，以及汇总日志的间隔。
    # 这条路径实测占 skiapi 工具调用的 89%，逐条 WARNING 会淹掉日志，
    # 所以按次数汇总。有损恢复走独立的 WARNING，不受这里影响。
    _malformed_args_recovered_total = 0
    _MALFORMED_ARGS_LOG_EVERY = 25

    # 有副作用的发送工具（避免 final_answer 重复发送）
    _SIDE_EFFECT_SEND_TOOLS = frozenset(
        {
            "send_group_message",
            "send_private_message",
            "send_emoji",
            "send_sticker",
            "learn_sticker",
            "upload_group_file",
            "upload_private_file",
            "send_group_ai_record",
            "send_group_forward_msg",
            "send_private_forward_msg",
        }
    )
    # 这些工具完成后应直接 final_answer，不再调用其他工具
    _TERMINAL_TOOLS = frozenset(
        {
            "learn_sticker",
            "correct_sticker",
        }
    )
    # 同一回合内 (工具, 参数) 完全相同只允许真正执行一次的工具。
    # 这些工具对外有副作用且幂等语义明确，重复执行会被用户直接看见：
    # 实测 send_face 连发 3 个相同表情进群、remember_user_fact 把同一条事实
    # 写了 4 次。max_same_tool_call（默认 3）对它们太宽。
    _ONCE_PER_TURN_TOOLS = _SIDE_EFFECT_SEND_TOOLS | {
        "send_face",
        "set_msg_emoji_like",
        "remember_user_fact",
        "correct_sticker",
    }
    _EXTERNAL_FACT_TOOLS = frozenset(
        {
            "web_search",
            "fetch_webpage",
            "github_search",
            "github_readme",
            "search_media",
            "search_web_media",
            "search_download_resources",
            "douyin_search",
            "scrape_extract",
            "scrape_structured",
            "scrape_follow_links",
        }
    )
    _FALLBACK_RAW_DISPLAY_SKIP_TOOLS = frozenset(
        {
            "scrape_extract",
            "scrape_summarize",
            "scrape_structured",
            "scrape_follow_links",
            "fetch_webpage",
            # 发现/清单类工具：它们的 display 是给模型看的能力目录，永远不是给用户的
            # 答案。实测「画一只戴宇航员头盔的柴犬」→ generate_image_enhanced 失败、
            # 模型接着调 list_image_models 诊断，兜底却把后者的 "可用模型: 1 个"
            # 当成回复发给了用户 —— 用户既没拿到图，也不知道生成失败了。
            "list_image_models",
            "list_faces",
            "list_emojis",
            "browse_sticker_categories",
            # 内容搬运类工具：display 里是**别人说过的原话**，是给模型读的素材，
            # 不是给用户的答案。兜底把它原样发出去等于把那些话在群里又播一遍 ——
            # 实测事故形状：查精华消息时把「@某人 你妈也死了」完整复述进群并加调侃。
            # 用户确实主动要求了，但完整复述等于又发一遍。
            # 这条路零 LLM、零人格底稿参与，所以 prompt 层的禁令碰不到它
            # （core/agent_tools_napcat.py 各 handler 的 display 构造见括号内行号）。
            "get_essence_msg_list",  # :2482 "共 N 条精华:\n[昵称] 内容"
            "get_group_history",  # :1896
            "get_chat_history",  # :1921
            "get_group_msg_history",  # :2431
            "get_forward_msg",  # :4008
            # 名单/资料类：display 是成员名册与个人资料，同样不是答案，
            # 而且直通等于把一批 QQ 号和昵称倒进群里。
            "get_group_member_list",  # :1153
            "get_friend_list",  # :1778
            "get_group_list",  # :1787
            "get_user_info",  # :1175
            "get_login_info",  # :1846
        }
    )
    _DOWNLOAD_LLM_EXTRACT_TOOLS = frozenset(
        {
            "scrape_extract",
            "scrape_summarize",
            "scrape_structured",
            "scrape_follow_links",
        }
    )
    # 失败类别标记 —— 只匹配 ToolCallResult.error 这个字段。
    # 那里面的值全部由本仓代码自己写死（tool_timeout:xxx / permission_denied:yyy /
    # memory_engine_unavailable / missing_required_args:...），是机器码而不是自然
    # 语言，所以按前后缀取类别属于"结构事实"，不是对句子做语义判断。
    # display（中文散文）绝对不参与分类。
    _TOOL_FAILURE_CATEGORY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("timeout", ("timeout", "timed_out")),
        ("permission", ("permission_denied", "need_super_admin", "need_group_admin")),
        ("missing_args", ("missing_required_args", "missing_", "empty_", "invalid_", "required")),
        (
            "blocked",
            ("blocked", "unsafe", "nsfw", "guard", "disabled", "not_supported", "unsupported"),
        ),
        ("unavailable", ("unavailable", "missing_dependency", "not_installed", "no_api_call")),
        ("not_found", ("not_found", "no_results", "no_result", "empty_result")),
        ("upstream", ("_error", "_failed", "failed", "exception", "crash")),
    )
    # 类别 → 喂给兜底 LLM 的短标签。这是枚举映射而不是可调文案，故留在代码里；
    # 给用户看的整句在 config/prompts.yml 的 messages.tool_failure_* 下。
    _TOOL_FAILURE_CATEGORY_LABELS: dict[str, str] = {
        "timeout": "超时",
        "permission": "权限不足",
        "missing_args": "关键信息不全",
        "blocked": "被安全策略拦下",
        "unavailable": "所需能力当前不可用",
        "not_found": "没找到内容",
        "upstream": "外部来源没响应",
        "unknown": "没能完成",
    }
    # 这些兜底原因说明预算已经耗尽（模型侧超时 / 报错），再打一次 LLM 只会
    # 二次超时。此时直接用类别兜底句，不做第二次模型调用。
    _NO_SECOND_LLM_FALLBACK_REASONS = frozenset(
        {"total_timeout", "llm_timeout", "llm_error"}
    )

    def __init__(
        self,
        model_client: ModelClient,
        tool_registry: AgentToolRegistry,
        config: dict[str, Any],
        persona_text: str = "",
        skill_registry: Any = None,
        step_journal: Any = None,
        checkpoint_dir: Any = None,
    ):
        self.model_client = model_client
        self.tool_registry = tool_registry
        self.persona_text = persona_text
        self.skill_registry = skill_registry
        self.step_journal = step_journal
        self.checkpoint_dir = checkpoint_dir
        self.config: dict[str, Any] = {}
        self.max_steps = 8
        self.max_tokens = 4096
        self.enable = True
        self.fallback_on_parse_error = True
        self.allow_silent_on_llm_error = False
        self.repeat_tool_guard_enable = True
        self.max_same_tool_call = 3
        self.max_consecutive_think = 3
        self.tool_timeout_seconds = 28
        self.tool_timeout_seconds_media = 45
        self.llm_step_timeout_seconds = 22
        self.llm_step_timeout_seconds_after_tool = 32
        self.navigator_obvious_tool_timeout_seconds = 5.0
        self.navigator_preflight_plain_text = False
        self.navigator_preflight_timeout_seconds = 8.0
        self.navigator_post_timeout_retry_seconds = 5.0
        self.navigator_chain_wall_clock_seconds = 55.0
        self.navigator_retry_model = ""
        self.total_timeout_seconds = 0
        self.queue_timeout_margin_seconds = 8
        self.prompt_policy = PromptPolicy.from_config({})
        self._admin_ids: set[str] = set()
        self._pending_high_risk_actions: dict[str, dict[str, Any]] = {}
        # Claude Code 式 PreToolUse 审批钩子：外部可注册 `(ctx, tool_name, tool_args) -> str`，
        # 返回非空字符串 = 阻止该工具并回喂这段说明。内置高风险守卫作为机制保留，
        # 钩子是额外的可插拔审批层。
        self._pre_tool_hooks: list[Any] = []
        self.high_risk_control_enable = True
        self.high_risk_default_require_confirmation = True
        self.high_risk_categories: set[str] = {"admin"}
        self.high_risk_pending_ttl_seconds = 180
        self.high_risk_name_patterns: tuple[re.Pattern[str], ...] = ()
        self.high_risk_description_patterns: tuple[re.Pattern[str], ...] = ()
        self.high_risk_user_enable_patterns: tuple[re.Pattern[str], ...] = ()
        self.high_risk_user_disable_patterns: tuple[re.Pattern[str], ...] = ()
        self.high_risk_use_confirm_token = False
        self.high_risk_confirm_cues: tuple[str, ...] = ()
        self.high_risk_cancel_cues: tuple[str, ...] = ()
        self.search_followup_resend_media_cues: tuple[str, ...] = ()
        self.tool_args_log_max_chars = 600

        # 安全: 需要管理员权限的工具 (与 AgentToolRegistry 保持同步)
        self._super_admin_tools = set(AgentToolRegistry._SUPER_ADMIN_TOOLS)
        # 从 registry 取，不再手维护第二份。
        #
        # 原来这里是一份硬编码清单，和 `AgentToolRegistry._GROUP_ADMIN_TOOLS`
        # 各自维护，**已经漂移**：2026-08-06 实测 registry 有 16 项、这里 15 项，
        # 少的是 `recall_recent_messages` —— 于是批量撤回跳过了 :2286 的
        # 「执行群管理操作前需要明确点名机器人」这道门，而它 15 个同族兄弟都受这道门管。
        #
        # 两份清单漂移也是同一批权限漏洞的根因（delete_group_folder /
        # set_group_add_request / upload_* 等六个工具当时两边都漏）。
        # registry 是权限的执行方（:272 那两处 return permission_denied），
        # 所以以它为真相源。
        self._group_admin_tools = set(AgentToolRegistry._GROUP_ADMIN_TOOLS)
        self._admin_only_tools = self._super_admin_tools | self._group_admin_tools
        self.refresh_runtime_config(config)

    def refresh_runtime_config(
        self, config: dict[str, Any], persona_text: str | None = None
    ) -> None:
        """热更新 Agent 的运行参数、管理员权限集合，以及人格底稿。

        persona_text 必须一起刷：它在 core/engine.py:183 是**按值**传进构造函数的，
        而 reload_config() 不重建 AgentLoop、只调本方法。原先本方法不碰它，
        于是 `/yukibot` 之后 router 路和 thinking 路拿到了新人格，
        **agent 路（线上每回合都走的那条）还在用进程启动时那份旧稿** ——
        改 config/personas/yukiko.md 看起来"热重载成功"，实际对主路径零效果。
        传 None 表示调用方不打算改它，保持现值。
        """
        self.config = config if isinstance(config, dict) else {}
        if persona_text is not None:
            new_persona = normalize_text(str(persona_text))
            if new_persona != normalize_text(str(getattr(self, "persona_text", "") or "")):
                _log.info(
                    "agent_persona_reloaded | chars=%d",
                    len(new_persona),
                )
            self.persona_text = str(persona_text)
        agent_cfg = (
            self.config.get("agent", {}) if isinstance(self.config, dict) else {}
        )
        if not isinstance(agent_cfg, dict):
            agent_cfg = {}
        self.max_steps = max(1, min(15, int(agent_cfg.get("max_steps", 8))))
        self.max_tokens = max(512, int(agent_cfg.get("max_tokens", 4096)))
        self.enable = bool(agent_cfg.get("enable", True))
        self.fallback_on_parse_error = bool(
            agent_cfg.get("fallback_on_parse_error", True)
        )
        self.allow_silent_on_llm_error = bool(
            agent_cfg.get("allow_silent_on_llm_error", False)
        )
        self.repeat_tool_guard_enable = bool(
            agent_cfg.get("repeat_tool_guard_enable", True)
        )
        self.max_same_tool_call = max(
            2, min(8, int(agent_cfg.get("max_same_tool_call", 3)))
        )
        self.max_consecutive_think = max(
            2, min(8, int(agent_cfg.get("max_consecutive_think", 3)))
        )
        self.tool_timeout_seconds = max(
            8, min(120, int(agent_cfg.get("tool_timeout_seconds", 28)))
        )
        self.tool_timeout_seconds_media = max(
            self.tool_timeout_seconds,
            min(180, int(agent_cfg.get("tool_timeout_seconds_media", 45))),
        )
        self.llm_step_timeout_seconds = max(
            6, min(120, int(agent_cfg.get("llm_step_timeout_seconds", 30)))
        )
        self.llm_step_timeout_seconds_after_tool = max(
            self.llm_step_timeout_seconds,
            min(
                120,
                int(
                    agent_cfg.get(
                        "llm_step_timeout_seconds_after_tool",
                        max(32, self.llm_step_timeout_seconds),
                    )
                ),
            ),
        )
        # 默认 0（关闭）。这个 cap 的作用是「分区已有明确证据时故意早超时，
        # 落进小 prompt 重试」，前提是 LLM 能在 cap 内答完。实测本项目的 provider
        # （skiapi）小 prompt 延迟 6.7 / 8.6 / 10.5 / 10.7 秒 —— 最快 6.7 秒，
        # 原默认 5 秒低于物理下限，于是主调用 100% 超时（日志 68 次），
        # 而它落进的那条小 prompt 重试自己也超时（41 次里 39 次失败，95%）。
        # 净效果是每回合白烧 5 秒、再拿一个上下文更少的决策，比不做这个优化更慢更笨。
        # 机制保留：provider 确实能快速响应时把它配成正数即可。
        try:
            navigator_obvious_tool_timeout = float(
                agent_cfg.get("navigator_obvious_tool_timeout_seconds", 0.0)
            )
        except (TypeError, ValueError):
            navigator_obvious_tool_timeout = 0.0
        self.navigator_obvious_tool_timeout_seconds = max(
            0.0, min(30.0, navigator_obvious_tool_timeout)
        )
        self.navigator_preflight_plain_text = bool(
            agent_cfg.get("navigator_preflight_plain_text", False)
        )
        # preflight 的小 prompt 预算。原先是硬编码的 `min(20.0, …)`，实测
        # （storage/logs/yukiko.log）**有收益**的 preflight 均值只有 8.6s，而
        # 51 次超时的均值 17.9s、37 次返回 same_section 的均值 9.4s ——
        # 合计 562 秒纯白等。压到 8s 基本不损失命中，砍掉的是长尾白等。
        self.navigator_preflight_timeout_seconds = self._clamp_float_cfg(
            agent_cfg.get("navigator_preflight_timeout_seconds", 8.0), 3.0, 30.0, 8.0
        )
        # 主 LLM 已经超时 = provider 当前不可用，同一个 provider 上再要 20 秒
        # 只是把用户的等待翻倍。实测 trace 118886-14：20s preflight + 45s 主调用
        # + 20s retry = 85 秒，用户只拿到一句「我这边处理超时了」。
        self.navigator_post_timeout_retry_seconds = self._clamp_float_cfg(
            agent_cfg.get("navigator_post_timeout_retry_seconds", 5.0), 0.0, 30.0, 5.0
        )
        # 一整条 preflight → 主调用 → retry 链的统一墙钟上限。没有它，三段
        # 各自独立看 `remaining`，20/45/20 就这么叠成 85 秒。
        self.navigator_chain_wall_clock_seconds = self._clamp_float_cfg(
            agent_cfg.get("navigator_chain_wall_clock_seconds", 55.0), 10.0, 300.0, 55.0
        )
        self.navigator_retry_model = normalize_text(
            str(agent_cfg.get("navigator_retry_model", "") or "")
        )
        self.total_timeout_seconds = max(
            0, int(agent_cfg.get("total_timeout_seconds", 0))
        )
        self.queue_timeout_margin_seconds = max(
            1, min(30, int(agent_cfg.get("queue_timeout_margin_seconds", 8)))
        )
        self.prompt_policy = PromptPolicy.from_config(self.config)
        self._refresh_high_risk_control(agent_cfg)
        followup_cfg = (
            self.config.get("search_followup", {})
            if isinstance(self.config, dict)
            else {}
        )
        if not isinstance(followup_cfg, dict):
            followup_cfg = {}
        resend_media_cues_raw = followup_cfg.get("resend_media_cues", [])
        if not isinstance(resend_media_cues_raw, list):
            resend_media_cues_raw = []
        resend_media_cues = [
            normalize_text(str(item)).lower()
            for item in resend_media_cues_raw
            if normalize_text(str(item))
        ]
        self.search_followup_resend_media_cues = tuple(dict.fromkeys(resend_media_cues))

        self.tool_args_log_max_chars = max(
            200, int(agent_cfg.get("tool_args_log_max_chars", 600))
        )
        admin_cfg = (
            self.config.get("admin", {}) if isinstance(self.config, dict) else {}
        )
        if not isinstance(admin_cfg, dict):
            admin_cfg = {}
        self._admin_ids = set()
        for key in ("admin_ids", "super_users"):
            rows = admin_cfg.get(key, [])
            if isinstance(rows, list):
                for item in rows:
                    uid = str(item).strip()
                    if uid:
                        self._admin_ids.add(uid)
        sq = str(admin_cfg.get("super_admin_qq", "")).strip()
        if sq:
            self._admin_ids.add(sq)

        # 加白群集合 (从 admin 配置读取)
        self._whitelisted_groups: set[int] = set()
        for x in admin_cfg.get("whitelist_groups", []) or []:
            try:
                self._whitelisted_groups.add(int(x))
            except (ValueError, TypeError):
                pass

    def _resolve_permission_level(self, ctx: "AgentContext") -> str:
        """根据用户身份和群角色计算权限等级。

        返回: "super_admin" / "group_admin" / "user"
        - super_admin: 在 _admin_ids 中的超级管理员，凌驾一切
        - group_admin: 加白群中的群主或管理员
        - user: 普通用户
        """
        uid = str(ctx.user_id).strip()
        if uid in self._admin_ids:
            return "super_admin"
        # 群管理员: 必须在加白群 + 群角色是 owner 或 admin
        if not ctx.is_private and ctx.group_id:
            role = (ctx.sender_role or "").lower()
            if ctx.is_whitelisted_group and role in ("owner", "admin"):
                return "group_admin"
        return "user"

    def _is_explicit_bot_addressed(self, ctx: "AgentContext") -> bool:
        """是否明确在和机器人说话（用于高风险管理工具额外护栏）。"""
        return bool(ctx.is_private or ctx.mentioned)

    @staticmethod
    def _resolve_navigator_retry_timeout(
        remaining: float, budget_cap: float | None
    ) -> float:
        """小 prompt 重试的实际预算；`<= 0` 表示别调了。

        `budget_cap` 是调用点给的这一段的上限（preflight 一档、主调用超时后
        一档），`remaining` 是本回合墙钟剩余。取两者较小值，留 2 秒余量给
        解析和后续步骤。原先这里硬编码 `min(20.0, …)`，preflight 与超时后
        retry 共用同一个 20 秒 —— 于是 20 + 45 + 20 叠成 85 秒。
        """
        cap = 20.0 if budget_cap is None else float(budget_cap)
        if cap <= 0:
            return 0.0
        timeout = min(cap, max(0.0, remaining - 2.0))
        # 低于 2.5 秒的调用几乎必然超时，白付一次延迟，不如直接跳过。
        return timeout if timeout > 2.5 else 0.0

    @staticmethod
    def _clamp_float_cfg(raw: Any, low: float, high: float, default: float) -> float:
        """读一个秒数型配置并夹到 [low, high]；读不出数就用 default。"""
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        if value != value:  # NaN
            return default
        return max(low, min(high, value))

    @staticmethod
    def _is_directed_turn(ctx: AgentContext) -> bool:
        """本回合有没有人在跟机器人说话。全是结构事实，不看词义。

        engine 显式给了 `was_directed` 就听它的；没给（None）时用四个已有结构
        事实推断：私聊 / 被 @ / engine 已判定 explicit_bot_addressed / 引用了
        机器人自己的消息。都不成立 = 旁听探测轮。
        """
        explicit = getattr(ctx, "was_directed", None)
        if explicit is not None:
            return bool(explicit)
        if ctx.is_private or ctx.mentioned or ctx.explicit_bot_addressed:
            return True
        # 引用机器人自己的发言 = 在跟机器人接话，属于指向。
        bot_id = normalize_text(str(ctx.bot_id))
        return bool(bot_id) and normalize_text(str(ctx.reply_to_user_id)) == bot_id

    @staticmethod
    def _compile_regex_patterns(values: Any) -> tuple[re.Pattern[str], ...]:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return ()
        patterns: list[re.Pattern[str]] = []
        for item in values:
            raw = normalize_text(str(item))
            if not raw:
                continue
            try:
                patterns.append(re.compile(raw, re.IGNORECASE))
            except re.error:
                continue
        return tuple(patterns)

    @staticmethod
    def _normalize_word_tuple(values: Any, default: tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            values = list(default)
        rows = [
            normalize_text(str(item)).lower()
            for item in values
            if normalize_text(str(item))
        ]
        return tuple(rows) if rows else default

    def _refresh_high_risk_control(self, agent_cfg: dict[str, Any]) -> None:
        control = (
            agent_cfg.get("high_risk_control", {})
            if isinstance(agent_cfg, dict)
            else {}
        )
        if not isinstance(control, dict):
            control = {}
        default_name_patterns = [
            "^set_group_",
            "^delete_",
            "^ban_",
            "^kick_",
            "^config_update$",
            "^admin_command$",
            "^cli_invoke$",
            "^upload_group_file$",
            "^smart_download$",
        ]
        default_description_patterns = [
            "不可逆",
            "踢出群",
            "删除",
            "封禁",
            "禁言",
            "管理员权限",
            "可执行文件",
        ]
        self.high_risk_control_enable = bool(control.get("enable", True))
        self.high_risk_default_require_confirmation = bool(
            control.get("default_require_confirmation", True)
        )
        categories_raw = control.get("categories", ["admin"])
        if isinstance(categories_raw, str):
            categories_raw = [categories_raw]
        if isinstance(categories_raw, list):
            self.high_risk_categories = {
                normalize_text(str(item)).lower()
                for item in categories_raw
                if normalize_text(str(item))
            } or {"admin"}
        else:
            self.high_risk_categories = {"admin"}
        self.high_risk_pending_ttl_seconds = max(
            30, int(control.get("pending_ttl_seconds", 180))
        )
        self.high_risk_name_patterns = self._compile_regex_patterns(
            control.get("tool_name_patterns", default_name_patterns)
        )
        self.high_risk_description_patterns = self._compile_regex_patterns(
            control.get("description_patterns", default_description_patterns)
        )
        self.high_risk_user_enable_patterns = self._compile_regex_patterns(
            control.get("user_enable_patterns", [])
        )
        self.high_risk_user_disable_patterns = self._compile_regex_patterns(
            control.get("user_disable_patterns", [])
        )
        self.high_risk_use_confirm_token = bool(control.get("use_confirm_token", False))
        self.high_risk_confirm_cues = self._normalize_word_tuple(
            control.get("confirm_cues"),
            ("确认", "确认执行", "继续执行", "确定执行", "yes"),
        )
        self.high_risk_cancel_cues = self._normalize_word_tuple(
            control.get("cancel_cues"),
            ("取消", "算了", "停止", "不执行", "撤销"),
        )
        self._cleanup_pending_high_risk(force=True)

    def _pending_high_risk_key(self, ctx: AgentContext) -> str:
        return f"{ctx.conversation_id}:{ctx.user_id}"

    def _cleanup_pending_high_risk(self, force: bool = False) -> None:
        if not self._pending_high_risk_actions:
            return
        if force:
            self._pending_high_risk_actions.clear()
            return
        now = time.time()
        stale: list[str] = []
        for key, payload in self._pending_high_risk_actions.items():
            expires_at = float(payload.get("expires_at", 0))
            if expires_at <= 0 or expires_at < now:
                stale.append(key)
        for key in stale:
            self._pending_high_risk_actions.pop(key, None)

    # 决定「这个操作作用在谁/哪个群身上」的参数。高风险确认只在这些参数变化时
    # 重新提示 —— 模型第二轮常会附带生成 reason 之类的字段，把那也算漂移会让
    # 管理员陷入确认死循环（tests/test_high_risk_and_sticker_regression.py 钉过）。
    _IDENTITY_ARG_KEYS = (
        "target",
        "user_id",
        "group_id",
        "member_id",
        "message_id",
        "action",
        "file",
        "folder",
        "key",
        "path",
    )

    @classmethod
    def _build_identity_signature(cls, args: dict[str, Any]) -> str:
        """只按身份类参数生成签名，用于判断确认轮里对象是否被更正。"""

        if not isinstance(args, dict):
            return ""
        subset = {k: args[k] for k in cls._IDENTITY_ARG_KEYS if k in args}
        if not subset:
            return ""
        return cls._build_args_signature(subset)

    @staticmethod
    def _build_args_signature(args: dict[str, Any]) -> str:
        """Build a normalized signature for repeat-tool detection.

        Strips whitespace from string values and lowercases them so that
        minor LLM variations like trailing spaces or case changes are
        treated as the same call.

        字符串先做 NFKC 归一化：模型经常只把半角括号换成全角
        （`(QQ:1)` → `（QQ:1）`）就绕过重复判定，导致同一副作用工具被真的
        执行第二次（实测同一条 remember_user_fact 入库 4 次）。
        只做等价字形折叠，不做任何语义/相似度判断。
        """
        def _norm(v: Any) -> Any:
            if isinstance(v, str):
                return unicodedata.normalize("NFKC", v).strip().lower()
            if isinstance(v, dict):
                return {k: _norm(val) for k, val in v.items()}
            if isinstance(v, list):
                return [_norm(item) for item in v]
            return v
        try:
            return json.dumps(_norm(args or {}), ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(args or {})

    # 守卫自己写进 steps 的 error 标记，不是工具真实失败原因，重述时要跳过
    _GUARD_STEP_ERRORS = (
        "repeated_tool_call:",
        "duplicate_external_fact_query",
        "consecutive_crashes_guard",
    )

    @classmethod
    def _last_failure_text(cls, steps: list[dict[str, Any]]) -> str:
        """取最近一次「工具真实失败」的原因文本，跳过守卫自己写的标记。"""
        for step in reversed(steps):
            if bool(step.get("ok")):
                continue
            error = normalize_text(str(step.get("error", "")))
            if error.startswith(cls._GUARD_STEP_ERRORS):
                continue
            detail = error or normalize_text(str(step.get("display", "")))
            if detail:
                return clip_text(detail, 300)
        return ""

    def _build_guard_feedback_payload(
        self,
        *,
        tool_name: str,
        steps: list[dict[str, Any]],
        reason_key: str,
        reason_text: str,
    ) -> dict[str, Any]:
        """守卫拦截时回喂给模型的结构化 payload。

        旧实现只回喂一句「禁止再次调用」的祈使句：模型既看不到上一次已经拿到
        的结果，也看不到上一次失败的真实原因，于是原地重试到熔断。这里把两者
        都显式带上，让模型自己决定 final_answer 还是换工具——不做本地语义否决。
        """
        payload: dict[str, Any] = {"tool": tool_name, "ok": False, "error": reason_text}
        # 只认这个工具自己拿到过的产物：否则别的工具的图会被说成是它的结果
        own_steps = [
            s
            for s in steps
            if normalize_text(str(s.get("tool", ""))).lower() == tool_name.lower()
        ]
        obtained: dict[str, Any] = {}
        summary = self._last_success_display(own_steps)
        if summary:
            obtained["summary"] = summary
        image_urls = self._last_success_image_urls(own_steps)
        if image_urls:
            obtained["image_urls"] = image_urls[:3]
        video_url = self._last_success_video_url(own_steps)
        if video_url:
            obtained["video_url"] = video_url
        audio_file = self._last_success_audio_file(own_steps)
        if audio_file:
            obtained["audio_file"] = audio_file
        # 「这个工具最近一次到底成功还是失败」决定该讲哪句话。
        # 不看这一点会同时错三处（都实测过）：
        #   - 先成功、随后连续崩溃时，obtained 非空就抢先说「上一次调用已经成功」，
        #     真实错误整条丢掉 —— 与这次修复要达到的「让模型能解释为什么失败」正好相反；
        #   - 失败原因取自全量 steps 而不是这个工具自己的 steps，
        #     于是别的工具的错误被说成「该工具最近一次失败的真实原因」；
        #   - 产物取的是「最后一次成功」，与被拦那次的参数无关。
        # 所以：最近一次失败了就以失败为主叙述，产物仅作附带；成功才说成功。
        last_own_failed = False
        for step in reversed(own_steps):
            if normalize_text(str(step.get("error", ""))).startswith(
                self._GUARD_STEP_ERRORS
            ):
                # 守卫自己写的标记不算一次真实调用结果
                continue
            last_own_failed = not bool(step.get("ok"))
            break

        own_failure = self._last_failure_text(own_steps)

        if obtained and not last_own_failed:
            payload["already_obtained"] = obtained
            payload["display"] = _pl.get_message(
                "agent_guard_already_obtained",
                "上一次调用已经成功，结果见 already_obtained。请直接用 final_answer 回复，"
                "并原样携带其中的 image_urls / video_url / audio_file，不要再调用这个工具。",
            )
            return payload

        failure = own_failure or reason_key
        template = _pl.get_message(
            "agent_guard_last_error",
            "该工具最近一次失败的真实原因：{error}。请换其他工具，或用 final_answer "
            "如实向用户说明这件事没做成以及为什么，不要重复同样的调用。",
        )
        payload["display"] = (
            template.replace("{error}", failure)
            if "{error}" in template
            else f"{template} {failure}"
        )
        if obtained:
            # 先前确实拿到过东西，但最近一次是失败的。两件事都给，
            # 并说清产物是「更早那次」的，避免模型把它当成本次结果去交付。
            payload["earlier_partial_result"] = obtained
        return payload

    @staticmethod
    def _rewrite_tool_call_arguments(
        tool_call: dict[str, Any], args: dict[str, Any]
    ) -> None:
        """用解码后的参数覆盖 tool_call 里的原始 arguments 串。

        `assistant_msg` 会被原样追加进 messages 回送 provider，所以历史里必须是
        合法 JSON。否则下一轮 provider 解析自己吐出的畸形串得到空参数，模型看到
        「上一轮我没带参数」，就会重复调用同一个工具直到重复守卫熔断。
        """

        func = tool_call.get("function")
        if not isinstance(func, dict):
            return
        try:
            func["arguments"] = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            func["arguments"] = "{}"

    @staticmethod
    def _decode_tool_call_arguments(
        raw: Any,
        *,
        tool_name: str = "",
        trace_id: str = "",
        step_idx: int = -1,
    ) -> dict[str, Any]:
        """解析原生 tool_call 的 arguments，容忍串接的多个 JSON 对象。

        实测 skiapi 会返回 `{}{"keyword": "..."}` —— 真参数前粘了一个空对象，
        `json.loads` 抛 `Extra data`。此前这里静默兜成 `{}`，结果每个带参工具
        都拿到空参数且日志无痕，故障完全不可见。

        逐段 raw_decode，合并所有解出的对象，后出现的键覆盖先出现的空值。
        任何异常都必须留日志 —— 静默是这个 bug 能藏这么久的唯一原因。
        """

        if isinstance(raw, dict):
            return raw
        if raw is None:
            return {}

        text = raw if isinstance(raw, str) else str(raw)
        if not text.strip():
            return {}

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # Hermes 式第三级兜底：模型可能把 JSON 包在 ```json ... ``` markdown 块里。
        md_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
        if md_match:
            candidate = md_match.group(1).strip()
            if candidate:
                try:
                    parsed_md = json.loads(candidate)
                    if isinstance(parsed_md, dict):
                        AgentLoop._malformed_args_recovered_total += 1
                        _log.info(
                            "agent_tool_args_markdown_extracted | trace=%s | step=%d | tool=%s",
                            trace_id,
                            step_idx,
                            tool_name,
                        )
                        return parsed_md
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
                # markdown 里可能是串接 JSON（如 `{}{"keyword": "z"}`）——让 raw_decode
                # 跑在提取出的 candidate 上，而不是带 ``` 围栏的原始文本。
                text = candidate

        merged: dict[str, Any] = {}
        decoder = json.JSONDecoder()
        idx = 0
        chunks = 0
        clobbered: list[str] = []
        while idx < len(text):
            while idx < len(text) and text[idx].isspace():
                idx += 1
            if idx >= len(text):
                break
            try:
                obj, end = decoder.raw_decode(text, idx)
            except ValueError:
                break
            chunks += 1
            if isinstance(obj, dict):
                # 后段覆盖前段是既定行为，但「覆盖成不同的值」意味着有一个真参数
                # 被悄悄丢掉了 —— 那是必须报警的形态，不是正常恢复。
                clobbered.extend(
                    key for key, value in obj.items()
                    if key in merged and merged[key] != value
                )
                merged.update(obj)
            idx = end

        # 是否把整段文本都消费掉了。没消费完 = 有一段解不出来被丢了
        # （例如 provider 把第二段截断），而 merged 非空会让它看起来像成功。
        tail = text[idx:].strip()

        if merged:
            AgentLoop._malformed_args_recovered_total += 1
            if clobbered or tail:
                # 真告警：恢复过程本身有损。
                _log.warning(
                    "agent_tool_args_recovery_lossy | trace=%s | step=%d | tool=%s "
                    "| chunks=%d | raw_len=%d | clobbered=%s | unconsumed=%s",
                    trace_id,
                    step_idx,
                    tool_name,
                    chunks,
                    len(text),
                    ",".join(sorted(set(clobbered))) or "-",
                    clip_text(tail, 120) or "-",
                )
            else:
                # 无损恢复。实测 skiapi 89% 的工具调用走这条路（37 次里 33 次，
                # 形态全是 `{}` + 真参数两段），逐条 WARNING 会淹掉日志，
                # 所以降到 DEBUG，另外每 N 次汇总一条 INFO 保留可见度。
                _log.debug(
                    "agent_tool_args_recovered_from_malformed_json | trace=%s | step=%d "
                    "| tool=%s | chunks=%d | raw_len=%d",
                    trace_id,
                    step_idx,
                    tool_name,
                    chunks,
                    len(text),
                )
                total = AgentLoop._malformed_args_recovered_total
                if total % AgentLoop._MALFORMED_ARGS_LOG_EVERY == 0:
                    _log.info(
                        "agent_tool_args_malformed_json_rollup | total=%d | latest_tool=%s "
                        "| chunks=%d | provider 持续返回串接 JSON，恢复无损",
                        total,
                        tool_name,
                        chunks,
                    )
            return merged

        _log.warning(
            "agent_tool_args_unparseable | trace=%s | step=%d | tool=%s | raw=%s",
            trace_id,
            step_idx,
            tool_name,
            clip_text(text, 200),
        )
        return {}

    def _truncate_tool_args_for_log(self, tool_args: dict[str, Any]) -> str:
        """将 tool_args 序列化并截断用于日志，默认 600 字符。"""
        limit = getattr(self, "tool_args_log_max_chars", 600)
        try:
            raw = json.dumps(tool_args, ensure_ascii=False)
        except Exception:
            raw = str(tool_args)
        if len(raw) <= limit:
            return raw
        return raw[:limit] + f"... [truncated={len(raw)}]"
    def _build_tool_context(
        self, ctx: AgentContext, permission_level: str
    ) -> dict[str, Any]:
        return {
            "api_call": ctx.api_call,
            "output_filter": ctx.output_filter,
            "topic_gate": ctx.topic_gate,
            "admin_handler": ctx.admin_handler,
            "config_patch_handler": ctx.config_patch_handler,
            "sticker_manager": ctx.sticker_manager,
            "tool_executor": ctx.tool_executor,
            "crawler_hub": ctx.crawler_hub,
            "knowledge_base": ctx.knowledge_base,
            "memory_engine": ctx.memory_engine,
            "conversation_id": ctx.conversation_id,
            "user_id": ctx.user_id,
            "user_name": ctx.user_name,
            "group_id": ctx.group_id,
            "bot_id": ctx.bot_id,
            "is_private": ctx.is_private,
            "mentioned": ctx.mentioned,
            "explicit_bot_addressed": ctx.explicit_bot_addressed,
            "trace_id": ctx.trace_id,
            "message_text": ctx.message_text,
            "original_message_text": ctx.original_message_text or ctx.message_text,
            "message_id": ctx.message_id,
            "raw_segments": ctx.raw_segments,
            "reply_media_segments": ctx.reply_media_segments,
            "reply_to_message_id": ctx.reply_to_message_id,
            "reply_to_user_id": ctx.reply_to_user_id,
            "reply_to_user_name": ctx.reply_to_user_name,
            "reply_to_text": ctx.reply_to_text,
            "at_other_user_ids": ctx.at_other_user_ids,
            "at_other_user_names": ctx.at_other_user_names,
            "memory_context": ctx.memory_context,
            "related_memories": ctx.related_memories,
            "user_profile_summary": ctx.user_profile_summary,
            "preferred_name": ctx.preferred_name,
            "recent_speakers": ctx.recent_speakers,
            "thread_state": ctx.thread_state,
            "runtime_group_context": ctx.runtime_group_context,
            "runtime_admin_policy": ctx.runtime_admin_policy,
            "media_summary": ctx.media_summary,
            "reply_media_summary": ctx.reply_media_summary,
            "event_payload": ctx.event_payload,
            "user_policies": ctx.user_policies,
            "user_directives": ctx.user_directives,
            "sender_role": ctx.sender_role,
            "is_whitelisted_group": ctx.is_whitelisted_group,
            "is_admin_user": permission_level in ("super_admin", "group_admin"),
            "permission_level": permission_level,
            "config": self.config,
        }

    @staticmethod
    def _tool_result_reply_text(tool_name: str, result: ToolCallResult) -> str:
        display = normalize_text(result.display)
        if display:
            return display
        if result.ok:
            return f"{tool_name} 已执行。"
        error = normalize_text(result.error)
        if error:
            return f"{tool_name} 执行失败：{error}"
        return f"{tool_name} 执行失败。"

    def _is_confirmation_text(
        self, text: str, pending: dict[str, Any] | None = None
    ) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        if isinstance(pending, dict):
            token = normalize_text(str(pending.get("confirm_token", ""))).lower()
            if token and token in content:
                return True
        return any(
            cue in content and not self._cue_is_negated(content, cue)
            for cue in self.high_risk_confirm_cues
        )

    @staticmethod
    def _cue_is_negated(content: str, cue: str) -> bool:
        """确认词前面紧挨着否定词时，这不是确认，是拒绝。

        2026-08-06 子 agent 审计发现：`confirm_cues` 用的是**无锚点子串匹配**，
        而「我不确认」「别确认」「无法确认」「不要确认」都包含「确认」，于是
        `_is_confirmation_text` 全部返回 True。取消判定又拦不住它们
        （cancel_cues 里没有这些词形），所以二次确认这道闸门**在用户明确拒绝时
        反而放行封禁**。实测四种说法全部走到执行。

        这里只做**语法否定**判定，不是语义词表：中文否定词是一个封闭的小集合，
        且必须紧邻确认词才算（"确认取消订单" 里的「取消」不该让确认失效）。
        `use_confirm_token` 打开时走 token 路径，不依赖这层。
        """

        negators = ("不", "别", "勿", "非", "无法", "未", "没", "莫")
        # 否定词与确认词之间常隔一个情态字（「不要确认」「不能确认」「不用确认」）。
        # 这些字本身不表意，剥掉再判否定。注意不能把主语一起剥 ——
        # 「我要确认」剥成「我」，不是否定词，仍算确认。
        modals = "要想能会用准许可以得了着"
        start = 0
        while True:
            idx = content.find(cue, start)
            if idx < 0:
                return True  # 每一处出现都被否定了
            prefix = content[:idx].rstrip(modals)
            if not any(prefix.endswith(neg) for neg in negators):
                return False  # 存在一处未被否定的确认词 → 视为确认
            start = idx + 1

    def _is_cancellation_text(
        self, text: str, pending: dict[str, Any] | None = None
    ) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        if isinstance(pending, dict):
            token = normalize_text(str(pending.get("cancel_token", ""))).lower()
            if token and token in content:
                return True
        return any(cue in content for cue in self.high_risk_cancel_cues)

    def _tool_is_high_risk(self, tool_name: str) -> bool:
        schema = self.tool_registry.get_schema(tool_name)
        category = (
            normalize_text(getattr(schema, "category", "")).lower() if schema else ""
        )
        description = (
            normalize_text(getattr(schema, "description", "")) if schema else ""
        )
        if category and category in self.high_risk_categories:
            return True
        if any(pattern.search(tool_name) for pattern in self.high_risk_name_patterns):
            return True
        if description and any(
            pattern.search(description)
            for pattern in self.high_risk_description_patterns
        ):
            return True
        return False

    def _require_high_risk_confirmation_for_user(self, ctx: AgentContext) -> bool:
        runtime_policy = (
            ctx.runtime_admin_policy if isinstance(ctx.runtime_admin_policy, dict) else {}
        )
        if "high_risk_confirmation_required" in runtime_policy:
            return bool(runtime_policy.get("high_risk_confirmation_required"))
        return self.high_risk_default_require_confirmation

    @staticmethod
    def _is_regular_user_self_ban_attempt(
        ctx: AgentContext,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> bool:
        if tool_name == "delete_message":
            # 自助撤回例外：普通用户可撤回机器人自己发送的消息。
            # 归属校验在 handler（_handle_delete_message）内完成，这里只放行到 handler。
            return True
        if tool_name != "set_group_ban":
            return False
        target_uid = normalize_text(str((tool_args or {}).get("user_id", "")))
        current_uid = normalize_text(str(ctx.user_id))
        if not current_uid:
            return False
        return not target_uid or target_uid == current_uid

    def _build_high_risk_confirm_prompt(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> tuple[str, str, str]:
        target = ""
        if isinstance(tool_args, dict):
            for key in ("user_id", "target_user_id", "group_id"):
                value = normalize_text(str(tool_args.get(key, "")))
                if value:
                    target = f"{key}={value}"
                    break
        detail = f"（{target}）" if target else ""
        confirm_token = ""
        cancel_token = ""
        if bool(getattr(self, "high_risk_use_confirm_token", False)):
            short = secrets.token_hex(2).lower()
            confirm_token = f"confirm-{short}"
            cancel_token = f"cancel-{short}"
            prompt = (
                f"这是高风险操作：{tool_name}{detail}。"
                f"请回复“{confirm_token}”确认执行，或回复“{cancel_token}”取消。"
            )
            return prompt, confirm_token, cancel_token
        return (
            (
                f"这是高风险操作：{tool_name}{detail}。"
                "请二次确认后我才会执行。"
                "请回复“确认执行”，或回复“取消”。"
            ),
            confirm_token,
            cancel_token,
        )

    def _cross_group_authority_error(
        self, ctx: AgentContext, tool_name: str, tool_args: dict[str, Any]
    ) -> str:
        """群管理员的权限只在他本群有效 —— 跨群的高风险操作一律拒绝。

        2026-08-06 子 agent 审计发现：`_resolve_permission_level` 按**消息来源群**
        （`ctx.group_id` + `ctx.sender_role` + 该群已加白）授予 `group_admin`，
        而 `set_group_ban` 这类 handler 的 `group_id` 是从**模型参数**读的，
        两者从不交叉校验。后果：在 A 群当管理的人，可以让机器人对 B 群执行封禁
        —— 权限在 A 群赚到，作用在 B 群，而 B 群里他可能什么都不是。

        super_admin 不受此限（它本来就凌驾一切，见 `_resolve_permission_level`）。
        """

        if not self._tool_is_high_risk(tool_name):
            return ""
        # 先读参数里的目标群：没写 group_id 或就是本群时，什么都不用做。
        # 顺序有意如此 —— 只有真的出现跨群嫌疑才去解析权限等级，
        # 那一步要读 ctx 的多个字段，不该在每次高风险调用上白跑。
        try:
            target_group = int(tool_args.get("group_id", 0) or 0)
        except (TypeError, ValueError):
            return ""
        if not target_group:
            return ""
        if target_group == int(getattr(ctx, "group_id", 0) or 0):
            return ""
        if self._resolve_permission_level(ctx) != "group_admin":
            return ""
        _log.warning(
            "high_risk_cross_group_blocked | trace=%s | tool=%s | authority_group=%s "
            "| target_group=%s | user=%s",
            ctx.trace_id,
            tool_name,
            ctx.group_id,
            target_group,
            ctx.user_id,
        )
        return "这个操作要在目标群里由那个群的管理员发起，我不能跨群执行。"

    def register_pre_tool_hook(self, hook: Any) -> None:
        """注册 PreToolUse 审批钩子（Claude Code hooks 风格）。

        hook 签名 `(ctx, tool_name, tool_args) -> str`：返回非空字符串 = 阻止该工具
        并回喂该字符串作为说明；返回空字符串 = 放行。异常钩子被跳过并记日志。
        """
        if callable(hook):
            self._pre_tool_hooks.append(hook)

    def _run_pre_tool_hooks(
        self, ctx: AgentContext, tool_name: str, tool_args: dict[str, Any]
    ) -> str:
        """按注册顺序跑所有审批钩子；第一个非空返回即阻止。"""
        for hook in self._pre_tool_hooks:
            try:
                block_message = hook(ctx, tool_name, tool_args)
            except Exception:
                _log.warning(
                    "pre_tool_hook_error | trace=%s | tool=%s",
                    getattr(ctx, "trace_id", "-"),
                    tool_name,
                    exc_info=True,
                )
                continue
            if block_message:
                return str(block_message)
        return ""

    def _guard_high_risk_tool_call(
        self, ctx: AgentContext, tool_name: str, tool_args: dict[str, Any]
    ) -> str:
        if not self.high_risk_control_enable:
            return ""
        if not self._tool_is_high_risk(tool_name):
            return ""
        # 跨群越权先拦，且**不受 high_risk_control_enable 之外的确认策略影响** ——
        # 确认策略解决的是「你确定吗」，这里解决的是「你没有这个群的权限」，
        # 后者不该因为某群把确认关掉就放行。
        cross_group = self._cross_group_authority_error(ctx, tool_name, tool_args)
        if cross_group:
            return cross_group
        if not self._require_high_risk_confirmation_for_user(ctx):
            return ""

        self._cleanup_pending_high_risk(force=False)
        key = self._pending_high_risk_key(ctx)
        pending = self._pending_high_risk_actions.get(key)
        msg_text = normalize_text(ctx.message_text)

        if pending and self._is_cancellation_text(msg_text, pending):
            self._pending_high_risk_actions.pop(key, None)
            _log.info("confirm_cancelled | trace=%s | tool=%s", ctx.trace_id, tool_name)
            return "已取消上一条高风险操作，不会执行。"

        if pending:
            pending_tool = normalize_text(str(pending.get("tool_name", "")))
            if (
                self._is_confirmation_text(msg_text, pending)
                and pending_tool == tool_name
            ):
                # 参数在「提示」与「确认」之间变了：**重新提示新参数**，不要静默替换。
                #
                # 原来的做法是无条件用 pending 里保存的参数覆盖当前参数（注释写「防漂移」）。
                # 防漂移的意图是对的 —— 你确认的必须是你看到的那一条。但「静默替换」
                # 把它实现反了：管理员在确认轮里更正对象（「搞错了是 222222，确认执行」）
                # 会导致**原来那个人**被封，而机器人报告成功。2026-08-06 子 agent 审计报的。
                #
                # 正确动作是拒绝这次确认并按新参数重新提示：既没有执行用户没看过的操作，
                # 也没有把更正当成没发生。`args_sig` 本来就是为此写入的
                # （此前只写不读，grep 只有一个写入点）。
                saved_args = pending.get("saved_tool_args")
                saved_identity = self._build_identity_signature(
                    saved_args if isinstance(saved_args, dict) else {}
                )
                current_identity = self._build_identity_signature(tool_args)
                if saved_identity and saved_identity != current_identity:
                    _log.warning(
                        "confirm_args_drifted | trace=%s | tool=%s | 重新提示新参数，"
                        "不执行旧参数",
                        ctx.trace_id,
                        tool_name,
                    )
                    prompt, confirm_token, cancel_token = (
                        self._build_high_risk_confirm_prompt(tool_name, tool_args)
                    )
                    self._pending_high_risk_actions[key] = {
                        "tool_name": tool_name,
                        "args_sig": self._build_args_signature(tool_args),
                        "saved_tool_args": copy.deepcopy(tool_args),
                        "created_at": time.time(),
                        "expires_at": time.time() + self.high_risk_pending_ttl_seconds,
                        "prompt": prompt,
                        "confirm_token": confirm_token,
                        "cancel_token": cancel_token,
                    }
                    return prompt
                self._pending_high_risk_actions.pop(key, None)
                if saved_args is not None:
                    tool_args.clear()
                    tool_args.update(saved_args)
                _log.info("confirm_matched | trace=%s | tool=%s", ctx.trace_id, tool_name)
                return ""
            if pending_tool == tool_name:
                # 同工具但未确认 → 重新提示
                return (
                    normalize_text(str(pending.get("prompt", "")))
                    or self._build_high_risk_confirm_prompt(tool_name, tool_args)[0]
                )
            # 用户在同会话发起了新的高风险操作，覆盖旧待确认项
            self._pending_high_risk_actions.pop(key, None)

        prompt, confirm_token, cancel_token = self._build_high_risk_confirm_prompt(
            tool_name, tool_args
        )


        self._pending_high_risk_actions[key] = {
            "tool_name": tool_name,
            "args_sig": self._build_args_signature(tool_args),
            "saved_tool_args": copy.deepcopy(tool_args),
            "created_at": time.time(),
            "expires_at": time.time() + self.high_risk_pending_ttl_seconds,
            "prompt": prompt,
            "confirm_token": confirm_token,
            "cancel_token": cancel_token,
        }
        return prompt

    async def run(self, ctx: AgentContext) -> AgentResult:
        """执行 Agent 循环，返回最终结果。"""
        t0 = time.monotonic()
        steps: list[dict[str, Any]] = []
        strict_tool_routing = self._strict_tool_routing_enabled()
        model_client = getattr(self, "model_client", None)
        native_tool_calling = bool(
            getattr(model_client, "supports_native_tool_calling", lambda: False)()
        )

        system_prompt = self._build_system_prompt(ctx)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": await self._compose_user_content(ctx, model_client),
            },
        ]

        tool_calls_made = 0
        missing_arg_counts: dict[str, int] = {}
        successful_external_fact_tools = 0
        seen_external_fact_signatures: set[str] = set()
        repeated_tool_counts: dict[str, int] = {}
        # 真执行成功过的 (工具, 参数) 签名。副作用工具的"一回合只做一次"数的是
        # 成功次数，不是调用次数。
        succeeded_tool_signatures: set[str] = set()
        consecutive_tool_errors: dict[str, int] = {}
        # OpenClaw 式实时 loop 检测：同参数同结果连续空转分级止损。
        # 阈值按 max_steps 校准（默认 8）：warning=2 / critical=4 / circuit=8，
        # 保证单回合内 critical/circuit 真实可达，不是死代码。
        loop_guard = LoopGuard(
            warning=max(2, self.max_steps // 3),
            critical=max(3, self.max_steps // 2),
            circuit_breaker=max(4, self.max_steps),
        )
        consecutive_think_count = 0
        # 追踪工具已发送的媒体（避免 final_answer 重复发送）
        tool_sent_media: set[str] = set()
        # 含媒体时给更多时间；总预算自动对齐 queue 超时，避免队列先把任务打断。
        has_media = bool(ctx.media_summary) or bool(ctx.reply_media_summary)
        total_timeout = self._resolve_total_timeout_seconds(ctx, has_media)
        deadline_ts = t0 + total_timeout
        # preflight → 主 LLM → 超时后 retry 这三段的**统一**墙钟上限。
        # 原先三段各自只看 `remaining`（对齐总预算 / queue 超时），于是
        # 20 + 45 + 20 独立叠加成 85 秒（实测 trace 118886-14）。
        chain_deadline_ts = min(
            deadline_ts, t0 + float(self.navigator_chain_wall_clock_seconds)
        )
        strict_tool_policy_blocked = False

        # 工具超时不计入步骤预算（最多 3 次免费），避免慢工具浪费推理机会
        _tool_timeout_free_budget = 3
        step_idx = -1
        # 回合 checkpoint：超时重试时从上次保存的 step_idx/messages/steps 恢复。
        checkpoint = (
            AgentTurnCheckpoint(self.checkpoint_dir)
            if getattr(self, "checkpoint_dir", None)
            else None
        )
        if checkpoint is not None:
            restored = checkpoint.load(ctx.trace_id)
            if restored:
                restored_messages = restored.get("messages")
                if isinstance(restored_messages, list) and restored_messages:
                    messages = restored_messages
                    if messages and messages[0].get("role") == "system":
                        messages[0] = {"role": "system", "content": system_prompt}
                restored_steps = restored.get("steps")
                if isinstance(restored_steps, list):
                    steps = restored_steps
                step_idx = int(restored.get("step_idx", -1))
                _log.info(
                    "agent_turn_restored | trace=%s | step_idx=%d | steps=%d | msgs=%d",
                    ctx.trace_id, step_idx, len(steps), len(messages),
                )
        while step_idx < self.max_steps - 1:
            step_idx += 1
            # 总超时保护
            elapsed = time.monotonic() - t0
            remaining = deadline_ts - time.monotonic()
            if remaining <= 3:
                _log.warning(
                    "agent_total_timeout | trace=%s | elapsed=%.1fs | limit=%.1fs",
                    ctx.trace_id,
                    elapsed,
                    total_timeout,
                )
                return await self._build_fallback_result(
                    ctx, steps, tool_calls_made, t0, "total_timeout"
                )
            raw_response: Any = None
            assistant_msg: dict[str, Any] = {}
            response_text = ""
            parsed = None
            synthetic_tool_call = False
            preflight_section = None
            if (
                strict_tool_routing
                and self.navigator_preflight_plain_text
                and step_idx == 0
                and tool_calls_made == 0
                and not steps
                and ctx.navigator_state is not None
                and normalize_text(ctx.navigator_state.active_section) == "general_chat"
            ):
                preflight_started_at = time.monotonic()
                preflight_section = await self._navigator_timeout_section_retry(
                    ctx=ctx,
                    step_idx=step_idx,
                    tool_calls_made=tool_calls_made,
                    steps=steps,
                    remaining=min(remaining, chain_deadline_ts - time.monotonic()),
                    budget_cap=self.navigator_preflight_timeout_seconds,
                )
                preflight_elapsed = time.monotonic() - preflight_started_at
            if preflight_section:
                if preflight_section[2]:
                    ctx.navigator_pending_tool_retry = (
                        preflight_section[2],
                        dict(preflight_section[3] or {}),
                    )
                parsed = {
                    "tool": NAVIGATE_SECTION_TOOL,
                    "args": {
                        "section_id": preflight_section[0],
                        "reason": preflight_section[1],
                    },
                }
                response_text = json.dumps(parsed, ensure_ascii=False)
                assistant_msg = {"role": "assistant", "content": response_text}
                synthetic_tool_call = True
                _log.info(
                    "navigator_preflight_section | trace=%s | step=%d | section=%s"
                    " | elapsed=%.1fs | reason=%s",
                    ctx.trace_id,
                    step_idx,
                    preflight_section[0],
                    preflight_elapsed,
                    clip_text(preflight_section[1], 120),
                )
            else:
                # 调用 LLM（带重试，agent loop 是关键路径）
                llm_budget = float(self.llm_step_timeout_seconds)
                if tool_calls_made > 0:
                    llm_budget = max(
                        llm_budget, float(self.llm_step_timeout_seconds_after_tool)
                    )
                llm_timeout = min(llm_budget, max(6.0, remaining - 1.5))
                if tool_calls_made == 0:
                    # 第一步还归 preflight→主调用→retry 这条链管：把已经花掉的
                    # preflight 时间从链预算里扣掉，主调用不能独立再要满 45 秒。
                    chain_left = chain_deadline_ts - time.monotonic()
                    # 链预算再紧也要给主调用留点时间，否则等于取消它 ——
                    # 一次都没试比超时更糟。但这个地板**不能反过来抬高**
                    # 已经算出来的 llm_timeout（配置故意给小预算时要听配置），
                    # 所以地板本身也被 llm_timeout 夹住。
                    floor = min(6.0, llm_timeout)
                    llm_timeout = max(floor, min(llm_timeout, chain_left))
                # 分区已经有明确证据时，不等满 llm_budget：早点超时进小 prompt 重试，
                # 由模型在该分区的真实工具里挑一个，而不是本地 if-链替它挑。
                if (
                    strict_tool_routing
                    and tool_calls_made == 0
                    and self.navigator_obvious_tool_timeout_seconds > 0
                    and llm_timeout > self.navigator_obvious_tool_timeout_seconds
                    and (
                        not steps
                        or self._has_only_navigator_tool_policy_blocks(steps)
                    )
                    and self._has_navigator_section_evidence(ctx)
                ):
                    llm_timeout = self.navigator_obvious_tool_timeout_seconds
                    _log.info(
                        "navigator_obvious_tool_timeout_cap | trace=%s | step=%d | section=%s | evidence=%s | timeout=%.1fs",
                        ctx.trace_id,
                        step_idx,
                        ctx.navigator_state.active_section if ctx.navigator_state else "",
                        ",".join((ctx.navigator_state.evidence if ctx.navigator_state else [])[:6]),
                        llm_timeout,
                    )
                schemas: list[dict[str, Any]] = []
                if native_tool_calling and ctx.native_tools:
                    schemas = self.tool_registry.get_schemas_for_native_tools(
                        ctx.native_tools
                    )

                # 每步 LLM 调用计时。此前整条链路只有 agent_total_timeout 记总耗时，
                # 单步耗时无处可查 —— 实测回合 p50 31s / p90 59s（2026-08-06，16 个
                # 真实回合），但「慢在 provider 还是慢在步数」当时只能靠 31s÷3步 猜。
                # 没有这条日志就没法判断优化该往哪走。
                llm_started_at = time.monotonic()
                try:
                    if native_tool_calling:
                        raw_response = await asyncio.wait_for(
                            self.model_client.chat_completion_with_retry(
                                messages,
                                max_tokens=self.max_tokens,
                                tools=schemas if schemas else None,
                                retries=1,
                                backoff=1.0,
                            ),
                            timeout=llm_timeout,
                        )
                    else:
                        raw_response = await asyncio.wait_for(
                            self.model_client.chat_text_with_retry(
                                messages,
                                max_tokens=self.max_tokens,
                                retries=1,
                                backoff=1.0,
                            ),
                            timeout=llm_timeout,
                        )
                    _log.info(
                        "agent_llm_step_latency | trace=%s | step=%d | elapsed=%.1fs "
                        "| tools=%d | native=%s | outcome=ok",
                        ctx.trace_id,
                        step_idx,
                        time.monotonic() - llm_started_at,
                        len(schemas),
                        native_tool_calling,
                    )
                except asyncio.TimeoutError:
                    # 这条 latency 日志此前**只在成功分支**记录，超时样本被系统性
                    # 排除：实测 n=108 max=39.8s 让人得出「45s 阈值很安全」，同期
                    # 却真有 7 次 45s 超时。少了这条，下一个人会再次读出假结论。
                    _log.info(
                        "agent_llm_step_latency | trace=%s | step=%d | elapsed=%.1fs "
                        "| tools=%d | native=%s | outcome=timeout",
                        ctx.trace_id,
                        step_idx,
                        time.monotonic() - llm_started_at,
                        len(schemas),
                        native_tool_calling,
                    )
                    _log.warning(
                        "agent_llm_timeout | trace=%s | step=%d | timeout=%.1fs "
                        "| elapsed=%.1fs",
                        ctx.trace_id,
                        step_idx,
                        llm_timeout,
                        time.monotonic() - llm_started_at,
                    )
                    retry_tool = self._consume_navigator_pending_tool_retry(ctx)
                    if retry_tool:
                        _log.info(
                            "navigator_pending_tool_retry | trace=%s | step=%d | section=%s | tool=%s",
                            ctx.trace_id,
                            step_idx,
                            ctx.navigator_state.active_section if ctx.navigator_state else "",
                            retry_tool[0],
                        )
                    else:
                        # 主 LLM 刚刚超时，同一个 provider 上再要 20 秒只是把
                        # 用户的等待翻倍。这一段单独一档小预算，并且仍受
                        # 整条链的墙钟上限约束。
                        retry_tool = await self._navigator_timeout_tool_retry(
                            ctx=ctx,
                            step_idx=step_idx,
                            tool_calls_made=tool_calls_made,
                            steps=steps,
                            remaining=min(
                                remaining, chain_deadline_ts - time.monotonic()
                            ),
                            budget_cap=self.navigator_post_timeout_retry_seconds,
                        )
                    if retry_tool:
                        tool_name_retry, tool_args_retry = retry_tool
                        parsed = {"tool": tool_name_retry, "args": dict(tool_args_retry)}
                        response_text = json.dumps(parsed, ensure_ascii=False)
                        assistant_msg = {
                            "role": "assistant",
                            "content": response_text,
                        }
                        synthetic_tool_call = True
                        _log.info(
                            "navigator_timeout_tool_retry | trace=%s | step=%d | section=%s | tool=%s",
                            ctx.trace_id,
                            step_idx,
                            ctx.navigator_state.active_section if ctx.navigator_state else "",
                            tool_name_retry,
                        )
                    retry_section = None
                    if not synthetic_tool_call:
                        retry_section = await self._navigator_timeout_section_retry(
                            ctx=ctx,
                            step_idx=step_idx,
                            tool_calls_made=tool_calls_made,
                            steps=steps,
                            remaining=min(
                                remaining, chain_deadline_ts - time.monotonic()
                            ),
                            budget_cap=self.navigator_post_timeout_retry_seconds,
                        )
                    if retry_section:
                        if retry_section[2]:
                            ctx.navigator_pending_tool_retry = (
                                retry_section[2],
                                dict(retry_section[3] or {}),
                            )
                        parsed = {
                            "tool": NAVIGATE_SECTION_TOOL,
                            "args": {
                                "section_id": retry_section[0],
                                "reason": retry_section[1],
                            },
                        }
                        response_text = json.dumps(parsed, ensure_ascii=False)
                        assistant_msg = {
                            "role": "assistant",
                            "content": response_text,
                        }
                        synthetic_tool_call = True
                        _log.info(
                            "navigator_timeout_section_retry | trace=%s | step=%d | section=%s | reason=%s",
                            ctx.trace_id,
                            step_idx,
                            retry_section[0],
                            clip_text(retry_section[1], 120),
                        )
                    elif not synthetic_tool_call and steps:
                        return await self._build_fallback_result(
                            ctx, steps, tool_calls_made, t0, "llm_timeout"
                        )
                    elif not synthetic_tool_call:
                        if not self._is_directed_turn(ctx):
                            return self._undirected_silent_result(
                                ctx, steps, tool_calls_made, t0, "llm_timeout"
                            )
                        fallback = _pl.get_message(
                            "llm_timeout_fallback",
                            "我这边处理超时了。你可以把问题再精简一点，我马上继续。",
                        )
                        return AgentResult(
                            reply_text=fallback,
                            action="reply",
                            reason="agent_llm_timeout",
                            total_time_ms=self._elapsed(t0),
                        )
                    if not synthetic_tool_call and parsed is None:
                        if steps:
                            return await self._build_fallback_result(
                                ctx, steps, tool_calls_made, t0, "llm_timeout"
                            )
                        if not self._is_directed_turn(ctx):
                            return self._undirected_silent_result(
                                ctx, steps, tool_calls_made, t0, "llm_timeout"
                            )
                        fallback = _pl.get_message(
                            "llm_timeout_fallback",
                            "我这边处理超时了。你可以把问题再精简一点，我马上继续。",
                        )
                        return AgentResult(
                            reply_text=fallback,
                            action="reply",
                            reason="agent_llm_timeout",
                            total_time_ms=self._elapsed(t0),
                        )
                except Exception as exc:
                    _log.warning(
                        "agent_llm_error | trace=%s | step=%d | %s",
                        ctx.trace_id,
                        step_idx,
                        exc,
                    )
                    # LLM 报错时不再本地挑工具兜底：如果上一轮 section retry 已经让模型
                    # 指定了下一个工具，用它；否则把错误如实交给上层，不发明动作。
                    retry_tool = self._consume_navigator_pending_tool_retry(ctx)
                    if retry_tool:
                        tool_name_retry, tool_args_retry = retry_tool
                        parsed = {"tool": tool_name_retry, "args": dict(tool_args_retry)}
                        response_text = json.dumps(parsed, ensure_ascii=False)
                        assistant_msg = {"role": "assistant", "content": response_text}
                        synthetic_tool_call = True
                        _log.info(
                            "navigator_llm_error_pending_tool_retry | trace=%s | step=%d | section=%s | tool=%s",
                            ctx.trace_id,
                            step_idx,
                            ctx.navigator_state.active_section if ctx.navigator_state else "",
                            tool_name_retry,
                        )
                    elif steps:
                        return await self._build_fallback_result(
                            ctx, steps, tool_calls_made, t0, "llm_error"
                        )
                    else:
                        # 旁听探测轮：没人跟机器人说话，内部故障文案不进群。
                        # 原先这条静默还要 `allow_silent_on_llm_error`（默认 False）
                        # 才生效，于是默认配置下群友闲聊也会收到一句错误文案。
                        if not self._is_directed_turn(ctx):
                            return self._undirected_silent_result(
                                ctx, steps, tool_calls_made, t0, "llm_error"
                            )
                        if (
                            self.allow_silent_on_llm_error
                            and not ctx.mentioned
                            and not ctx.is_private
                        ):
                            return AgentResult(
                                reply_text="",
                                action="reply",
                                reason="agent_llm_error_silent",
                                total_time_ms=self._elapsed(t0),
                            )
                        err_text = normalize_text(str(exc)).lower()
                        if (
                            "http 401" in err_text
                            or "invalid token" in err_text
                            or "unauthorized" in err_text
                            or "无效的令牌" in err_text
                            or "认证失败" in err_text
                        ):
                            fallback = _pl.get_message(
                                "llm_auth_error_fallback",
                                "AI 服务鉴权失败（令牌无效/过期），请管理员检查 API Key 后重试。",
                            )
                        else:
                            fallback = _pl.get_message(
                                "llm_error_fallback",
                                _pl.get_message(
                                    "generic_error", "我这边接口抖了，稍等我再试一次。"
                                ),
                            )
                        return AgentResult(
                            reply_text=fallback,
                            action="reply",
                            reason="agent_llm_error",
                            total_time_ms=self._elapsed(t0),
                        )
            if not synthetic_tool_call and native_tool_calling:
                assistant_msg = (
                    raw_response.get("choices", [{}])[0].get("message", {})
                    if isinstance(raw_response, dict)
                    else {}
                )
                response_text = normalize_text(assistant_msg.get("content", ""))
                tool_calls = assistant_msg.get("tool_calls", [])
                if tool_calls:
                    for tc in tool_calls:
                        if tc.get("type") == "function":
                            func = tc.get("function", {})
                            args = self._decode_tool_call_arguments(
                                func.get("arguments"),
                                tool_name=str(func.get("name") or ""),
                                trace_id=ctx.trace_id,
                                step_idx=step_idx,
                            )
                            # 把修正后的参数写回 assistant_msg —— 它会原样追加进
                            # messages 送回 provider。留着畸形串（实测 skiapi 返回
                            # `{}{"url": ...}`）会让下一轮解析成空参数，模型于是认为
                            # 「我上一轮没带参数」并重复调用同一工具，直到熔断。
                            # 实测：畸形串 → 模型重复调 parse_video；修正后 → final_answer。
                            self._rewrite_tool_call_arguments(tc, args)
                            parsed = {
                                "tool": func.get("name"),
                                "args": args,
                                "id": tc.get("id"),
                            }
                            # 仅处理第一个 tool_call，后续的被丢弃（记录日志）
                            remaining_tcs = [
                                t for t in tool_calls
                                if t.get("type") == "function" and t is not tc
                            ]
                            if remaining_tcs:
                                _log.info(
                                    "agent_dropped_parallel_tool_calls | trace=%s | step=%d | dropped=%d | names=%s",
                                    ctx.trace_id,
                                    step_idx,
                                    len(remaining_tcs),
                                    ",".join(
                                        str(t.get("function", {}).get("name", "?"))
                                        for t in remaining_tcs[:3]
                                    ),
                                )
                            break

                if parsed is None:
                    # Fallback to parse text just in case model ignores native tools
                    parsed = self._parse_llm_output(response_text)
            elif not synthetic_tool_call:
                response_text = normalize_text(raw_response)
                if not response_text:
                    break
                parsed = self._parse_llm_output(response_text)
                
            if parsed is None:
                if not response_text:
                    break
                # 无法解析为 tool_call
                # 安全检查：如果内容看起来像 JSON，不要当作回复发出去
                if response_text.strip().startswith("{"):
                    _log.warning(
                        "agent_unparseable_json | trace=%s | step=%d",
                        ctx.trace_id,
                        step_idx,
                    )
                    break
                if (
                    strict_tool_routing
                    and not strict_tool_policy_blocked
                    and self._requires_tool_review_before_final(ctx)
                ):
                    strict_tool_policy_blocked = True
                    _log.info(
                        "navigator_tool_policy_block | trace=%s | step=%d | reason=direct_text_without_tool | evidence=%s",
                        ctx.trace_id,
                        step_idx,
                        ",".join((ctx.navigator_state.evidence if ctx.navigator_state else [])[:6]),
                    )
                    steps.append(
                        {
                            "step": step_idx,
                            "tool": "policy_guard",
                            "error": "navigator_tool_required_before_direct_reply",
                        }
                    )
                    self._append_tool_result(
                        messages,
                        parsed,
                        assistant_msg,
                        response_text,
                        {
                            "tool": "policy_guard",
                            "ok": False,
                            "error": "当前消息带有结构化链接/媒体/待投递 artifact，不能直接自然语言作答。请先在当前 Prompt Navigator 分区调用最合适的工具；如果分区不对，先 navigate_section。",
                            "display": "请先调用当前分区工具完成处理，再 final_answer。",
                        },
                    )
                    continue
                _log.info(
                    "agent_direct_reply | trace=%s | step=%d", ctx.trace_id, step_idx
                )
                return AgentResult(
                    reply_text=response_text,
                    action="reply",
                    reason="agent_direct_reply",
                    tool_calls_made=tool_calls_made,
                    total_time_ms=self._elapsed(t0),
                    steps=steps,
                )

            tool_name = parsed.get("tool", "")
            tool_args = parsed.get("args", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
            tool_args = self._normalize_tool_args(tool_name, tool_args, ctx)
            if tool_name == NAVIGATE_SECTION_TOOL:
                ok, nav_result = self._handle_navigate_section_tool(ctx, tool_args)
                steps.append(
                    {
                        "step": step_idx,
                        "tool": NAVIGATE_SECTION_TOOL,
                        "ok": ok,
                        "display": clip_text(str(nav_result.get("display", "")), 300),
                        "error": str(nav_result.get("error", "")),
                    }
                )
                self._append_tool_result(
                    messages,
                    parsed,
                    assistant_msg,
                    response_text,
                    nav_result,
                )
                continue
            missing_args = self._missing_required_tool_args(tool_name, tool_args)
            log_tool_args = tool_args
            if tool_name == "final_answer" and isinstance(tool_args, dict):
                log_tool_args = dict(tool_args)
                log_video_url = normalize_text(
                    str(log_tool_args.get("video_url", "") or self._last_success_video_url(steps))
                )
                if log_video_url:
                    log_tool_args["text"] = self._sanitize_final_text_for_local_media(
                        str(log_tool_args.get("text", "")),
                        log_video_url,
                    )
                    if self._is_local_media_path(log_video_url) and "video_url" in log_tool_args:
                        log_tool_args["video_url"] = "[local_media_artifact]"

            _log.info(
                "agent_tool_call | trace=%s | step=%d | tool=%s | args=%s",
                ctx.trace_id,
                step_idx,
                tool_name,
                self._truncate_tool_args_for_log(log_tool_args),
            )

            if missing_args:
                miss_text = ", ".join(missing_args)
                miss_key = f"{tool_name}:{'|'.join(sorted(missing_args))}"
                missing_arg_counts[miss_key] = missing_arg_counts.get(miss_key, 0) + 1
                steps.append(
                    {
                        "step": step_idx,
                        "tool": tool_name,
                        "ok": False,
                        "error": f"missing_required_args:{miss_text}",
                    }
                )
                if missing_arg_counts[miss_key] >= 3:
                    # 这里过去把工具名和参数名原样喂给兜底 LLM，模型会照抄进群。
                    # 只传类别标签。
                    fallback_text = await self._ai_fallback_reply(
                        ctx, self._build_failure_situation_hint(["missing_args"])
                    )
                    return AgentResult(
                        reply_text=fallback_text
                        or "我先停一下，当前这步参数一直不完整。你补一句更具体的目标，我立刻继续。",
                        action="reply",
                        reason="agent_missing_args_loop_break",
                        tool_calls_made=tool_calls_made,
                        total_time_ms=self._elapsed(t0),
                        steps=steps,
                    )
                miss_hints = " ".join(
                    _MISSING_ARG_HINTS[name]
                    for name in missing_args
                    if name in _MISSING_ARG_HINTS
                )
                self._append_tool_result(messages, parsed, assistant_msg, response_text, {
                                    "tool": tool_name,
                                    "ok": False,
                                    "error": f"工具 {tool_name} 缺少必填参数: {miss_text}",
                                    "display": (
                                        f"{tool_name} 缺少参数({miss_text})，请补全后重试。"
                                        + (f" {miss_hints}" if miss_hints else "")
                                    ),
                                })
                continue

            # final_answer 特殊处理 — 直接返回
            if tool_name == "final_answer":
                text = str(tool_args.get("text", "")).strip()
                image_url = str(tool_args.get("image_url", "")).strip()
                raw_image_urls = tool_args.get("image_urls", [])
                image_urls: list[str] = []
                if isinstance(raw_image_urls, list):
                    image_urls = [
                        str(u).strip() for u in raw_image_urls if str(u).strip()
                    ]
                if image_url and image_url not in image_urls:
                    image_urls.insert(0, image_url)
                if image_urls and not image_url:
                    image_url = image_urls[0]
                if not image_url and not image_urls:
                    image_urls = self._last_success_image_urls(steps)
                    image_url = image_urls[0] if image_urls else ""
                video_url = str(tool_args.get("video_url", "")).strip()
                audio_file = str(tool_args.get("audio_file", "")).strip()
                cover_url = str(tool_args.get("cover_url", "")).strip()
                if audio_file.lower().endswith(".silk"):
                    preferred_audio = self._last_success_audio_file(
                        steps, prefer_non_silk=True
                    )
                    if preferred_audio:
                        _log.info(
                            "agent_audio_file_override | trace=%s | step=%d | from=%s | to=%s",
                            ctx.trace_id,
                            step_idx,
                            clip_text(audio_file, 120),
                            clip_text(preferred_audio, 120),
                        )
                        audio_file = preferred_audio
                if not audio_file:
                    audio_file = self._last_success_audio_file(steps)
                if not video_url:
                    video_url = self._last_success_video_url(steps)
                if video_url:
                    text = self._sanitize_final_text_for_local_media(text, video_url)
                # 模型显式声明这是拒绝时，不要逼它先调工具。
                #
                # 实测（2026-08-06，trace 118886-5-f46539ff）：用户带链接要盗版软件，
                # 模型 step 0 就正确拒绝了「破解版这种东西涉及侵权，我这边不去搜也不会推」，
                # 这道门以 evidence=url 驳回 → 模型只好 web_search 了它刚说不搜的东西
                # → step 2 再答一遍。**拒绝被这道门推翻，而且多烧两次 LLM 往返（36.7s）。**
                #
                # 这道门本身要保留：它防的是「有链接却说我看不到」。但拒绝不是
                # 「用嘴代替动手」—— 拒绝时本来就不该动手。
                declined_request = bool(tool_args.get("declined"))
                if declined_request:
                    _log.info(
                        "agent_final_answer_declined | trace=%s | step=%d | "
                        "跳过 tool_review 门（模型声明这是拒绝）",
                        ctx.trace_id,
                        step_idx,
                    )
                if (
                    strict_tool_routing
                    and tool_calls_made == 0
                    and not strict_tool_policy_blocked
                    and not declined_request
                    and self._requires_tool_review_before_final(ctx)
                ):
                    strict_tool_policy_blocked = True
                    _log.info(
                        "navigator_tool_policy_block | trace=%s | step=%d | reason=final_answer_without_tool | evidence=%s",
                        ctx.trace_id,
                        step_idx,
                        ",".join((ctx.navigator_state.evidence if ctx.navigator_state else [])[:6]),
                    )
                    steps.append(
                        {
                            "step": step_idx,
                            "tool": "policy_guard",
                            "error": "navigator_tool_required_before_final_answer",
                        }
                    )
                    self._append_tool_result(
                        messages,
                        parsed,
                        assistant_msg,
                        response_text,
                        {
                            "tool": "policy_guard",
                            "ok": False,
                            "error": "当前消息带有结构化链接/媒体/待投递 artifact，不能直接 final_answer。请先调用当前 Prompt Navigator 分区中的真实工具；如果分区不对，先 navigate_section。",
                            "display": "请先调用当前分区工具完成处理，再 final_answer。",
                        },
                    )
                    continue
                # 防止工具 JSON 泄漏给用户
                if text.startswith("{") and text.endswith("}"):
                    try:
                        maybe_json = json.loads(text)
                        if isinstance(maybe_json, dict):
                            text = _pl.get_message(
                                "tool_payload_leaked",
                                "检测到模型输出了工具调用格式，我已自动重试处理。",
                            )
                    except (json.JSONDecodeError, ValueError):
                        pass
                # 禁止占位/伪造媒体链接直接落地，强制模型回到工具链拿真实可发送 URL。
                invalid_media_urls: list[str] = []
                for candidate in [image_url, *image_urls, video_url, audio_file]:
                    if self._is_placeholder_media_url(candidate):
                        invalid_media_urls.append(candidate)
                if invalid_media_urls:
                    steps.append(
                        {
                            "step": step_idx,
                            "tool": "policy_guard",
                            "error": "invalid_media_url_placeholder",
                        }
                    )
                    self._append_tool_result(messages, parsed, assistant_msg, response_text, {
                                        "tool": "policy_guard",
                                        "ok": False,
                                        "error": "final_answer 里出现了占位媒体链接（如 example.com）。请先调用工具获取真实 URL 再 final_answer。",
                                    })
                    continue
                media_candidates = [
                    normalize_text(url)
                    for url in [image_url, *image_urls, video_url, audio_file]
                    if normalize_text(url)
                ]
                if media_candidates:
                    known_media_urls = self._collect_known_media_urls(
                        steps=steps, ctx=ctx
                    )
                    known_local_media_paths = self._collect_known_local_media_paths(
                        steps=steps, ctx=ctx
                    )
                    out_of_chain_urls: list[str] = []
                    for candidate in media_candidates:
                        if self._is_local_media_path(candidate):
                            local_norm = self._normalize_local_media_path(candidate)
                            if (
                                not local_norm
                                or local_norm not in known_local_media_paths
                            ):
                                out_of_chain_urls.append(candidate)
                            continue
                        if not self._url_matches_known_media(
                            candidate, known_media_urls
                        ):
                            out_of_chain_urls.append(candidate)
                    if out_of_chain_urls:
                        dropped = {
                            normalize_text(item)
                            for item in out_of_chain_urls
                            if normalize_text(item)
                        }
                        if dropped:
                            if image_url and normalize_text(image_url) in dropped:
                                image_url = ""
                            image_urls = [
                                u
                                for u in image_urls
                                if normalize_text(u) not in dropped
                            ]
                            if video_url and normalize_text(video_url) in dropped:
                                video_url = ""
                            if audio_file and normalize_text(audio_file) in dropped:
                                audio_file = ""
                            if image_urls and not image_url:
                                image_url = image_urls[0]
                        if text or image_url or image_urls or video_url or audio_file:
                            _log.info(
                                "agent_strip_out_of_chain_media | trace=%s | step=%d | dropped=%d",
                                ctx.trace_id,
                                step_idx,
                                len(out_of_chain_urls),
                            )
                        else:
                            steps.append(
                                {
                                    "step": step_idx,
                                    "tool": "policy_guard",
                                    "error": "media_url_not_from_tool_chain",
                                }
                            )
                            self._append_tool_result(
                                messages,
                                parsed,
                                assistant_msg,
                                response_text,
                                {
                                    "tool": "policy_guard",
                                    "ok": False,
                                    "error": "final_answer 的媒体链接必须来自本轮工具结果或用户原始消息。请先调用工具获取真实可发送链接，再 final_answer。",
                                }
                            )
                            continue
                # 空 final_answer 的归属判定。
                #
                # 「要不要在这个群里说话」是模型的决定，表达方式就是交一个空
                # final_answer（CLAUDE.md）。原先这里额外要求 `has_thought`、
                # `not ctx.mentioned`、`not ctx.is_private`、`len(msg) <= 4`
                # 四个条件才认这次沉默 —— 这四个都跟「模型想不想沉默」无关。
                # 实测（2026-08-06 真实群日志）239 条 final_answer 里 60 条 text 为空，
                # 而 agent_intentional_silence 全天只有 5 次：至少 55 次模型选择的沉默
                # 被改写成一句道歉发进群。
                #
                # 唯一该看的结构事实是：本回合有没有真的工具失败。
                # 有失败步 → 空 final_answer 可能是「工具挂了没话说」→ 走兜底；
                # 无失败步（含零工具调用）→ 空 final_answer 就是模型选择沉默 → 保持空。
                failed_steps = self._real_tool_failure_count(steps)
                intentional_silence = (
                    not text
                    and not image_url
                    and not video_url
                    and not audio_file
                    and failed_steps == 0
                )
                if tool_name == "final_answer":
                    # 某些模型会把真正的工具调用 JSON 包在 final_answer.text 里，尝试恢复。
                    recovered = None
                    if text and not image_url and not video_url and not audio_file:
                        recovered = self._extract_embedded_tool_call_from_text(text)
                    if recovered:
                        recovered_tool = str(recovered.get("tool", "")).strip()
                        recovered_args = recovered.get("args", {})
                        if recovered_tool == "final_answer":
                            recovered_text = ""
                            if isinstance(recovered_args, dict):
                                recovered_text = normalize_text(
                                    str(recovered_args.get("text", ""))
                                )
                            if recovered_text:
                                _log.info(
                                    "agent_final_answer_embedded_final_unwrapped | trace=%s | step=%d",
                                    ctx.trace_id,
                                    step_idx,
                                )
                                text = recovered_text
                        elif recovered_tool and self.tool_registry.has_tool(recovered_tool):
                            _log.warning(
                                "agent_final_answer_embedded_tool_recovered | trace=%s | step=%d | tool=%s",
                                ctx.trace_id,
                                step_idx,
                                recovered_tool,
                            )
                            tool_name = recovered_tool
                            tool_args = (
                                recovered_args if isinstance(recovered_args, dict) else {}
                            )
                        else:
                            text = _pl.get_message(
                                "tool_payload_leaked",
                                "检测到模型输出了工具调用格式，我已自动重试处理。",
                            )
                    elif self._looks_like_embedded_tool_payload_text(text):
                        _log.warning(
                            "agent_final_answer_embedded_tool_payload_blocked | trace=%s | step=%d",
                            ctx.trace_id,
                            step_idx,
                        )
                        text = _pl.get_message(
                            "tool_payload_leaked",
                            "检测到模型输出了工具调用格式，我已自动拦截。",
                        )
                if tool_name == "final_answer":
                    if not text and not image_url and not video_url and not audio_file:
                        if intentional_silence:
                            _log.info(
                                "agent_intentional_silence | trace=%s | step=%d | steps=%d",
                                ctx.trace_id,
                                step_idx,
                                len(steps),
                            )
                        else:
                            # 有真失败步才允许代码替模型造话。记一条日志，
                            # 让「模型沉默」和「代码造话」以后可以分开聚合。
                            _log.info(
                                "agent_fallback_over_empty_final | trace=%s | failed_steps=%d",
                                ctx.trace_id,
                                failed_steps,
                            )
                            text = self._scrub_internal_state_text(
                                self._last_success_display(steps)
                            )
                            if not text:
                                text = await self._ai_fallback_reply(
                                    ctx,
                                    self._build_failure_situation_hint(["not_found"]),
                                )
                            if not text:
                                text = _pl.get_message("no_result", "")
                    text = self._normalize_final_answer_text(text)
                    steps.append(
                        {"step": step_idx, "tool": "final_answer", "result": "done"}
                    )
                    user_media_refs = self._extract_media_refs_from_segments(
                        ctx.raw_segments
                    )
                    reply_media_refs = self._extract_media_refs_from_segments(
                        ctx.reply_media_segments
                    )
                    _log.info(
                        "agent_final_answer_media_source | trace=%s | step=%d | image=%s | image_count=%d | video=%s | user_media=%d | reply_media=%d",
                        ctx.trace_id,
                        step_idx,
                        bool(image_url),
                        len(image_urls),
                        bool(video_url),
                        len(user_media_refs),
                        len(reply_media_refs),
                    )
                    # 去重：如果工具已经发送了媒体（副作用），final_answer 不再重复携带
                    if tool_sent_media:
                        if image_url and normalize_text(image_url) in tool_sent_media:
                            _log.info(
                                "agent_dedup_media | trace=%s | stripped image_url (already sent by tool)",
                                ctx.trace_id,
                            )
                            image_url = ""
                        image_urls = [
                            u
                            for u in image_urls
                            if normalize_text(u) not in tool_sent_media
                        ]
                        if video_url and normalize_text(video_url) in tool_sent_media:
                            _log.info(
                                "agent_dedup_media | trace=%s | stripped video_url (already sent by tool)",
                                ctx.trace_id,
                            )
                            video_url = ""
                    # 这里原本有一段"表情工具跑完就清空 final_answer 媒体"的逻辑，
                    # 判据是 step.get("result")，而工具步骤的键集只有
                    # step/tool/ok/display/error(/data)，"result" 只出现在
                    # final_answer 自己那条 —— 条件恒假，这段从未执行过。已删除：
                    # 它想防的重复投递已由上面的 tool_sent_media 去重覆盖，
                    # 而它的保留判据是"预览/发出来看看"关键词表，属于本仓明确
                    # 拒绝的本地语义否决层，修好它等于新上线一个反约定的行为。
                    return AgentResult(
                        reply_text=text,
                        image_url=image_url,
                        image_urls=(
                            image_urls
                            if image_urls
                            else ([image_url] if image_url else [])
                        ),
                        video_url=video_url,
                        audio_file=audio_file,
                        cover_url=cover_url,
                        action="reply",
                        reason="agent_final_answer",
                        tool_calls_made=tool_calls_made,
                        total_time_ms=self._elapsed(t0),
                        steps=steps,
                    )

            # think 工具 — 不算真正的工具调用
            if tool_name == "think":
                consecutive_think_count += 1
                if consecutive_think_count >= self.max_consecutive_think:
                    steps.append(
                        {
                            "step": step_idx,
                            "tool": "think",
                            "ok": False,
                            "error": "too_many_consecutive_think",
                        }
                    )
                    if consecutive_think_count >= self.max_consecutive_think + 2:
                        # 「连续思考次数过多／没有执行有效工具」是内部状态，
                        # 喂进去模型就会照抄。只给类别。
                        fallback_text = await self._ai_fallback_reply(
                            ctx, self._build_failure_situation_hint(["unknown"])
                        )
                        return AgentResult(
                            reply_text=fallback_text
                            or "我不绕圈了：你再说得具体一点，我直接执行。",
                            action="reply",
                            reason="agent_think_loop_break",
                            tool_calls_made=tool_calls_made,
                            total_time_ms=self._elapsed(t0),
                            steps=steps,
                        )
                    self._append_tool_result(messages, parsed, assistant_msg, response_text, {
                                        "tool": "think",
                                        "ok": False,
                                        "error": "think 连续过多，请直接调用具体工具或 final_answer。",
                                    })
                    continue
                thought = str(tool_args.get("thought", ""))
                steps.append(
                    {
                        "step": step_idx,
                        "tool": "think",
                        "thought": clip_text(thought, 200),
                    }
                )
                self._append_tool_result(messages, parsed, assistant_msg, response_text, {
                                    "tool": "think",
                                    "ok": True,
                                    "display": _pl.get_message(
                                        "think_done", "思考完成，请继续"
                                    ),
                                })
                continue
            else:
                consecutive_think_count = 0

            # 安全检查: 三级权限
            perm_level = self._resolve_permission_level(ctx)
            if tool_name in self._super_admin_tools and perm_level != "super_admin":
                steps.append(
                    {"step": step_idx, "tool": tool_name, "blocked": "need_super_admin"}
                )
                self._append_tool_result(messages, parsed, assistant_msg, response_text, {
                                    "tool": tool_name,
                                    "ok": False,
                                    "error": "权限不足，该操作仅超级管理员可执行",
                                })
                continue
            if tool_name in self._group_admin_tools and perm_level not in (
                "super_admin",
                "group_admin",
            ):
                if not self._is_regular_user_self_ban_attempt(ctx, tool_name, tool_args):
                    steps.append(
                        {"step": step_idx, "tool": tool_name, "blocked": "need_group_admin"}
                    )
                    self._append_tool_result(messages, parsed, assistant_msg, response_text, {
                                        "tool": tool_name,
                                        "ok": False,
                                        "error": "权限不足，该操作需要群管理员或超级管理员权限",
                                    })
                    continue
            if (
                tool_name in self._group_admin_tools
                and tool_name != "delete_message"
                and not self._is_explicit_bot_addressed(ctx)
            ):
                steps.append(
                    {
                        "step": step_idx,
                        "tool": tool_name,
                        "blocked": "explicit_bot_address_required",
                    }
                )
                self._append_tool_result(messages, parsed, assistant_msg, response_text, {
                                    "tool": tool_name,
                                    "ok": False,
                                    "error": "执行群管理操作前，需要明确点名机器人（@我或直接叫YUKI）",
                                })
                continue

            # 检查工具是否存在
            if not self.tool_registry.has_tool(tool_name):
                steps.append(
                    {"step": step_idx, "tool": tool_name, "error": "unknown_tool"}
                )
                self._append_tool_result(messages, parsed, assistant_msg, response_text, {
                                    "tool": tool_name,
                                    "ok": False,
                                    "error": f"工具 {tool_name} 不存在，请检查工具名",
                                })
                continue

            high_risk_guard_reply = self._guard_high_risk_tool_call(
                ctx=ctx,
                tool_name=tool_name,
                tool_args=tool_args,
            )
            if high_risk_guard_reply:
                steps.append(
                    {
                        "step": step_idx,
                        "tool": tool_name,
                        "blocked": "high_risk_confirmation_required",
                    }
                )
                return AgentResult(
                    reply_text=high_risk_guard_reply,
                    action="reply",
                    reason="agent_high_risk_guard",
                    tool_calls_made=tool_calls_made,
                    total_time_ms=self._elapsed(t0),
                    steps=steps,
                )

            # validate-then-repair：先做通用修复（P0/P2，透明标记），再走猜参兜底。
            tool_schema_obj = (
                self.tool_registry.get_schema(tool_name)
                if hasattr(self.tool_registry, "get_schema")
                else None
            )
            tool_schema = getattr(tool_schema_obj, "parameters", None) if tool_schema_obj is not None else None
            tool_args, repairs = repair_tool_call(tool_args, schema=tool_schema, tool_name=tool_name)
            if repairs:
                _log.info(
                    "agent_tool_args_repaired | trace=%s | step=%d | tool=%s | repairs=%s",
                    ctx.trace_id, step_idx, tool_name, ",".join(repairs),
                )
            # 自动补全缺失参数
            tool_args = self._normalize_tool_args(tool_name, tool_args, ctx)
            # PreToolUse 审批钩子：任一钩子返回非空说明即阻止该工具。
            pre_tool_block = self._run_pre_tool_hooks(ctx, tool_name, tool_args)
            if pre_tool_block:
                steps.append(
                    {
                        "step": step_idx,
                        "tool": tool_name,
                        "ok": False,
                        "error": "pre_tool_hook_block",
                    }
                )
                guard_payload = self._build_guard_feedback_payload(
                    tool_name=tool_name,
                    steps=steps,
                    reason_key="pre_tool_hook_block",
                    reason_text=clip_text(pre_tool_block, 500),
                )
                _log.info(
                    "agent_pre_tool_hook_block | trace=%s | step=%d | tool=%s",
                    ctx.trace_id, step_idx, tool_name,
                )
                self._append_tool_result(
                    messages, parsed, assistant_msg, response_text, guard_payload
                )
                continue
            tool_signature = f"{tool_name}|{self._build_args_signature(tool_args)}"
            # OpenClaw 式实时 loop 检测：同参数同结果连续空转，warn 记日志、critical/circuit 阻断。
            loop_level, loop_streak = loop_guard.veto_if_looping(
                tool_name, hash_call(tool_name, tool_args)
            )
            if loop_level in ("critical", "circuit"):
                steps.append(
                    {
                        "step": step_idx,
                        "tool": tool_name,
                        "ok": False,
                        "error": f"loop_guard:{loop_level}:{loop_streak}",
                    }
                )
                guard_payload = self._build_guard_feedback_payload(
                    tool_name=tool_name,
                    steps=steps,
                    reason_key="loop_guard_loop",
                    reason_text=(
                        "检测到同一工具和参数连续空转多次，结果没有进展。"
                        "请换一个工具或直接 final_answer，不要重复相同调用。"
                    ),
                )
                _log.info(
                    "agent_loop_guard | trace=%s | step=%d | tool=%s | level=%s | streak=%d",
                    ctx.trace_id, step_idx, tool_name, loop_level, loop_streak,
                )
                self._append_tool_result(
                    messages, parsed, assistant_msg, response_text, guard_payload
                )
                continue
            if loop_level == "warn":
                _log.warning(
                    "agent_loop_guard_warn | trace=%s | step=%d | tool=%s | streak=%d",
                    ctx.trace_id, step_idx, tool_name, loop_streak,
                )
            if self.repeat_tool_guard_enable:
                repeated_tool_counts[tool_signature] = (
                    repeated_tool_counts.get(tool_signature, 0) + 1
                )
                repeat_count = repeated_tool_counts[tool_signature]
                # 有对外副作用的工具，同 args 只允许真执行成功一次。
                # 数成功次数而不是调用次数：第一次瞬时失败（超时/网络抖动）后，
                # 同 args 重试必须放行，否则这条消息本回合再也发不出去。
                # 没成功过时退回通用上限 max_same_tool_call，仍然防死循环。
                repeat_limit = (
                    1
                    if tool_name in self._ONCE_PER_TURN_TOOLS
                    and tool_signature in succeeded_tool_signatures
                    else self.max_same_tool_call
                )
                if repeat_count > repeat_limit:
                    steps.append(
                        {
                            "step": step_idx,
                            "tool": tool_name,
                            "ok": False,
                            "error": f"repeated_tool_call:{repeat_count}",
                        }
                    )
                    guard_payload = self._build_guard_feedback_payload(
                        tool_name=tool_name,
                        steps=steps,
                        reason_key="repeated_tool_call",
                        reason_text="同一工具和参数重复过多，请换工具策略或直接 final_answer。",
                    )
                    _log.info(
                        "agent_guard_block | trace=%s | step=%d | tool=%s | guard=%s | repeat=%d | has_artifact=%s",
                        ctx.trace_id,
                        step_idx,
                        tool_name,
                        "repeated_tool_call",
                        repeat_count,
                        "already_obtained" in guard_payload,
                    )
                    # 熔断判定必须在 append 之前：熔断后模型再也不会被调用，
                    # 那条 tool 消息无人消费，纯浪费 token。
                    if repeat_count >= repeat_limit + 2:
                        return await self._build_fallback_result(
                            ctx, steps, tool_calls_made, t0, "repeated_tool_call"
                        )
                    self._append_tool_result(
                        messages, parsed, assistant_msg, response_text, guard_payload
                    )
                    continue

            ext_sig = ""
            if tool_name in self._EXTERNAL_FACT_TOOLS:
                ext_sig = self._build_external_fact_signature(tool_name, tool_args)
                if ext_sig and ext_sig in seen_external_fact_signatures:
                    steps.append(
                        {
                            "step": step_idx,
                            "tool": tool_name,
                            "ok": False,
                            "error": "duplicate_external_fact_query",
                        }
                    )
                    guard_payload = self._build_guard_feedback_payload(
                        tool_name=tool_name,
                        steps=steps,
                        reason_key="duplicate_external_fact_query",
                        reason_text="这个外部查询之前已经成功执行过，请基于已有结果继续。",
                    )
                    _log.info(
                        "agent_guard_block | trace=%s | step=%d | tool=%s | guard=%s | repeat=%d | has_artifact=%s",
                        ctx.trace_id,
                        step_idx,
                        tool_name,
                        "duplicate_external_fact_query",
                        0,
                        "already_obtained" in guard_payload,
                    )
                    self._append_tool_result(
                        messages, parsed, assistant_msg, response_text, guard_payload
                    )
                    continue

            # 致命级循环拦截：如果某个工具连续抛错超过 2 次，强行熔断
            if consecutive_tool_errors.get(tool_name, 0) >= 2:
                steps.append(
                    {
                        "step": step_idx,
                        "tool": tool_name,
                        "ok": False,
                        "error": "consecutive_crashes_guard",
                    }
                )
                guard_payload = self._build_guard_feedback_payload(
                    tool_name=tool_name,
                    steps=steps,
                    reason_key="consecutive_crashes_guard",
                    reason_text="该工具已连续崩溃或报错，底层拒绝执行，不要再调用它。",
                )
                _log.info(
                    "agent_guard_block | trace=%s | step=%d | tool=%s | guard=%s | repeat=%d | has_artifact=%s",
                    ctx.trace_id,
                    step_idx,
                    tool_name,
                    "consecutive_crashes_guard",
                    consecutive_tool_errors.get(tool_name, 0),
                    "already_obtained" in guard_payload,
                )
                self._append_tool_result(
                    messages, parsed, assistant_msg, response_text, guard_payload
                )
                continue

            # 执行工具
            tool_context = self._build_tool_context(ctx, perm_level)
            remaining_for_tool = deadline_ts - time.monotonic()
            if remaining_for_tool <= 3:
                return await self._build_fallback_result(
                    ctx, steps, tool_calls_made, t0, "total_timeout"
                )
            tool_timeout = min(
                self._resolve_tool_timeout_seconds(tool_name, has_media),
                max(4.0, remaining_for_tool - 1.0),
            )
            # 工具没写 display 时由循环合成一句给模型看的诊断串。它含工具名和
            # 错误码，绝不能直接外发 —— 打上标记让兜底路径认出来。
            display_synthetic = False
            try:
                result = await asyncio.wait_for(
                    self.tool_registry.call(tool_name, tool_args, tool_context),
                    timeout=tool_timeout,
                )
            except asyncio.TimeoutError:
                result = ToolCallResult(
                    ok=False,
                    display=f"{tool_name} 执行超时（>{int(tool_timeout)}s）",
                    error=f"tool_timeout:{tool_name}",
                    data={},
                )
                display_synthetic = True
                _log.warning(
                    "agent_tool_timeout | trace=%s | step=%d | tool=%s | timeout=%.1fs",
                    ctx.trace_id, step_idx, tool_name, tool_timeout,
                )
                # 工具超时同样消耗步骤预算，防止在失败工具上无限重试
                if _tool_timeout_free_budget > 0:
                    _tool_timeout_free_budget -= 1
            tool_calls_made += 1
            if not result.display and result.error:
                result.display = f"{tool_name} 失败: {result.error}"
                display_synthetic = True
            result_tool_name = tool_name

            # 工具失败后不再由本地 if-链偷偷换一个工具重跑：错误已经通过
            # _append_tool_result 回喂给模型，由模型自己决定下一步（换工具 / 换分区 /
            # 直接说明失败）。旧实现在同一个 step 里静默执行第二个工具，
            # 模型看不到这次调用，也无法否决它选的替代工具和拼出来的 query。
            if result.ok:
                succeeded_tool_signatures.add(tool_signature)
            if result.ok and ext_sig:
                seen_external_fact_signatures.add(ext_sig)
                successful_external_fact_tools += 1

            if result.ok:
                consecutive_tool_errors[result_tool_name] = 0
            else:
                consecutive_tool_errors[result_tool_name] = consecutive_tool_errors.get(result_tool_name, 0) + 1
            # 记录稳定指纹供 loop 检测：同参数同结果（含 ok/display/error）才算空转。
            loop_guard.observe(
                ToolCallRecord(
                    name=result_tool_name,
                    args_hash=hash_call(result_tool_name, tool_args),
                    result_hash=hash_result(
                        {
                            "ok": result.ok,
                            "display": result.display,
                            "error": result.error,
                        }
                    ),
                )
            )
            # 轻量 checkpoint：每步工具调用落 journal（若配置了 step_journal），供诊断/恢复。
            if getattr(self, "step_journal", None) is not None:
                try:
                    self.step_journal.record(
                        trace_id=ctx.trace_id,
                        step=step_idx,
                        tool=result_tool_name,
                        ok=result.ok,
                        error=result.error or "",
                        elapsed_ms=self._elapsed(t0),
                    )
                except Exception:
                    pass
            # 完整恢复 checkpoint：每步保存 step_idx/messages/steps，超时重试可续跑。
            if checkpoint is not None:
                try:
                    checkpoint.save(
                        trace_id=ctx.trace_id,
                        step_idx=step_idx,
                        messages=messages,
                        steps=steps,
                    )
                except Exception:
                    pass

            # 记录 side-effect 发送工具已发送的媒体 URL
            if result.ok and result_tool_name in self._SIDE_EFFECT_SEND_TOOLS:
                # 从工具返回的 data 中提取媒体 URL
                if result.data and isinstance(result.data, dict):
                    for key in ["image_url", "video_url", "audio_url", "audio_file", "file", "local_file"]:
                        url = normalize_text(str(result.data.get(key, "")))
                        if url:
                            tool_sent_media.add(url)
                    # 处理 image_urls 列表
                    image_urls_list = result.data.get("image_urls", [])
                    if isinstance(image_urls_list, list):
                        for url in image_urls_list:
                            url = normalize_text(str(url))
                            if url:
                                tool_sent_media.add(url)

            compact_data: dict[str, Any] = {}
            if isinstance(result.data, dict) and result.data:
                compact_data = self._compact_data(result.data)

            step_payload: dict[str, Any] = {
                "step": step_idx,
                "tool": result_tool_name,
                "ok": result.ok,
                "display": clip_text(result.display, 300),
                "error": result.error,
            }
            if display_synthetic:
                step_payload["display_synthetic"] = True
            if compact_data:
                step_payload["data"] = compact_data
            steps.append(step_payload)

            _log.info(
                "agent_tool_result | trace=%s | step=%d | tool=%s | ok=%s | display=%s",
                ctx.trace_id,
                step_idx,
                result_tool_name,
                result.ok,
                clip_text(result.display, 100),
            )

            # 把工具结果喂回 LLM
            tool_result_msg = {
                "tool_result": {
                    "tool": result_tool_name,
                    "ok": result.ok,
                    "display": clip_text(result.display, 800),
                }
            }
            if result.error:
                tool_result_msg["tool_result"]["error"] = result.error
            if not result.ok and result.error:
                # Hermes 式错误回喂：让模型根据 error 自纠重调，而不是代码侧猜参。
                # _normalize_tool_args 仍保留为最后防线，但优先引导模型自己改。
                tool_result_msg["tool_result"]["retry_instruction"] = (
                    "工具调用失败。请阅读上方 error 定位原因，用正确的参数重新调用该工具；"
                    "如果参数确实无法满足，直接向用户说明失败和替代方案。不要臆造工具结果。"
                )
            if compact_data:
                tool_result_msg["tool_result"]["data"] = compact_data

            # 终端工具完成后，强制 LLM 直接 final_answer，不再调用其他工具
            if result.ok and result_tool_name in self._TERMINAL_TOOLS:
                tool_result_msg["tool_result"]["hint"] = (
                    "操作已完成。请直接用 final_answer 回复用户确认结果，"
                    "不要再调用 send_emoji / send_sticker 等工具。"
                )
                _log.info(
                    "agent_terminal_tool_hint | trace=%s | step=%d | tool=%s",
                    ctx.trace_id, step_idx, result_tool_name,
                )
                
            self._append_tool_result(
                messages,
                parsed,
                assistant_msg,
                response_text,
                tool_result_msg["tool_result"]
            )

        # 达到 max_steps，用最后的信息兜底
        _log.warning(
            "agent_max_steps | trace=%s | steps=%d | external_fact_ok=%d",
            ctx.trace_id,
            self.max_steps,
            successful_external_fact_tools,
        )
        return await self._build_fallback_result(
            ctx, steps, tool_calls_made, t0, "max_steps_reached"
        )

    # ── 系统提示词构建 ──

    def _build_sticker_hint(self, ctx: AgentContext) -> str:
        """构建表情包使用提示，含心情状态。"""
        if not ctx.sticker_manager:
            return ""
        face_count = ctx.sticker_manager.face_count
        emoji_count = ctx.sticker_manager.emoji_count
        if face_count == 0 and emoji_count == 0:
            return ""
        hint_parts = []
        if face_count > 0:
            faces = ctx.sticker_manager.face_list_for_prompt()
            if faces:
                hint_parts.append(f"\n\n可用 QQ 经典表情 ({face_count} 个): {faces}")
        if emoji_count > 0:
            hint_parts.append(
                f"\n可用自定义表情包: {emoji_count} 个 (使用 send_emoji 工具，兼容别名 send_sticker)"
            )
            latest_parts: list[str] = []
            latest_for_user = None
            if hasattr(ctx.sticker_manager, "last_learned_emoji"):
                latest_for_user = ctx.sticker_manager.last_learned_emoji(
                    source_user=ctx.user_id
                )
                latest_global = ctx.sticker_manager.last_learned_emoji()
            else:
                latest_global = None

            def _render_latest(prefix: str, payload: Any) -> str:
                if not payload or not isinstance(payload, tuple) or len(payload) < 2:
                    return ""
                key, emoji = payload[0], payload[1]
                desc = normalize_text(str(getattr(emoji, "description", ""))) or str(key).split("/")[-1]
                category = normalize_text(str(getattr(emoji, "category", "")))
                tags = getattr(emoji, "tags", []) or []
                tag_text = ",".join(
                    normalize_text(str(item)) for item in tags[:3] if normalize_text(str(item))
                )
                parts = [f"{prefix}: {desc}"]
                if category:
                    parts.append(f"分类={category}")
                if tag_text:
                    parts.append(f"标签={tag_text}")
                return " | ".join(parts)

            latest_user_line = _render_latest("当前用户最近学到的表情包", latest_for_user)
            if latest_user_line:
                latest_parts.append(latest_user_line)
            latest_global_line = _render_latest("全局最近学到的表情包", latest_global)
            if latest_global_line and latest_global_line not in latest_parts:
                latest_parts.append(latest_global_line)
            if latest_parts:
                hint_parts.append("\n" + "\n".join(latest_parts))
        mood = getattr(ctx, "bot_mood", "") or ""
        if mood and emoji_count > 0:
            hint_parts.append(f"\n当前心情: {mood}。")
        if emoji_count > 0 or face_count > 0:
            hint_parts.append(
                "\n规则: 只有用户明确要求发送/预览表情时，才调用 send_emoji/send_face。"
                "如果用户是在学习表情包、纠正描述、查询表情包库、问“学会了吗/更新了吗/刚学的是什么”，"
                "优先回答状态或使用 list_emojis / correct_sticker，不要自己顺手发一张表情。"
            )
        return "".join(hint_parts) if hint_parts else ""

    def _load_prompt_navigator(self) -> PromptNavigator:
        return PromptNavigator.from_payload(_pl.get_section("prompt_navigator"))

    def _strict_tool_routing_enabled(self) -> bool:
        navigator = self._load_prompt_navigator()
        return bool(navigator.enabled and navigator.config.strict_tool_routing)

    def _list_permission_visible_tools(self, permission_level: str) -> list[str]:
        registry = self.tool_registry
        public_lister = getattr(registry, "list_tools_for_permission", None)
        if callable(public_lister):
            return list(public_lister(permission_level))
        private_lister = getattr(registry, "_list_tools_for_permission", None)
        if callable(private_lister):
            return list(private_lister(permission_level))
        legacy_selector = getattr(registry, "select_tools_for_intent", None)
        if callable(legacy_selector):
            return list(legacy_selector("", permission_level))
        return ["think", "final_answer", NAVIGATE_SECTION_TOOL]

    def _requires_tool_review_before_final(self, ctx: AgentContext) -> bool:
        state = ctx.navigator_state
        evidence = set(state.evidence if state is not None else [])
        if evidence & {
            "video_url",
            "url",
            "message_or_reply_media",
            "image_url",
            "download_file_extension",
            "recent_media_artifact",
        }:
            return True
        if ctx.media_summary or ctx.reply_media_summary:
            return True
        if isinstance(ctx.recent_media_artifact, dict) and ctx.recent_media_artifact:
            return True
        merged = "\n".join(
            normalize_text(str(item or ""))
            for item in (
                ctx.message_text,
                ctx.original_message_text,
                ctx.reply_to_text,
            )
        )
        return bool(self._extract_first_url(merged))

    def _has_navigator_section_evidence(self, ctx: AgentContext) -> bool:
        """当前分区是否由结构证据选中（而不是默认闲聊分区）。

        只用来决定「首轮 LLM 要不要早点超时、把决定权交给小 prompt 重试」，
        不用来选工具：工具名一律由模型在分区可见工具里挑。
        """
        state = ctx.navigator_state
        if state is None:
            return False
        active = normalize_text(state.active_section)
        if not active or active in {"general_chat", "fallback_debug"}:
            return False
        if not set(state.evidence or []):
            return False
        domain_tools = [
            normalize_text(str(name))
            for name in (ctx.native_tools or [])
            if normalize_text(str(name))
            not in {"", "think", "final_answer", NAVIGATE_SECTION_TOOL}
        ]
        return any(self.tool_registry.has_tool(name) for name in domain_tools)

    def _consume_navigator_pending_tool_retry(
        self, ctx: AgentContext
    ) -> tuple[str, dict[str, Any]] | None:
        pending = ctx.navigator_pending_tool_retry
        ctx.navigator_pending_tool_retry = None
        if not pending:
            return None
        tool_name, tool_args = pending
        tool_name = normalize_text(str(tool_name))
        if not tool_name or not isinstance(tool_args, dict):
            return None
        visible_tools = [
            normalize_text(str(name))
            for name in (ctx.native_tools or [])
            if normalize_text(str(name))
        ]
        if tool_name not in visible_tools or not self.tool_registry.has_tool(tool_name):
            _log.info(
                "navigator_pending_tool_retry_invalid | trace=%s | section=%s | tool=%s | visible=%s",
                ctx.trace_id,
                ctx.navigator_state.active_section if ctx.navigator_state else "",
                tool_name or "-",
                ",".join(visible_tools),
            )
            return None
        if tool_name in {"think", "final_answer", NAVIGATE_SECTION_TOOL}:
            return None

        # 必填参数校验：这次调用的参数是超时兜底那个小 prompt 合成的，不是模型在
        # 完整上下文里给的。缺必填字段就丢掉这次调用，只保留分区切换 —— 让模型自己
        # 在目标分区里重新调，它有完整 schema，比小 prompt 猜得准。
        # 实测：`知乎上搜一下 rust 值不值得学` 合成出 {"query": ...} 漏了 mode，
        # 工具直接报 invalid_args，白烧一步预算。
        missing = self._missing_required_args_from_schema(tool_name, tool_args)
        if missing:
            _log.info(
                "navigator_pending_tool_retry_missing_args | trace=%s | tool=%s | missing=%s",
                ctx.trace_id,
                tool_name,
                ",".join(missing),
            )
            return None

        return tool_name, dict(tool_args)

    def _missing_required_args_from_schema(
        self, tool_name: str, args: dict[str, Any]
    ) -> list[str]:
        """按注册表里的 ToolSchema 校验必填参数。

        与 `_missing_required_tool_args` 的硬编码白名单不同，这里读的是工具自己
        声明的 `parameters.required`，所以新增工具自动受益，不需要同步两处。
        """

        getter = getattr(self.tool_registry, "get_schema", None)
        if not callable(getter):
            return []
        schema = getter(tool_name)
        params = getattr(schema, "parameters", None)
        if not isinstance(params, dict):
            return []
        required = params.get("required")
        if not isinstance(required, list):
            return []
        return [
            str(name)
            for name in required
            if not normalize_text(str(args.get(str(name), "") or ""))
        ]

    async def _navigator_timeout_tool_retry(
        self,
        *,
        ctx: AgentContext,
        step_idx: int,
        tool_calls_made: int,
        steps: list[dict[str, Any]],
        remaining: float,
        budget_cap: float | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """Ask a tiny prompt for the next tool when the active section stalls.

        This is still Prompt Navigator driven: the retry can only choose from
        tools already exposed by the active section, and it receives the same
        section instructions plus tool schemas.
        """
        if tool_calls_made > 0:
            return None
        if not self._has_only_navigator_retry_steps(steps):
            return None
        state = ctx.navigator_state
        if state is None:
            return None
        active = normalize_text(state.active_section)
        if not active or active == "general_chat":
            return None
        navigator = self._load_prompt_navigator()
        if not navigator.enabled:
            return None
        section = navigator.config.sections.get(active)
        if section is None:
            return None
        visible_tools = [
            normalize_text(str(name))
            for name in (ctx.native_tools or [])
            if normalize_text(str(name))
        ]
        domain_tools = [
            name
            for name in visible_tools
            if name not in {"think", "final_answer", NAVIGATE_SECTION_TOOL}
            and self.tool_registry.has_tool(name)
        ]
        if not domain_tools:
            return None
        timeout = self._resolve_navigator_retry_timeout(remaining, budget_cap)
        if timeout <= 0:
            return None
        tool_docs = ""
        try:
            tool_docs = self.tool_registry.get_schemas_for_prompt_filtered(domain_tools)
        except Exception:
            tool_docs = "\n".join(f"- {name}" for name in domain_tools)
        lines = [
            "你是 YuKiKo 的 Prompt Navigator 工具决策器。",
            '只输出 JSON: {"tool":"工具名","args":{...}}。',
            "不能回答用户，不能输出 final_answer/think，只能从当前分区已暴露的真实工具中选一个下一步工具。",
            f"当前分区: {active}",
            f"分区适用: {section.when_to_use or section.name or active}",
            f"分区指令: {clip_text(section.instructions, 900)}",
            "可用工具: " + ", ".join(domain_tools),
        ]
        if tool_docs:
            lines.append("工具 schema/说明:\n" + clip_text(tool_docs, 3600))
        if active == "web_research" and "wayback_lookup" in domain_tools:
            lines.append(
                "网络时光机/历史网页/Wayback 任务默认先调用 wayback_lookup；"
                "只有用户明确要求按年份统计/时间线时才用 wayback_timeline。"
            )
        if active == "media_search" and "search_media" in domain_tools:
            lines.append(
                '媒体检索默认调用 search_media；args 必须包含 {"query":"主题关键词","media_type":"video|image|gif"}。'
            )
        if section.fallback_sections:
            lines.append(
                "如果当前分区明显不适合，本轮不要硬答；返回当前分区里最接近的探索工具，"
                "后续主 Agent 会根据 observation 再跳分区。"
            )
        user_parts = [
            f"当前消息: {clip_text(normalize_text(ctx.message_text), 300)}",
        ]
        original = normalize_text(ctx.original_message_text)
        if original and original != normalize_text(ctx.message_text):
            user_parts.append(f"原始消息: {clip_text(original, 300)}")
        if normalize_text(ctx.reply_to_text):
            user_parts.append(
                f"引用文本: {clip_text(normalize_text(ctx.reply_to_text), 300)}"
            )
        if ctx.media_summary or ctx.reply_media_summary:
            user_parts.append(
                "媒体结构: "
                + ", ".join(
                    [*list(ctx.media_summary or []), *list(ctx.reply_media_summary or [])][
                        :8
                    ]
                )
            )
        if isinstance(ctx.recent_media_artifact, dict) and ctx.recent_media_artifact:
            artifact = {
                key: ctx.recent_media_artifact.get(key)
                for key in (
                    "type",
                    "source_url",
                    "url",
                    "video_url",
                    "image_url",
                    "title",
                    "send_status",
                )
                if ctx.recent_media_artifact.get(key)
            }
            if artifact:
                user_parts.append(
                    "最近媒体 artifact: "
                    + clip_text(json.dumps(artifact, ensure_ascii=False), 500)
                )
        try:
            raw = await asyncio.wait_for(
                self.model_client.chat_text_with_retry(
                    [
                        {"role": "system", "content": "\n".join(lines)},
                        {"role": "user", "content": "\n".join(user_parts)},
                    ],
                    max_tokens=260,
                    retries=0,
                    backoff=0.0,
                    **self._navigator_retry_model_kwargs(),
                ),
                timeout=timeout,
            )
        except Exception as exc:
            _log.info(
                "navigator_timeout_tool_retry_failed | trace=%s | step=%d | section=%s | model=%s | %s",
                ctx.trace_id,
                step_idx,
                active,
                self.navigator_retry_model or "-",
                exc,
            )
            return None
        payload = self._parse_json_object_from_text(str(raw or ""))
        if not isinstance(payload, dict):
            return None
        tool_name = normalize_text(
            str(
                payload.get("tool")
                or payload.get("name")
                or payload.get("tool_name")
                or ""
            )
        )
        args = payload.get("args", {})
        if not isinstance(args, dict):
            args = {}
        if tool_name not in domain_tools:
            _log.info(
                "navigator_timeout_tool_retry_invalid | trace=%s | step=%d | section=%s | tool=%s | allowed=%s",
                ctx.trace_id,
                step_idx,
                active,
                tool_name or "-",
                ",".join(domain_tools),
            )
            return None
        return tool_name, args

    @staticmethod
    def _has_only_navigator_retry_steps(steps: list[dict[str, Any]]) -> bool:
        for step in steps:
            if not isinstance(step, dict):
                return False
            tool = step.get("tool")
            if tool == NAVIGATE_SECTION_TOOL:
                continue
            if tool == "policy_guard" and step.get("error") in {
                "navigator_tool_required_before_final_answer",
                "navigator_tool_required_before_direct_reply",
            }:
                continue
            return False
        return True

    async def _navigator_timeout_section_retry(
        self,
        *,
        ctx: AgentContext,
        step_idx: int,
        tool_calls_made: int,
        steps: list[dict[str, Any]],
        remaining: float,
        budget_cap: float | None = None,
    ) -> tuple[str, str, str, dict[str, Any]] | None:
        """Use a tiny LLM router prompt when the full Agent prompt stalls before any tool.

        This keeps free-text routing prompt-driven: the retry can choose a
        Navigator section and, when it is confident, a first tool from that
        target section. The Agent validates both before execution.
        """
        if tool_calls_made > 0 or steps or step_idx != 0:
            return None
        state = ctx.navigator_state
        if state is None:
            return None
        if normalize_text(state.active_section) != "general_chat":
            return None
        navigator = self._load_prompt_navigator()
        if not navigator.enabled:
            return None
        timeout = self._resolve_navigator_retry_timeout(remaining, budget_cap)
        if timeout <= 0:
            return None
        # 到这一行，这次 LLM 调用一定会发生 —— 无论后面走哪条 return，
        # 本回合的延迟已经付掉了。原先只有「选中分区」和「超时」两条留日志，
        # 另外三条静默 return None（模型选了同一分区 / 未知分区 / JSON 解不出）
        # 一条都没有，于是无法回答「preflight 跑了多少次、多少次白跑」。
        # 实测（storage/logs/yukiko.log，186 个 general_chat 回合）：
        # 全部跑了 preflight，日志里却只看得见 99 个，另外 87 个静默。
        started_at = time.monotonic()
        lines = [
            "你是 YuKiKo 的 Prompt Navigator 分区选择器。",
            '只输出 JSON: {"section_id":"分区ID","reason":"一句话原因","tool":"可选工具名","args":{}}。',
            "不能回答用户。先选择最合适分区；如果目标分区需要马上调用工具且参数足够，就填该分区真实工具名和 args。",
            "tool 可以为空；不能填 think、final_answer、navigate_section；不能选择目标分区 tools 以外的工具。",
            "可执行任务不要只选分区：找/看/发图片视频通常同时给 search_media；网页归档通常同时给 wayback_lookup。",
            '媒体检索 args 示例: {"query":"主题关键词","media_type":"video|image|gif"}。',
            "分区目录:",
        ]
        visible = {
            normalize_text(str(name))
            for name in (state.visible_tools or [])
            if normalize_text(str(name))
        }
        for sid, section in navigator.config.sections.items():
            tools = [
                name
                for name in section.tools
                if name in visible and self.tool_registry.has_tool(name)
            ]
            lines.append(
                f"- {sid}: {section.when_to_use or section.name or sid} | "
                f"tools={', '.join(tools) if tools else '-'} | "
                f"instructions={clip_text(section.instructions, 240)}"
            )
        user_parts = [
            f"当前消息: {clip_text(normalize_text(ctx.message_text), 240)}",
        ]
        if normalize_text(ctx.reply_to_text):
            user_parts.append(
                f"引用文本: {clip_text(normalize_text(ctx.reply_to_text), 240)}"
            )
        if ctx.media_summary or ctx.reply_media_summary:
            user_parts.append(
                "媒体结构: "
                + ", ".join(
                    [*list(ctx.media_summary or []), *list(ctx.reply_media_summary or [])][
                        :8
                    ]
                )
            )
        artifact = ctx.recent_media_artifact if isinstance(ctx.recent_media_artifact, dict) else {}
        if artifact:
            user_parts.append(
                "最近媒体 artifact: "
                + clip_text(json.dumps(artifact, ensure_ascii=False, default=str), 360)
            )
        try:
            raw = await asyncio.wait_for(
                self.model_client.chat_text_with_retry(
                    [
                        {"role": "system", "content": "\n".join(lines)},
                        {"role": "user", "content": "\n".join(user_parts)},
                    ],
                    max_tokens=220,
                    retries=0,
                    backoff=0.0,
                    **self._navigator_retry_model_kwargs(),
                ),
                timeout=timeout,
            )
        except Exception as exc:
            # `asyncio.TimeoutError` 的 str() 是空的，所以显式打出异常类型 ——
            # 否则日志尾部只剩一个空字段，看不出是超时还是别的错误。
            _log.info(
                "navigator_timeout_section_retry_failed | trace=%s | step=%d | model=%s"
                " | elapsed=%.1fs | budget=%.1fs | exc=%s | %s",
                ctx.trace_id,
                step_idx,
                self.navigator_retry_model or "-",
                time.monotonic() - started_at,
                timeout,
                type(exc).__name__,
                exc,
            )
            return None
        elapsed = time.monotonic() - started_at

        def _noop(outcome: str, detail: str = "") -> None:
            """记一次「跑完但没改变任何东西」的 preflight。

            这三种结果与超时不同：调用成功返回了，只是结论用不上。延迟照付，
            所以必须和成功分区切换分开计数，否则 preflight 的收益会被高估。
            """
            _log.info(
                "navigator_preflight_noop | trace=%s | step=%d | outcome=%s"
                " | elapsed=%.1fs | active=%s%s",
                ctx.trace_id,
                step_idx,
                outcome,
                elapsed,
                state.active_section,
                f" | detail={detail}" if detail else "",
            )

        payload = self._parse_json_object_from_text(str(raw or ""))
        if not isinstance(payload, dict):
            _noop("unparseable_json", clip_text(str(raw or ""), 120) or "-")
            return None
        section_id = normalize_text(str(payload.get("section_id", "")))
        reason = normalize_text(str(payload.get("reason", ""))) or "full prompt timed out; tiny navigator selected this section"
        if not section_id:
            _noop("missing_section_id")
            return None
        if section_id == state.active_section:
            # 模型认为就该留在当前分区。这是最常见的一条，且完全合理 ——
            # 但它意味着这次调用没产生任何决策，是 preflight 成本核算的关键项。
            _noop("same_section")
            return None
        if section_id not in navigator.config.sections:
            _noop("unknown_section", clip_text(section_id, 60))
            return None
        tool_name = normalize_text(
            str(
                payload.get("tool")
                or payload.get("name")
                or payload.get("tool_name")
                or ""
            )
        )
        tool_args = payload.get("args", {})
        if not isinstance(tool_args, dict):
            tool_args = {}
        if tool_name.lower() in {"", "none", "null", "无", "不调用"}:
            tool_name = ""
            tool_args = {}
        if tool_name:
            target = navigator.config.sections[section_id]
            allowed = {
                normalize_text(str(name))
                for name in target.tools
                if normalize_text(str(name))
            }
            if (
                tool_name not in allowed
                or tool_name not in visible
                or tool_name in {"think", "final_answer", NAVIGATE_SECTION_TOOL}
                or not self.tool_registry.has_tool(tool_name)
            ):
                _log.info(
                    "navigator_timeout_section_retry_tool_invalid | trace=%s | step=%d | section=%s | tool=%s",
                    ctx.trace_id,
                    step_idx,
                    section_id,
                    tool_name or "-",
                )
                tool_name = ""
                tool_args = {}
        return section_id, reason, tool_name, tool_args

    @staticmethod
    def _parse_json_object_from_text(text: str) -> dict[str, Any] | None:
        raw = normalize_text(text)
        if not raw:
            return None
        block = _RE_CODE_BLOCK.search(raw)
        if block:
            raw = block.group(1).strip()
        else:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                raw = raw[start : end + 1]
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _navigator_retry_model_kwargs(self) -> dict[str, str]:
        model = normalize_text(str(getattr(self, "navigator_retry_model", "") or ""))
        return {"model": model} if model else {}

    @staticmethod
    def _strip_urls_and_hosts(text: str) -> str:
        stripped = _RE_URL_STRIP.sub(" ", normalize_text(text))
        stripped = _RE_BARE_WEB_HOST.sub(" ", stripped)
        stripped = re.sub(r"\[CQ:[^\]]+\]", " ", stripped)
        stripped = re.sub(r"@\S+", " ", stripped)
        stripped = _RE_PUNCTUATION_CJK.sub(" ", stripped)
        stripped = _RE_WHITESPACE.sub(" ", stripped).strip()
        return stripped

    @staticmethod
    def _has_only_navigator_tool_policy_blocks(steps: list[dict[str, Any]]) -> bool:
        if not steps:
            return False
        for step in steps:
            if not isinstance(step, dict):
                return False
            if step.get("tool") != "policy_guard":
                return False
            if step.get("error") not in {
                "navigator_tool_required_before_final_answer",
                "navigator_tool_required_before_direct_reply",
            }:
                return False
        return True

    def _apply_prompt_navigator_scope(
        self,
        ctx: AgentContext,
        base_tools: list[str],
    ) -> tuple[list[str], str]:
        navigator = self._load_prompt_navigator()
        if not navigator.enabled:
            ctx.navigator_state = None
            return base_tools, ""

        state = navigator.initial_state(ctx, base_tools)
        ctx.navigator_state = state
        selected_tools = navigator.scoped_tools(state) or list(base_tools)
        _log.info(
            "navigator_preselect | trace=%s | active=%s | candidates=%s | evidence=%s",
            ctx.trace_id,
            state.active_section,
            ",".join(state.candidate_sections),
            ",".join(state.evidence),
        )
        _log.info(
            "navigator_tool_scope | trace=%s | section=%s | tools=%s",
            ctx.trace_id,
            state.active_section,
            ",".join(selected_tools),
        )
        _log.info(
            "navigator_section_selected | trace=%s | section=%s",
            ctx.trace_id,
            state.active_section,
        )
        return selected_tools, navigator.render_system_block(state, selected_tools)

    def _handle_navigate_section_tool(
        self,
        ctx: AgentContext,
        tool_args: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        state = ctx.navigator_state
        navigator = self._load_prompt_navigator()
        if state is None or not navigator.enabled:
            _log.info("navigator_fallback | trace=%s | reason=disabled", ctx.trace_id)
            return False, {
                "tool": NAVIGATE_SECTION_TOOL,
                "ok": False,
                "error": "prompt_navigator_disabled",
                "display": "Prompt Navigator 未启用，请直接使用当前工具或 final_answer。",
            }

        section_id = normalize_text(str(tool_args.get("section_id", "")))
        reason = normalize_text(str(tool_args.get("reason", "")))
        previous = state.active_section
        ok, status = navigator.switch_section(state, section_id)
        selected_tools = navigator.scoped_tools(state) or list(state.visible_tools)
        ctx.native_tools = selected_tools
        if ok:
            _log.info(
                "navigator_switch | trace=%s | from=%s | to=%s | count=%d | reason=%s",
                ctx.trace_id,
                previous,
                state.active_section,
                state.switch_count,
                clip_text(reason, 160),
            )
            _log.info(
                "navigator_tool_scope | trace=%s | section=%s | tools=%s",
                ctx.trace_id,
                state.active_section,
                ",".join(selected_tools),
            )
        else:
            _log.info(
                "navigator_fallback | trace=%s | from=%s | target=%s | status=%s",
                ctx.trace_id,
                previous,
                section_id,
                status,
            )
        tool_docs = self.tool_registry.get_schemas_for_prompt_filtered(selected_tools)
        display = navigator.render_switch_result(state, selected_tools, tool_docs)
        payload: dict[str, Any] = {
            "tool": NAVIGATE_SECTION_TOOL,
            "ok": ok,
            "display": display if ok else f"{status}\n{display}",
            "data": {
                "active_section": state.active_section,
                "tools": selected_tools,
                "switch_count": state.switch_count,
            },
        }
        if not ok:
            payload["error"] = status
        return ok, payload

    def _build_system_prompt(self, ctx: AgentContext) -> str:
        """构建 Agent 系统提示词。"""
        template = _pl.get_dict("agent")

        identity_text = template.get("identity", "")
        rules_text = template.get("rules", "")
        reply_style_text = template.get("reply_style", "")
        model_client = getattr(self, "model_client", None)
        native_tool_calling = bool(
            getattr(model_client, "supports_native_tool_calling", lambda: False)()
        )
        if native_tool_calling:
            output_format_text = (
                "你可以直接回复自然语言与用户交互。\n"
                "当你需要执行特定操作（如搜索、发图、控制群等）时，请调用注入的 tools (函数调用)。\n"
                "可以按需多次调用工具，直到获得足够信息后再给出最终自然语言回复。"
            )
            tools_text = (
                "可用工具已经通过 function call 机制传入，无需输出 JSON，直接调用对应函数即可。"
            )
        else:
            output_format_text = template.get("output_format", "")
            tools_text = template.get("tools", "") or template.get("tool_usage", "")
        tool_priority_text = template.get("tool_priority", "")
        context_rules_text = template.get("context_rules", "")
        network_flow_text = template.get("network_flow", "")

        # Prompt Navigator: 本地只用结构信号预选分区，最终由 LLM 复核/跳转。
        perm_level = self._resolve_permission_level(ctx)
        base_tools = self._list_permission_visible_tools(perm_level)
        selected_tools, navigator_prompt = self._apply_prompt_navigator_scope(
            ctx,
            base_tools,
        )
        ctx.native_tools = selected_tools
        tool_docs = (
            ""
            if native_tool_calling
            else self.tool_registry.get_schemas_for_prompt_filtered(selected_tools)
        )
        tool_hints_map = _pl.get_dict("tool_hints")
        selected_tool_hints: list[str] = []
        if tool_hints_map and selected_tools:
            for tool_name in selected_tools:
                hint_text = normalize_text(tool_hints_map.get(tool_name, ""))
                if not hint_text:
                    continue
                selected_tool_hints.append(f"- {tool_name}: {hint_text}")
                if len(selected_tool_hints) >= 12:
                    break
        sticker_hint = ""
        if hasattr(ctx, "sticker_manager") and ctx.sticker_manager:
            sticker_hint = self._build_sticker_hint(ctx)

        context_parts = []
        if ctx.memory_context:
            context_parts.append(
                "最近对话:\n" + "\n".join(f"- {m}" for m in ctx.memory_context[-8:])
            )
        if ctx.related_memories:
            context_parts.append(
                "相关记忆:\n" + "\n".join(f"- {m}" for m in ctx.related_memories[:5])
            )
        if ctx.user_profile_summary:
            context_parts.append(
                f"用户画像: {clip_text(ctx.user_profile_summary, 300)}"
            )
        if ctx.preferred_name:
            context_parts.append(f"用户偏好称呼: {ctx.preferred_name}")
        compat_context = normalize_text(ctx.compat_context)
        if compat_context:
            context_parts.append(compat_context)
        if ctx.recent_speakers:
            speaker_rows: list[str] = []
            for uid, name, preview in ctx.recent_speakers[:8]:
                user_label = normalize_text(name)
                if not user_label:
                    user_label = "群成员" if uid else "某人"
                tail = (
                    f" 最近说: {clip_text(normalize_text(preview), 60)}"
                    if normalize_text(preview)
                    else ""
                )
                speaker_rows.append(f"- {user_label}(QQ:{uid}){tail}")
            if speaker_rows:
                context_parts.append("最近活跃用户:\n" + "\n".join(speaker_rows))
                context_parts.append(
                    "多人对话规则: 先判断用户在回复谁；出现“他/她/这个人”等指代时，优先结合 @对象、回复锚点和最近活跃用户再作答。"
                )
        if ctx.runtime_group_context:
            rows = [
                f"- {clip_text(normalize_text(item), 100)}"
                for item in ctx.runtime_group_context[:8]
                if normalize_text(item)
            ]
            if rows:
                context_parts.append("群聊近期上下文:\n" + "\n".join(rows))
        if ctx.thread_state:
            state_text = self._clip_json_for_prompt(ctx.thread_state, max_chars=360)
            if normalize_text(state_text):
                context_parts.append(f"会话线程状态: {state_text}")
        if ctx.runtime_admin_policy:
            required = bool(
                ctx.runtime_admin_policy.get("high_risk_confirmation_required", True)
            )
            source = normalize_text(str(ctx.runtime_admin_policy.get("source", "default"))) or "default"
            context_parts.append(
                f"当前高风险二次确认策略: {'开启' if required else '关闭'}（来源: {source}）"
            )
        if ctx.user_directives:
            context_parts.append(
                "用户专属指令:\n" + "\n".join(f"- {d}" for d in ctx.user_directives[:5])
            )
        # ── 好感度 & 心情注入 Agent 上下文 ──
        if ctx.affinity_hint:
            context_parts.append(ctx.affinity_hint)
        if ctx.mood_hint:
            context_parts.append(ctx.mood_hint)
        context_block = (
            "\n\n".join(context_parts) if context_parts else "(无额外上下文)"
        )

        prompt = (
            f"## 身份\n{identity_text}\n\n"
        )
        if self.persona_text:
            prompt += f"## 人格底稿（最高优先级，定义你是谁、怎么说话、怎么互动）\n{self.persona_text}\n\n"
        prompt += (
            f"## 输出格式\n{output_format_text}\n\n"
            f"## 规则\n{rules_text}\n\n"
        )
        if reply_style_text:
            prompt += f"## 回复风格\n{reply_style_text}\n\n"
        if network_flow_text:
            prompt += f"## 联网任务流程\n{network_flow_text}\n\n"
        prompt += f"## 工具使用\n{tools_text}{sticker_hint}\n\n"
        if normalize_text(tool_priority_text):
            prompt += f"## 工具优先级\n{tool_priority_text}\n\n"
        if navigator_prompt:
            prompt += f"{navigator_prompt}\n\n"
        # 渐进式披露：只注入技能目录（name+description），全文由 read_skill 命中后读取。
        if getattr(self, "skill_registry", None) is not None:
            try:
                if self.skill_registry.load():
                    skill_catalog = self.skill_registry.describe()
                    if skill_catalog:
                        prompt += (
                            "## 可用技能\n"
                            f"{skill_catalog}\n\n"
                            "技能详细步骤需用 read_skill 工具读取全文后再执行。\n\n"
                        )
            except Exception:
                # 技能目录异常不应影响主流程。
                _log.warning("skill_catalog_render_failed", exc_info=True)
        if selected_tool_hints:
            prompt += (
                "## 工具细粒度提示（按本轮可用工具）\n"
                + "\n".join(selected_tool_hints)
                + "\n\n"
            )
        prompt += (
            "## 执行预算（硬约束）\n"
            f"- 本轮最多 {self.max_steps} 步，优先选择成功率最高的路径，不要重复同类搜索。\n"
            "- 下载类任务若工具返回扩展名/签名不匹配，必须立即换源或改用资源检索，不得继续复述失败结果。\n\n"
            "## 上下文判定优先级（必须遵守）\n"
            "- 当前消息、当前附带媒体、引用锚点优先于旧记忆。\n"
            "- 当前用户近期 > 引用对象近期 > 相关记忆 > 群聊缓存。\n"
            "- 用户事实、偏好、身份不要套给其他群成员；多人群聊先确认对象再回答。\n"
            "- 会话线程状态和群聊近期上下文用于补全语境，但不能覆盖用户当前这条的明确意思。\n"
            "- 证据冲突时优先更近、更具体、更可验证的信息；拿不准就先确认。\n"
            "- 不要用“用户+QQ尾号/QQ号”称呼当前说话人；有昵称或偏好称呼就用昵称，没有就省略称呼。\n"
            "- GIF/动图按多帧内容理解，优先回答它在表达什么，不要只盯单帧。\n\n"
            "## 上下文关联（极其重要）\n"
            f"{context_rules_text}"
        )
        # 插件注入的规则
        plugin_rules = self.tool_registry.get_prompt_hints_text(
            "rules", tool_names=selected_tools
        )
        if plugin_rules:
            prompt += f"{plugin_rules}\n"
        plugin_tools_guidance = self.tool_registry.get_prompt_hints_text(
            "tools_guidance", tool_names=selected_tools
        )
        if plugin_tools_guidance:
            prompt += f"## 工具使用指南（插件）\n{plugin_tools_guidance}\n\n"
        plugin_context = self.tool_registry.get_prompt_hints_text(
            "context", tool_names=selected_tools
        )
        if plugin_context:
            prompt += f"## 插件上下文\n{plugin_context}\n\n"
        # 动态上下文提供者
        dynamic_context = self.tool_registry.get_dynamic_context(
            {"ctx": ctx, "config": self.config, "selected_tools": selected_tools},
            tool_names=selected_tools,
        )
        if dynamic_context:
            prompt += f"## 动态上下文\n{dynamic_context}\n\n"

        # PromptPolicy 注入
        policy_tool_guidance = self.prompt_policy.build_tool_guidance_block()
        if policy_tool_guidance:
            prompt += f"## 工具注入规则（配置）\n{policy_tool_guidance}\n\n"

        agent_cfg = (
            self.config.get("agent", {}) if isinstance(self.config, dict) else {}
        )
        if isinstance(agent_cfg, dict):
            runtime_rules = normalize_text(str(agent_cfg.get("runtime_rules", "")))
            if runtime_rules:
                prompt += f"## 运行时规则（配置）\n{runtime_rules}\n\n"
            preferred_name_prompt = normalize_text(
                str(agent_cfg.get("preferred_name_prompt", ""))
            )
            if preferred_name_prompt and normalize_text(ctx.preferred_name):
                prompt += (
                    "## 用户偏好规则（配置）\n"
                    f"{preferred_name_prompt.replace('{preferred_name}', ctx.preferred_name)}\n\n"
                )

        if perm_level == "super_admin":
            prompt += (
                "## 当前用户权限: 超级管理员\n"
                "此用户是超级管理员，可以执行所有管理操作，也可以修改机器人运行策略。\n"
                "- 高风险操作是否需要二次确认，以“当前高风险二次确认策略”为准，不要自行脑补。\n"
                "- 当管理员要求调整高风险确认、忽略某人、恢复某人等运行时策略时，优先调用 admin_command。\n"
                "- 当用户明确要求修改机器人配置/策略/开关/阈值/提示词注入等时，优先调用 config_update。\n"
                "- config_update.args.patch 必须是最小变更补丁，只填必要字段，不要整份配置重写。\n"
                "- 如果需求不明确，先用简短问题确认后再调用 config_update。\n"
                "- config_update 成功后，用一句话回报已变更项与新值。\n\n"
            )
        elif perm_level == "group_admin":
            prompt += (
                "## 当前用户权限: 群管理员\n"
                "此用户是本群的管理员/群主，可以执行群管理操作（禁言、踢人、设置群名片、精华消息等）。\n"
                "- 当管理员要求调整本群高风险确认、忽略某人、恢复某人等运行时策略时，优先调用 admin_command。\n"
                "但不能执行超级管理员专属操作（退群、删好友、修改机器人配置、清缓存等）。\n\n"
            )
        else:
            prompt += (
                "## 当前用户权限: 普通用户\n"
                "此用户是普通成员，不能管理其他成员。\n"
                "- 唯一例外：如果用户明确要求禁言自己/解除自己的禁言，可以调用 set_group_ban，但目标必须是当前用户本人。\n"
                "- 其他管理操作一律不要执行。\n\n"
            )

        # 输出详略度
        _verbosity_hints = _pl.get_dict("verbosity") or {
            "verbose": "回复可以详细展开，给出完整分析和解释，不用刻意压缩。",
            "medium": "",
            "brief": "回复简短精炼，抓重点，不要展开细节。闲聊一句话搞定。",
            "minimal": "极简回复，一两句话概括。能不说就不说。",
        }
        v_hint = _verbosity_hints.get(ctx.verbosity, "")
        if v_hint:
            prompt += f"## 输出详略度\n{v_hint}\n\n"
        output_style_instruction = clip_text(
            normalize_text(ctx.output_style_instruction), 400
        )
        if output_style_instruction:
            prompt += f"## 输出风格附加要求（配置）\n{output_style_instruction}\n\n"

        now_local = datetime.now().astimezone()
        now_label = now_local.strftime("%Y-%m-%d %H:%M:%S %z")
        tz_name = now_local.tzname() or "local"
        safe_env_user_name = normalize_text(ctx.user_name)
        if safe_env_user_name == normalize_text(str(ctx.user_id)):
            safe_env_user_name = ""
        prompt += (
            f"## 环境\n"
            f"{'私聊' if ctx.is_private else f'群聊 {ctx.group_id}'} | "
            f"当前说话人: {safe_env_user_name or '当前用户'}(QQ:{ctx.user_id}) | @我: {ctx.mentioned} | 当前时间: {now_label} ({tz_name})\n\n"
            f"## 上下文\n{context_block}\n\n"
            f"## 可用工具\n{tool_docs}"
        )
        return self.prompt_policy.compose_prompt(channel="agent", base_prompt=prompt)

    @staticmethod
    def _render_runtime_tpl(template_text: str, values: dict[str, Any]) -> str:
        """安全渲染模板：缺失占位符不抛错，保留原样。"""

        class _SafeMap(dict):
            def __missing__(self, key: str) -> str:  # type: ignore[override]
                return "{" + key + "}"

        text = str(template_text or "")
        if not text:
            return ""
        try:
            return text.format_map(_SafeMap(values))
        except Exception:
            return text

    @staticmethod
    def _runtime_tpl(runtime_templates: dict[str, str], key: str, default: str) -> str:
        """读取 agent_runtime 模板；若用户显式配置空字符串则视为关闭该行。"""
        if key in runtime_templates:
            return str(runtime_templates.get(key, ""))
        return default

    @staticmethod
    def _clip_json_for_prompt(payload: Any, max_chars: int = 1100) -> str:
        try:
            text = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), default=str
            )
        except Exception:
            text = normalize_text(str(payload))
        return clip_text(normalize_text(text), max_chars)

    def _build_napcat_event_anchor(self, ctx: AgentContext) -> str:
        payload = ctx.event_payload if isinstance(ctx.event_payload, dict) else {}
        if not payload:
            return ""
        sender = payload.get("sender", {})
        if not isinstance(sender, dict):
            sender = {}
        raw = payload.get("raw", {})
        if not isinstance(raw, dict):
            raw = {}

        anchor: dict[str, Any] = {
            "post_type": payload.get("post_type", ""),
            "message_type": payload.get("message_type", ""),
            "sub_type": payload.get("sub_type", ""),
            "time": payload.get("time", ""),
            "message_id": payload.get("message_id", ""),
            "message_seq": payload.get("message_seq", ""),
            "real_id": payload.get("real_id", ""),
            "real_seq": payload.get("real_seq", ""),
            "group_id": payload.get("group_id", ""),
            "group_name": payload.get("group_name", ""),
            "user_id": payload.get("user_id", ""),
            "to_me": bool(payload.get("to_me", False)),
            "raw_message": clip_text(
                normalize_text(str(payload.get("raw_message", ""))), 220
            ),
        }
        sender_info = {
            "user_id": sender.get("user_id", ""),
            "nickname": sender.get("nickname", ""),
            "card": sender.get("card", ""),
            "role": sender.get("role", ""),
        }
        if any(normalize_text(str(v)) for v in sender_info.values()):
            anchor["sender"] = sender_info

        if raw:
            raw_anchor: dict[str, Any] = {}
            for key in (
                "id",
                "msgId",
                "msgSeq",
                "msgRandom",
                "chatType",
                "msgType",
                "subMsgType",
                "sendType",
                "msgTime",
                "senderUid",
                "senderUin",
                "peerUid",
                "peerUin",
                "peerName",
                "sendNickName",
                "sendMemberName",
            ):
                value = raw.get(key, "")
                if value not in ("", None):
                    raw_anchor[key] = value

            elements = raw.get("elements", [])
            if isinstance(elements, list) and elements:
                previews: list[dict[str, Any]] = []
                for element in elements[:3]:
                    if not isinstance(element, dict):
                        continue
                    item: dict[str, Any] = {
                        "elementType": element.get("elementType", ""),
                    }
                    text_ele = element.get("textElement", {})
                    if isinstance(text_ele, dict):
                        text_content = normalize_text(str(text_ele.get("content", "")))
                        if text_content:
                            item["text"] = clip_text(text_content, 80)
                    if element.get("picElement") is not None:
                        item["hasPic"] = True
                    if element.get("videoElement") is not None:
                        item["hasVideo"] = True
                    if element.get("pttElement") is not None:
                        item["hasPtt"] = True
                    previews.append(item)
                if previews:
                    raw_anchor["elements_preview"] = previews
                if len(elements) > 3:
                    raw_anchor["elements_more"] = len(elements) - 3
            if raw_anchor:
                anchor["napcat_raw"] = raw_anchor

        compact = self._clip_json_for_prompt(anchor, max_chars=1300)
        if not compact:
            return ""
        return f"[NapCat事件锚点]\n{compact}"

    @staticmethod
    def _build_turn_target_line(ctx: AgentContext) -> str:
        current_uid = normalize_text(str(ctx.user_id))
        current_name = normalize_text(ctx.user_name) or (
            "当前用户" if current_uid else "当前用户"
        )
        if current_name == current_uid:
            current_name = "当前用户"
        bot_uid = normalize_text(str(ctx.bot_id))
        reply_uid = normalize_text(str(ctx.reply_to_user_id))

        mention_ids: list[str] = []
        for raw_uid in ctx.at_other_user_ids or []:
            uid = normalize_text(str(raw_uid))
            if not uid:
                continue
            if uid in {bot_uid, current_uid}:
                continue
            if uid not in mention_ids:
                mention_ids.append(uid)

        if reply_uid and reply_uid != bot_uid:
            target_uid = reply_uid
            target_name = normalize_text(ctx.reply_to_user_name) or normalize_text(
                (ctx.at_other_user_names or {}).get(target_uid, "")
            )
            source = "reply_anchor"
        elif mention_ids:
            target_uid = mention_ids[0]
            target_name = normalize_text((ctx.at_other_user_names or {}).get(target_uid, ""))
            source = "mention"
        else:
            target_uid = current_uid
            target_name = current_name
            source = "current_speaker"

        target_name = target_name or (
            "被提及成员" if target_uid else current_name
        )
        if target_uid:
            return f"[本轮主要对象: {target_name}(QQ:{target_uid}) | 来源: {source}]"
        return f"[本轮主要对象: {target_name} | 来源: {source}]"

    def _build_user_message(self, ctx: AgentContext) -> str | list[dict[str, Any]]:
        """构建用户消息。"""
        runtime_templates = _pl.get_dict("agent_runtime")
        rebuilt_query = self._rebuild_query_with_context(ctx.message_text, ctx)
        speaker_name = normalize_text(ctx.user_name) or (
            "当前用户" if normalize_text(str(ctx.user_id)) else "当前用户"
        )
        if speaker_name == normalize_text(str(ctx.user_id)):
            speaker_name = "当前用户"
        speaker_line = f"[当前说话人: {speaker_name}(QQ:{ctx.user_id})]"
        if normalize_text(ctx.sender_role):
            speaker_line = f"{speaker_line[:-1]} | role={normalize_text(ctx.sender_role)}]"
        parts = [speaker_line, ctx.message_text]
        target_line = self._build_turn_target_line(ctx)
        if normalize_text(target_line):
            parts.insert(1, target_line)
        if rebuilt_query and rebuilt_query != normalize_text(ctx.message_text):
            parts.append(f"[语境补全: {rebuilt_query}]")

        event_anchor = self._build_napcat_event_anchor(ctx)
        if event_anchor:
            parts.append(event_anchor)

        # @提及的其他用户（非 bot 自身）
        if ctx.at_other_user_ids:
            at_descs = []
            for uid in ctx.at_other_user_ids:
                name = ctx.at_other_user_names.get(uid, "")
                at_descs.append(f"{name}(QQ:{uid})" if name else f"QQ:{uid}")
            parts.append(f"[用户@了: {', '.join(at_descs)}]")

        # 引用/回复消息上下文
        reply_mid = normalize_text(ctx.reply_to_message_id)
        reply_uid = normalize_text(str(ctx.reply_to_user_id))
        reply_name = normalize_text(ctx.reply_to_user_name)
        reply_text = normalize_text(ctx.reply_to_text)
        if reply_mid or reply_uid or reply_text:
            is_reply_to_bot = bool(reply_uid and reply_uid == str(ctx.bot_id))
            anchor_lines = [
                self._runtime_tpl(
                    runtime_templates, "reply_anchor_header", "[引用锚点]"
                ),
                self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "reply_anchor_line_message_id",
                        "reply_to_message_id={reply_to_message_id}",
                    ),
                    {"reply_to_message_id": reply_mid or "-"},
                ),
                self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "reply_anchor_line_user_id",
                        "reply_to_user_id={reply_to_user_id}",
                    ),
                    {"reply_to_user_id": reply_uid or "-"},
                ),
                self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "reply_anchor_line_user_name",
                        "reply_to_user_name={reply_to_user_name}",
                    ),
                    {"reply_to_user_name": reply_name or "-"},
                ),
                self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "reply_anchor_line_is_reply_to_bot",
                        "is_reply_to_bot={is_reply_to_bot}",
                    ),
                    {"is_reply_to_bot": "true" if is_reply_to_bot else "false"},
                ),
            ]
            if reply_text:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "reply_anchor_line_text",
                        "reply_to_text={reply_to_text}",
                    ),
                    {"reply_to_text": clip_text(reply_text, 240)},
                )
                if normalize_text(line):
                    anchor_lines.append(line)
            if ctx.reply_media_summary:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "reply_anchor_line_media",
                        "reply_to_media={reply_to_media}",
                    ),
                    {"reply_to_media": ", ".join(ctx.reply_media_summary[:5])},
                )
                if normalize_text(line):
                    anchor_lines.append(line)
            anchor_lines = [line for line in anchor_lines if normalize_text(line)]
            if anchor_lines:
                parts.append("\n".join(anchor_lines))

        if normalize_text(ctx.reply_to_text):
            reply_from = (
                normalize_text(ctx.reply_to_user_name)
                or normalize_text(ctx.reply_to_user_id)
                or "未知用户"
            )
            is_reply_to_bot = reply_uid == str(ctx.bot_id)
            if is_reply_to_bot:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "reply_context_to_bot",
                        "[用户在回复bot之前的消息 | bot原文: {reply_to_text}]",
                    ),
                    {
                        "reply_to_text": clip_text(
                            normalize_text(ctx.reply_to_text), 220
                        )
                    },
                )
            else:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "reply_context_to_user",
                        "[用户在回复: {reply_from}(QQ:{reply_to_user_id}) | 原文: {reply_to_text}]",
                    ),
                    {
                        "reply_from": reply_from,
                        "reply_to_user_id": reply_uid or "-",
                        "reply_to_text": clip_text(
                            normalize_text(ctx.reply_to_text), 220
                        ),
                    },
                )
            if normalize_text(line):
                parts.append(line)

        if isinstance(ctx.recent_media_artifact, dict) and ctx.recent_media_artifact:
            compact_artifact = self._clip_json_for_prompt(
                ctx.recent_media_artifact,
                max_chars=900,
            )
            if compact_artifact:
                parts.append(f"[最近可复用媒体 artifact]\n{compact_artifact}")

        if ctx.media_summary:
            image_count = sum(1 for m in ctx.media_summary if m.startswith("image:"))
            video_count = sum(1 for m in ctx.media_summary if m.startswith("video:"))
            voice_count = sum(
                1
                for m in ctx.media_summary
                if m.startswith("record") or m.startswith("audio")
            )
            media_desc = ", ".join(ctx.media_summary[:5])
            media_line = self._render_runtime_tpl(
                self._runtime_tpl(
                    runtime_templates, "attached_media_line", "[附带媒体: {media_desc}]"
                ),
                {
                    "media_desc": media_desc,
                    "image_count": image_count,
                    "video_count": video_count,
                    "voice_count": voice_count,
                },
            )
            if normalize_text(media_line):
                parts.append(media_line)
            if image_count and self._looks_like_image_question(ctx.message_text):
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_user_images",
                        "[提示: 用户发了{image_count}张图片并提问，请用 analyze_image 工具分析]",
                    ),
                    {"image_count": image_count},
                )
                if normalize_text(line):
                    parts.append(line)
            if video_count:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_user_video",
                        "[提示: 用户直接发了视频文件；内容理解优先 analyze_local_video，切片/抽音频/封面/关键帧优先 split_video]",
                    ),
                    {"video_count": video_count},
                )
                if normalize_text(line):
                    parts.append(line)
            if voice_count:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_user_voice",
                        "[提示: 用户发了语音消息，请用 analyze_voice 工具转录]",
                    ),
                    {"voice_count": voice_count},
                )
                if normalize_text(line):
                    parts.append(line)
            if self._has_animated_image_summary(ctx.media_summary):
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_user_gif",
                        "[提示: 用户发的是 GIF/动图，分析时按多帧理解动作、情绪和想表达的意思]",
                    ),
                    {},
                )
                if normalize_text(line):
                    parts.append(line)
        # 检测用户消息中的链接
        first_url = self._extract_first_url(ctx.message_text)
        if first_url:
            if self._looks_like_video_url(first_url):
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_video_url",
                        "[检测到视频链接 {url}；拿可发送直链优先 parse_video，要分析内容优先 analyze_video]",
                    ),
                    {"url": first_url},
                )
                if normalize_text(line):
                    parts.append(line)
            elif first_url.startswith("http"):
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_web_url",
                        "[检测到网页链接 {url}，用 fetch_webpage 打开]",
                    ),
                    {"url": first_url},
                )
                if normalize_text(line):
                    parts.append(line)
        if ctx.reply_media_summary:
            reply_image_count = sum(
                1 for m in ctx.reply_media_summary if m.startswith("image:")
            )
            reply_video_count = sum(
                1 for m in ctx.reply_media_summary if m.startswith("video:")
            )
            reply_voice_count = sum(
                1
                for m in ctx.reply_media_summary
                if m.startswith("record") or m.startswith("audio")
            )
            reply_media_line = self._render_runtime_tpl(
                self._runtime_tpl(
                    runtime_templates,
                    "reply_media_line",
                    "[引用消息中的媒体: {reply_media_desc}]",
                ),
                {
                    "reply_media_desc": ", ".join(ctx.reply_media_summary[:5]),
                    "reply_image_count": reply_image_count,
                    "reply_video_count": reply_video_count,
                    "reply_voice_count": reply_voice_count,
                },
            )
            if normalize_text(reply_media_line):
                parts.append(reply_media_line)
            if reply_image_count:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_reply_images_always",
                        "[提示: 引用消息里有图片；若用户在问这条引用内容，请优先 analyze_image 并以引用图为目标]",
                    ),
                    {"reply_image_count": reply_image_count},
                )
                if normalize_text(line):
                    parts.append(line)
            if reply_image_count and self._looks_like_image_question(ctx.message_text):
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_reply_images",
                        "[提示: 用户回复了一条含{reply_image_count}张图片的消息并提问，请用 analyze_image 工具分析]",
                    ),
                    {"reply_image_count": reply_image_count},
                )
                if normalize_text(line):
                    parts.append(line)
            if reply_video_count:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_reply_video",
                        "[提示: 引用消息里有视频；内容理解优先 analyze_local_video，切片/抽音频/封面/关键帧优先 split_video，并以引用视频为目标]",
                    ),
                    {"reply_video_count": reply_video_count},
                )
                if normalize_text(line):
                    parts.append(line)
            if reply_voice_count:
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_reply_voice",
                        "[提示: 引用消息含语音，请用 analyze_voice 工具转录]",
                    ),
                    {"reply_voice_count": reply_voice_count},
                )
                if normalize_text(line):
                    parts.append(line)
            if self._has_animated_image_summary(ctx.reply_media_summary):
                line = self._render_runtime_tpl(
                    self._runtime_tpl(
                        runtime_templates,
                        "hint_reply_gif",
                        "[提示: 引用消息里的是 GIF/动图；如果用户在问这条内容，要按多帧理解它的动作和语气]",
                    ),
                    {},
                )
                if normalize_text(line):
                    parts.append(line)
        
        text_content = "\n".join(parts)
        
        # 原生看图的图片块**不在这里拼** —— 见 _compose_user_content()。
        # 这里原本有一段自己从 raw_segments 取 url 塞 image_url 块的代码，已删除：
        # 那些 url 是 QQ CDN 链接，实测对外不可达
        #   HTTP 400 {"retcode":-5503022,"retmsg":"appid is not supported"}
        #   HTTP 400 {"retcode":-5503007,"retmsg":"download url has expired"}
        # 所以它只会让模型收到一堆死链，还不如不给。
        # 现在走 core/tools_vision.py 的 build_native_vision_blocks()，
        # 那边只用实测成功的取法（本地文件 61/61、NapCat get_image 45/45），
        # 且保证块里的 url 一定是 data URI。
        # 本函数保持**同步**是刻意的：tests/test_agent_smoke.py:188、
        # tests/test_tool_call_leak_regression.py:357 会把它替换成同步 lambda，
        # tests/test_dialog_and_sticker_regression.py:84 与
        # scripts/agent_deep_selfcheck.py:165 直接同步调用它。
        return text_content

    async def _compose_user_content(
        self, ctx: AgentContext, model_client: Any
    ) -> str | list[dict[str, Any]]:
        """组装 user 消息内容：纯文本，或「文本 + 原生图片块」。

        拆成独立的 async 方法而不是把 _build_user_message 改成 async，
        是因为后者有四个同步调用方（两个测试替换成 lambda、一个直接调、
        还有 scripts/agent_deep_selfcheck.py）—— 改签名会全破。
        """

        text_content = self._build_user_message(ctx)
        # _build_user_message 理论上可能已经返回 list（历史签名允许），那就别再包一层
        if not isinstance(text_content, str):
            return text_content

        executor = getattr(ctx, "tool_executor", None)
        builder = getattr(executor, "build_native_vision_blocks", None)
        if builder is None:
            return text_content
        # 模型不支持图片输入时不做无用功，也不白烧 get_image 的往返
        supports_vision = getattr(model_client, "supports_vision_input", None)
        if callable(supports_vision):
            try:
                if not supports_vision():
                    return text_content
            except Exception as exc:
                _log.info(
                    "native_vision_capability_probe_failed | trace=%s | exc=%s",
                    ctx.trace_id,
                    type(exc).__name__,
                )
                return text_content

        try:
            blocks, reason = await builder(
                raw_segments=ctx.raw_segments,
                reply_media_segments=ctx.reply_media_segments,
                api_call=getattr(ctx, "api_call", None),
            )
        except Exception as exc:
            # 原生看图是增强项，不能因为它失败就让整个回合挂掉 ——
            # 退回纯文本，模型仍可显式调 analyze_image。
            _log.warning(
                "native_vision_blocks_error | trace=%s | exc=%s | err=%s",
                ctx.trace_id,
                type(exc).__name__,
                clip_text(str(exc), 160) or "-",
            )
            return text_content

        if not blocks:
            if reason and reason not in {"no_image_segments", "vision_disabled"}:
                _log.info(
                    "native_vision_blocks_skipped | trace=%s | reason=%s",
                    ctx.trace_id,
                    reason,
                )
            return text_content

        estimate = getattr(executor, "estimate_native_vision_tokens", None)
        est_tokens = 0
        if callable(estimate):
            try:
                est_tokens = int(estimate(blocks))
            except Exception:
                est_tokens = 0
        _log.info(
            "native_vision_attached | trace=%s | blocks=%d | est_tokens=%d",
            ctx.trace_id,
            len(blocks),
            est_tokens,
        )
        return [{"type": "text", "text": text_content}, *blocks]

    @staticmethod
    def _has_animated_image_summary(rows: list[str] | None) -> bool:
        return any(
            normalize_text(str(item)).lower().startswith("image:animated:")
            for item in (rows or [])
        )

    def _normalize_tool_args(
        self, tool_name: str, args: dict[str, Any], ctx: AgentContext
    ) -> dict[str, Any]:
        """对常见工具进行缺参兜底，减少 args={} 造成的空调用。"""
        fixed = dict(args or {})
        text = normalize_text(ctx.message_text)
        contextual_query = self._rebuild_query_with_context(text, ctx)
        full_text = normalize_text(f"{ctx.message_text}\n{ctx.reply_to_text}")
        first_url = self._extract_first_url(text)
        reply_url = self._extract_first_url(normalize_text(ctx.reply_to_text))
        candidate_url = first_url or reply_url
        recent_video_url = self._extract_recent_media_url(ctx, "video")
        qq_id = self._extract_candidate_qq_id(ctx)

        def _set_if_empty(key: str, value: Any) -> None:
            if value is None:
                return
            cur = fixed.get(key)
            if cur is None:
                fixed[key] = value
                return
            if isinstance(cur, str) and not normalize_text(cur):
                fixed[key] = value
                return
            if isinstance(cur, (int, float)) and cur == 0:
                fixed[key] = value

        if tool_name == "web_search":
            _set_if_empty("query", contextual_query or text)
        elif tool_name in {"lookup_wiki"}:
            _set_if_empty(
                "keyword", self._infer_lookup_keyword(contextual_query or text)
            )
        elif tool_name == "split_video":
            explicit_video_url = self._extract_first_video_url(text) or self._extract_first_video_url(
                normalize_text(ctx.reply_to_text)
            )
            _set_if_empty("url", explicit_video_url or recent_video_url or candidate_url)
            inferred_mode = self._infer_split_video_mode(contextual_query or text)
            if inferred_mode:
                _set_if_empty("mode", inferred_mode)
            time_hints = self._infer_video_time_hints(contextual_query or text)
            mode_now = normalize_text(str(fixed.get("mode", inferred_mode))).lower()
            if mode_now in {"clip", "audio"}:
                if time_hints.get("start") is not None:
                    _set_if_empty("start_seconds", time_hints.get("start"))
                if time_hints.get("end") is not None:
                    _set_if_empty("end_seconds", time_hints.get("end"))
            elif mode_now == "cover":
                if time_hints.get("point") is not None:
                    _set_if_empty("frame_time_seconds", time_hints.get("point"))
            elif mode_now == "frames":
                frame_hint = self._infer_frame_count_hint(contextual_query or text)
                if frame_hint > 0:
                    _set_if_empty("max_frames", frame_hint)
        elif tool_name in {
            "parse_video",
            "analyze_video",
            "fetch_webpage",
            "download_file",
            "smart_download",
        }:
            if tool_name in {"parse_video", "analyze_video"}:
                explicit_video_url = self._extract_first_video_url(text) or self._extract_first_video_url(
                    normalize_text(ctx.reply_to_text)
                )
                _set_if_empty("url", explicit_video_url or recent_video_url or candidate_url)
            elif tool_name == "fetch_webpage":
                _set_if_empty(
                    "url",
                    candidate_url
                    or self._extract_first_web_url(contextual_query or text),
                )
            else:
                _set_if_empty("url", candidate_url)
            if tool_name in {"download_file", "smart_download"}:
                _set_if_empty("query", contextual_query or text)
                _set_if_empty("kind", "auto")
                # upload 是带副作用的动作（往群里传文件），必须由模型显式声明，
                # 见 _missing_required_tool_args。这里只补结构参数：声明了要传，
                # 目标群号从 ctx 结构里取，不再从中文里猜「要不要传」。
                if self._to_declared_flag(fixed.get("upload")):
                    _set_if_empty("group_id", int(ctx.group_id or 0))
        elif tool_name in {"github_search", "douyin_search", "search_knowledge"}:
            _set_if_empty("query", contextual_query or text)
        elif tool_name in {"search_web_media", "search_media"}:
            _set_if_empty("query", contextual_query or text)
        elif tool_name == "resolve_image":
            _set_if_empty(
                "url",
                self._extract_first_image_url(full_text)
                or self._extract_first_url(text)
                or self._extract_first_url(ctx.original_message_text)
                or self._extract_first_url(ctx.reply_to_text),
            )
        elif tool_name == "analyze_local_video":
            explicit_video_url = self._extract_first_video_url(text) or self._extract_first_video_url(
                normalize_text(ctx.reply_to_text)
            )
            _set_if_empty("url", explicit_video_url or recent_video_url or candidate_url)
            _set_if_empty("question", text)
        elif tool_name == "analyze_image":
            _set_if_empty("question", text)
            _set_if_empty("allow_recent_fallback", True)
            # 批量识别只认模型显式声明的 analyze_all（工具侧同样只认它，
            # 见 core/agent_tools_media.py 的 analyze_all 读取）。原先这里按中文词表
            # 把 1 张图的分析悄悄扩成 8 张，模型并不知道自己的调用被改写了。
        elif tool_name == "search_download_resources":
            _set_if_empty("query", contextual_query or text)
            # file_type 由模型自己填：它是缩小搜索面的可选参数，
            # 空着就是全类型搜索，不需要本地从中文里剥后缀。
        elif tool_name == "cli_invoke":
            _set_if_empty("prompt", text)
        elif tool_name == "get_user_info":
            if qq_id:
                existing = self._to_safe_int(fixed.get("user_id"))
                if existing and existing != qq_id:
                    _log.info(
                        "agent_tool_arg_override | trace=%s | tool=%s | field=user_id | old=%s | new=%s",
                        ctx.trace_id,
                        tool_name,
                        existing,
                        qq_id,
                    )
                fixed["user_id"] = qq_id
            else:
                _set_if_empty("user_id", qq_id)
        elif tool_name == "get_message":
            reply_mid = self._to_safe_int(ctx.reply_to_message_id)
            if reply_mid:
                _set_if_empty("message_id", reply_mid)
        elif tool_name == "get_qq_avatar":
            if qq_id:
                _set_if_empty("qq", str(qq_id))
        elif tool_name in {
            "get_qzone_profile",
            "get_qzone_moods",
            "get_qzone_albums",
            "analyze_qzone",
            "get_qzone_photos",
        }:
            if qq_id:
                _set_if_empty("qq_number", str(qq_id))
        # send_emoji / send_sticker 的 query 不再由本地从中文里剥词：
        # 旧实现把「来个表情包」和「发一张猫猫表情」压成同一个 '随机'，
        # 用户真正指定的「猫猫」被丢掉。现在 query 是必填参数（见
        # _missing_required_tool_args），模型自己说要发什么。

        return fixed

    @staticmethod
    def _missing_required_tool_args(tool_name: str, args: dict[str, Any]) -> list[str]:
        """仅对高频失败工具做必填校验，避免空调用。"""
        required: dict[str, list[str]] = {
            "web_search": ["query"],
            "lookup_wiki": ["keyword"],
            "parse_video": ["url"],
            "analyze_video": ["url"],
            "fetch_webpage": ["url"],
            # upload 决定「文件只是下载下来，还是直接传进群里」——是带副作用的动作。
            # 以前由本地词表（"直接发我" 之类）替模型决定，现在必须由模型显式声明。
            "download_file": ["url", "upload"],
            "smart_download": ["url", "upload"],
            "github_search": ["query"],
            "douyin_search": ["query"],
            "search_knowledge": ["query"],
            "search_media": ["query", "media_type"],
            "search_web_media": ["query", "media_type"],
            "search_download_resources": ["query"],
            "cli_invoke": ["prompt"],
            "generate_image": ["prompt"],
            "generate_image_enhanced": ["prompt"],
            "get_user_info": ["user_id"],
            "get_message": ["message_id"],
            "get_qzone_profile": ["qq_number"],
            "get_qzone_moods": ["qq_number"],
            "get_qzone_albums": ["qq_number"],
            "analyze_qzone": ["qq_number"],
            "get_qzone_photos": ["qq_number", "album_id"],
            # query 说明要发哪个表情。以前本地从中文里剥词并在剥不到时填 '随机'，
            # 于是「发一张猫猫表情」丢掉了「猫猫」；现在模型必须自己说。
            "send_emoji": ["query"],
            "send_sticker": ["query"],
        }
        fields = required.get(tool_name, [])
        missing: list[str] = []
        for field in fields:
            val = args.get(field)
            if val is None:
                missing.append(field)
                continue
            # bool 是 int 的子类，下面 `val == 0` 会把 False 判成缺参数。
            # 对布尔型必填参数，False 是模型做出的明确声明（"不要上传"），必须放行。
            if field in _DECLARED_FLAG_ARGS and isinstance(val, bool):
                continue
            if isinstance(val, str) and not normalize_text(val):
                missing.append(field)
                continue
            if isinstance(val, (int, float)) and val == 0:
                missing.append(field)
                continue
            if isinstance(val, (list, dict)) and not val:
                missing.append(field)
        return missing

    @staticmethod
    def _extract_first_url(text: str) -> str:
        return media_utils.extract_first_url(text)

    @classmethod
    def _extract_first_video_url(cls, text: str) -> str:
        url = cls._extract_first_url(text)
        if url and cls._looks_like_video_url(url):
            return url
        return ""

    @classmethod
    def _extract_first_image_url(cls, text: str) -> str:
        for match in _RE_URL_EXTRACT.finditer(text or ""):
            url = media_utils.strip_trailing_url_noise(match.group(0))
            if url and cls._looks_like_image_url(url):
                return url
        return ""

    @classmethod
    def _extract_first_web_url(cls, text: str) -> str:
        explicit = cls._extract_first_url(text)
        if explicit:
            return explicit
        content = normalize_text(text)
        if not content:
            return ""
        match = _RE_BARE_WEB_HOST.search(content)
        if not match:
            return ""
        url = normalize_text(match.group(1)).rstrip(").,，。!?！？")
        if not url:
            return ""
        return f"https://{url}"

    @classmethod
    def _looks_like_webpage_fetch_request(cls, text: str) -> bool:
        url = cls._extract_first_web_url(text)
        if not url:
            return False
        if cls._looks_like_image_url(url) or cls._looks_like_video_url(url):
            return False
        content = normalize_text(text).lower()
        if not content:
            return False
        cues = (
            "网站",
            "网页",
            "页面",
            "官网",
            "打开",
            "看看",
            "看下",
            "帮我看",
            "分析",
            "介绍",
            "是什么",
            "安全吗",
            "看",
            "website",
            "webpage",
            "site",
            "page",
        )
        return any(cue in content for cue in cues)

    @staticmethod
    def _looks_like_image_url(url: str) -> bool:
        return media_utils.looks_like_image_url(url)

    @classmethod
    def _text_has_image_hint(cls, text: str) -> bool:
        norm = normalize_text(text).lower()
        if not norm:
            return False
        if "image:" in norm:
            return True
        url = cls._extract_first_url(norm)
        return bool(url and cls._looks_like_image_url(url))

    @staticmethod
    def _looks_like_video_url(url: str) -> bool:
        return media_utils.looks_like_video_url(url)

    @classmethod
    def _extract_recent_media_url(cls, ctx: AgentContext, media_type: str) -> str:
        wanted = normalize_text(media_type).lower()
        artifact = ctx.recent_media_artifact if isinstance(ctx.recent_media_artifact, dict) else {}
        if artifact:
            if wanted == "video":
                for key in ("video_url", "video_file", "path", "url"):
                    value = normalize_text(str(artifact.get(key, "")))
                    if value:
                        return value
            if wanted == "image":
                value = normalize_text(str(artifact.get("image_url", "")))
                if value:
                    return value
                image_urls = artifact.get("image_urls", [])
                if isinstance(image_urls, list):
                    for item in image_urls:
                        value = normalize_text(str(item))
                        if value:
                            return value
        summary_rows = list(ctx.reply_media_summary or []) + list(
            ctx.media_summary or []
        )
        for row in summary_rows:
            text = normalize_text(row)
            if not text:
                continue
            if wanted == "video" and not text.startswith("video:"):
                continue
            if wanted == "image" and not text.startswith("image:"):
                continue
            if wanted == "audio" and not (
                text.startswith("audio:") or text.startswith("record:")
            ):
                continue
            url = cls._extract_first_url(text)
            if url:
                return url
        recent_rows = list(ctx.memory_context or []) + list(ctx.related_memories or [])
        for row in reversed(recent_rows):
            text = normalize_text(row)
            if not text:
                continue
            url = cls._extract_first_url(text)
            if not url:
                continue
            if wanted == "video" and cls._looks_like_video_url(url):
                return url
            if wanted != "video":
                return url
        return ""

    @staticmethod
    def _looks_like_reference_to_previous_link(text: str) -> bool:
        t = normalize_text(text).lower()
        if not t:
            return False
        plain = _RE_WHITESPACE.sub("", t)
        explicit_tokens = (
            "/source",
            "source=previous",
            "source=last",
            "from=previous",
            "from=last",
            "use_previous_url=1",
            "use_last_url=1",
        )
        if any(token in plain for token in explicit_tokens):
            return True
        patterns = (
            r"(?:^|\s)/source(?:\s|$)",
            r"(?:^|\s)(?:source|from)\s*=\s*(?:previous|last)(?:\s|$)",
        )
        return any(re.search(pattern, t) for pattern in patterns)

    @staticmethod
    def _to_declared_flag(value: Any) -> bool:
        """把模型声明的布尔参数读成 bool。

        模型可能给真 bool，也可能给 "true"/"false" 字符串；后者用 bool() 判断会把
        "false" 当真。这里只做类型解析，不读用户原文，因此不是意图猜测。
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return normalize_text(str(value or "")).lower() in {"true", "1", "yes", "y", "on"}

    @staticmethod
    def _to_safe_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        text = normalize_text(str(value))
        if not text or not re.fullmatch(r"-?\d+", text):
            return 0
        try:
            return int(text)
        except ValueError:
            return 0

    def _extract_candidate_qq_id(self, ctx: AgentContext) -> int:
        # 1) 优先当前消息中 @ 的目标（且不是 bot 自己）
        for seg in ctx.raw_segments:
            if not isinstance(seg, dict):
                continue
            if normalize_text(str(seg.get("type", ""))).lower() != "at":
                continue
            data = seg.get("data", {})
            if not isinstance(data, dict):
                continue
            qq = normalize_text(str(data.get("qq", "")))
            if not qq or qq == str(ctx.bot_id):
                continue
            if re.fullmatch(r"[1-9]\d{5,11}", qq):
                return int(qq)

        # 2) 其次是 reply 目标（引用了谁）
        reply_uid = normalize_text(str(ctx.reply_to_user_id))
        if (
            reply_uid
            and reply_uid != str(ctx.bot_id)
            and re.fullmatch(r"[1-9]\d{5,11}", reply_uid)
        ):
            return int(reply_uid)

        # 3) 最后才回退到正文数字（避免截断数字抢占）
        text = normalize_text(ctx.message_text)
        m = _RE_QQ_NUMBER.search(text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return 0

    @staticmethod
    def _infer_lookup_keyword(text: str) -> str:
        t = normalize_text(text)
        if not t:
            return ""
        t = re.sub(r"^(?i:/(?:lookup|wiki))\s*", "", t)
        t = re.sub(r"^(?i:keyword)\s*=\s*", "", t)
        t = _RE_PUNCTUATION_CJK.sub(" ", t)
        t = _RE_WHITESPACE.sub(" ", t).strip()
        return t[:80]

    @staticmethod
    def _infer_split_video_mode(text: str) -> str:
        t = normalize_text(text).lower()
        if not t:
            return ""
        plain = _RE_WHITESPACE.sub("", t)
        if "mode=audio" in plain:
            return "audio"
        if "mode=cover" in plain:
            return "cover"
        if "mode=frames" in plain or "mode=frame" in plain:
            return "frames"
        if "mode=clip" in plain or re.search(
            r"\b\d+(?:\.\d+)?\s*(?:s|sec|seconds?)\s*-\s*\d+(?:\.\d+)?\s*(?:s|sec|seconds?)\b",
            t,
        ):
            return "clip"
        return ""

    @staticmethod
    def _parse_time_token_to_seconds(token: str) -> float | None:
        raw = normalize_text(token).lower()
        if not raw:
            return None
        clock = re.fullmatch(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", raw)
        if clock:
            h_or_m = int(clock.group(1))
            m_or_s = int(clock.group(2))
            sec_part = clock.group(3)
            if sec_part is None:
                return float(max(0, h_or_m * 60 + m_or_s))
            return float(max(0, h_or_m * 3600 + m_or_s * 60 + int(sec_part)))
        second = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:秒|s)?", raw)
        if second:
            try:
                return max(0.0, float(second.group(1)))
            except ValueError:
                return None
        return None

    @classmethod
    def _infer_video_time_hints(cls, text: str) -> dict[str, float]:
        t = normalize_text(text).lower()
        if not t:
            return {}

        range_patterns = (
            r"(\d{1,2}:\d{1,2}(?::\d{1,2})?|\d+(?:\.\d+)?\s*(?:秒|s))\s*(?:-|—|–|~|～|to)\s*(\d{1,2}:\d{1,2}(?::\d{1,2})?|\d+(?:\.\d+)?\s*(?:秒|s))",
        )
        for pattern in range_patterns:
            m = re.search(pattern, t)
            if not m:
                continue
            start = cls._parse_time_token_to_seconds(m.group(1))
            end = cls._parse_time_token_to_seconds(m.group(2))
            if start is not None and end is not None and end > start:
                return {"start": start, "end": end}

        if not re.search(r"(?:秒|\bs\b|\d{1,2}:\d{1,2})", t):
            return {}
        first_token = re.search(r"\d{1,2}:\d{1,2}(?::\d{1,2})?|\d+(?:\.\d+)?", t)
        if first_token:
            sec = cls._parse_time_token_to_seconds(first_token.group(0))
            if sec is not None:
                return {"point": sec}
        return {}

    @staticmethod
    def _infer_frame_count_hint(text: str) -> int:
        t = normalize_text(text)
        if not t:
            return 0
        m = re.search(
            r"(?:max_frames|frame_count)\s*=\s*(\d{1,2})", t, flags=re.IGNORECASE
        )
        if not m:
            m = re.search(
                r"(\d{1,2})\s*(?:screenshots?|frames?)", t, flags=re.IGNORECASE
            )
        if not m:
            m = re.search(r"(\d{1,2})\s*(?:张|幀|帧)", t, flags=re.IGNORECASE)
        if not m:
            return 0
        try:
            value = int(m.group(1))
        except ValueError:
            return 0
        return max(1, min(12, value))

    def _rebuild_query_with_context(self, text: str, ctx: AgentContext) -> str:
        raw = normalize_text(text)
        if not raw:
            return ""
        if not self._is_context_continuation_phrase(raw):
            return raw
        tail = self._strip_continuation_prefix(raw)
        # 优先使用被引用消息的正文作为语境锚点，解决 QQ reply 场景下指代丢失。
        # 但如果引用的是 bot 自己的消息，不要用 bot 的回复作为 topic（避免自我迷惑）。
        reply_uid = normalize_text(str(ctx.reply_to_user_id))
        is_reply_to_bot = reply_uid == str(ctx.bot_id)
        topic = ""
        if not is_reply_to_bot:
            topic = self._extract_topic_from_reply_text(ctx.reply_to_text)
        if not topic:
            topic = self._extract_recent_topic(ctx, current_text=raw)
        if topic and tail:
            if tail in topic:
                return topic
            if topic in tail:
                return tail
            return f"{topic} {tail}".strip()
        if topic:
            return topic
        return tail or raw

    @staticmethod
    def _is_context_continuation_phrase(text: str) -> bool:
        t = normalize_text(text).lower()
        if not t:
            return False
        plain = _RE_WHITESPACE.sub("", t)
        explicit_tokens = ("/next", "next=1", "continue=1", "context=continue")
        if any(token in plain for token in explicit_tokens):
            return True
        if len(t) <= 16 and re.fullmatch(r"[?？!！,，.。~\-\s]*", t):
            return True
        if len(t) <= 12 and any(
            cue in t
            for cue in (
                "继续找",
                "你找",
                "找啊",
                "查啊",
                "搜啊",
                "去找",
                "那你找",
            )
        ):
            return True
        return False

    @staticmethod
    def _strip_continuation_prefix(text: str) -> str:
        t = normalize_text(text)
        t = re.sub(r"^(?i:/(?:next|continue))\s*[?？:：,，]?\s*", "", t)
        t = normalize_text(t)
        return t

    def _extract_recent_topic(self, ctx: AgentContext, current_text: str) -> str:
        current = normalize_text(current_text)
        rows = list(ctx.memory_context or [])
        for line in reversed(rows):
            row = normalize_text(line)
            if not row:
                continue
            if row.startswith("[bot]"):
                continue
            while row.startswith("["):
                close = row.find("]")
                if close <= 0:
                    break
                row = normalize_text(row[close + 1 :])
            if not row or row == current:
                continue
            if self._is_context_continuation_phrase(row):
                continue
            cleaned = re.sub(
                r"^(帮我|给我|请|麻烦|你去|你帮我|我想看|我要看|想看|看一下|看下|我想|我要|搜一下|搜索|查一下|查下|找一下|找)\s*",
                "",
                row,
            ).strip()
            topic = cleaned or row
            if len(topic) < 2:
                continue
            return topic[:80]
        return ""

    @staticmethod
    def _extract_topic_from_reply_text(reply_text: str) -> str:
        text = normalize_text(reply_text)
        if not text:
            return ""
        text = _RE_URL_STRIP.sub(" ", text)
        text = normalize_text(text)
        if not text:
            return ""
        return clip_text(text, 100)

    def _resolve_tool_timeout_seconds(self, tool_name: str, has_media: bool) -> float:
        heavy_tools = {
            "parse_video",
            "analyze_video",
            "analyze_local_video",
            "split_video",
            "fetch_webpage",
            "download_file",
            "smart_download",
            "analyze_image",
            "scrape_extract",
            "scrape_summarize",
            "scrape_structured",
            "scrape_follow_links",
            "music_play",
            "music_play_by_id",
            "bilibili_audio_extract",
        }
        if tool_name in {"bilibili_audio_extract", "music_play_by_id"}:
            return float(max(float(self.tool_timeout_seconds_media), 70.0))
        if tool_name == "music_play":
            return float(max(float(self.tool_timeout_seconds_media), 55.0))
        if tool_name == "parse_video":
            return float(max(float(self.tool_timeout_seconds_media), 120.0))
        if tool_name == "search_media":
            return float(max(float(self.tool_timeout_seconds_media), 120.0))
        if has_media or tool_name in heavy_tools:
            return float(self.tool_timeout_seconds_media)
        return float(self.tool_timeout_seconds)

    def estimate_total_timeout_seconds(
        self, ctx: AgentContext, has_media: bool
    ) -> float:
        """公开给外层编排器使用的超时预算估算。"""
        return self._resolve_total_timeout_seconds(ctx, has_media)

    def _resolve_total_timeout_seconds(
        self, ctx: AgentContext, has_media: bool
    ) -> float:
        per_step_timeout = 35 if has_media else 30
        total_timeout = float(max(12, self.max_steps * per_step_timeout))
        if self.total_timeout_seconds > 0:
            total_timeout = min(total_timeout, float(self.total_timeout_seconds))

        queue_cfg = (
            self.config.get("queue", {}) if isinstance(self.config, dict) else {}
        )
        if isinstance(queue_cfg, dict):
            queue_timeout = self._to_safe_int(
                queue_cfg.get("process_timeout_seconds", queue_cfg.get("timeout_seconds"))
            )
            text = normalize_text(ctx.message_text).lower()
            web_override = self._to_safe_int(
                queue_cfg.get("web_process_timeout_seconds")
            )
            video_override = self._to_safe_int(
                queue_cfg.get("video_process_timeout_seconds")
            )
            download_override = self._to_safe_int(
                queue_cfg.get("download_process_timeout_seconds")
            )
            if any(
                token in text
                for token in ("下载", "安装包", ".exe", ".apk", ".zip", "网盘")
            ):
                queue_timeout = max(queue_timeout, download_override)
            elif has_media or any(
                token in text
                for token in ("视频", "解析", "bilibili", "抖音", "快手", "acfun", "腾讯视频", "v.qq.com", "bv")
            ):
                queue_timeout = max(queue_timeout, video_override)
            elif self._looks_like_webpage_fetch_request(text):
                queue_timeout = max(queue_timeout, web_override)

            if queue_timeout > 0:
                queue_budget = max(
                    15, queue_timeout - self.queue_timeout_margin_seconds
                )
                total_timeout = min(total_timeout, float(queue_budget))

        return max(12.0, total_timeout)

    @staticmethod
    def _build_external_fact_signature(tool_name: str, args: dict[str, Any]) -> str:
        if not isinstance(args, dict):
            return ""
        fields = [
            "query",
            "url",
            "repo",
            "instruction",
            "schema_desc",
            "mode",
            "keyword",
            "media_type",
        ]
        parts = [tool_name]
        for key in fields:
            value = normalize_text(str(args.get(key, ""))).lower()
            if value:
                parts.append(f"{key}={clip_text(value, 180)}")
        return "|".join(parts) if len(parts) > 1 else ""

    def _looks_like_choice_followup(self, text: str) -> bool:
        _ = text
        # 快捷跟进链路已下线，统一交给常规意图理解和工具调用。
        return False

    def _looks_like_profile_analysis_request(self, text: str) -> bool:
        return _shared_qq_profile_request(text, config=self.config)

    @staticmethod
    def _looks_like_image_question(text: str) -> bool:
        """Weak check: does the text ask about an image?

        Chinese keyword matching removed. Only explicit control tokens accepted.
        Image pipeline is driven by raw_segments / URL structural signals.
        """
        t = (text or "").lower()
        # Only accept explicit control tokens
        if any(tok in t for tok in ("/analyze", "mode=analyze", "ocr=true")):
            return True
        return False

    def _append_tool_result(
        self,
        messages: list[dict[str, Any]],
        parsed: dict[str, Any] | None,
        assistant_msg: dict[str, Any],
        response_text: str,
        tool_result: dict[str, Any],
    ) -> None:
        # 安全截断，避免返回数据撑爆 Context Window
        safe_result = dict(tool_result)
        if "display" in safe_result and isinstance(safe_result["display"], str):
            display_text = safe_result["display"]
            # 针对爬虫等重型数据返回放宽截断限制
            max_len = 12000 if "scrape" in str(safe_result.get("tool", "")) else 3000
            if len(display_text) > max_len:
                safe_result["display"] = display_text[:max_len] + f"\n\n...[已强制截断 {len(display_text) - max_len} 字符，防止 Token 超限]"
        if "error" in safe_result and isinstance(safe_result["error"], str):
            error_text = safe_result["error"]
            if len(error_text) > 1000:
                safe_result["error"] = error_text[:1000] + "...[错误信息过长已截断]"
        if parsed and "id" in parsed:
            if not messages or messages[-1] is not assistant_msg:
                messages.append(assistant_msg)
            
            # Handle the primary parsed tool call
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": parsed["id"],
                    "content": json.dumps({"tool_result": safe_result}, ensure_ascii=False),
                }
            )
            
            # Handle any extra ignored tool calls to satisfy OpenAI API requirement
            tool_calls = assistant_msg.get("tool_calls", [])
            for tc in tool_calls:
                tc_id = tc.get("id")
                if tc_id and tc_id != parsed["id"]:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps({"error": "ignored_extra_tool_call"}, ensure_ascii=False),
                        }
                    )
        else:
            messages.append({"role": "assistant", "content": response_text})
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps({"tool_result": safe_result}, ensure_ascii=False),
                }
            )

    def _parse_llm_output(self, text: str) -> dict[str, Any] | None:
        """解析 LLM 输出为 tool_call dict，失败返回 None。"""
        clean = text.strip()

        # 先剥离 <thinking>...</thinking> 块（LLM 可能在 tool call 前输出思考）
        clean = _RE_THINKING_BLOCK.sub("", clean)
        clean = _RE_THINKING_TAG.sub("", clean)
        # 剥离 <tool_call>...</tool_call> 包裹（保留内部 JSON）
        clean = _RE_TOOL_CALL_TAG.sub("", clean)

        # 兼容 <tool_use> tool_name {"arg":"val"} </tool_use> 格式
        tool_use_match = re.search(
            r"<tool_use>\s*(\w+)\s*(\{.*?\})\s*</tool_use>",
            clean,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if tool_use_match:
            tool_name = tool_use_match.group(1).strip()
            try:
                tool_args = json.loads(tool_use_match.group(2))
                if isinstance(tool_args, dict) and tool_name:
                    return {"tool": tool_name, "args": tool_args}
            except (json.JSONDecodeError, ValueError):
                pass
        # 剥离残留的 <tool_use> 标签
        clean = _RE_TOOL_USE_TAG.sub("", clean)

        # 兼容 [tool_use: tool_name] key: value 格式
        bracket_match = re.search(
            r"\[tool_use:\s*(\w+)\]\s*(.*)",
            clean,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if bracket_match:
            tool_name = bracket_match.group(1).strip()
            rest = bracket_match.group(2).strip()
            if tool_name:
                # 尝试解析 key: value 对
                args: dict[str, Any] = {}
                for kv_match in re.finditer(r"(\w+)\s*[:=]\s*(\S+)", rest):
                    args[kv_match.group(1)] = kv_match.group(2)
                return {"tool": tool_name, "args": args}

        # 兼容 [tool_call(tool_name, key="value")] 格式
        call_match = re.search(
            r"\[tool_call\(\s*(\w+)\s*,\s*(.*?)\)\]",
            clean,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if call_match:
            tool_name = call_match.group(1).strip()
            params_str = call_match.group(2).strip()
            if tool_name:
                args = {}
                for kv_match in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', params_str):
                    args[kv_match.group(1)] = kv_match.group(2)
                return {"tool": tool_name, "args": args}

        clean = clean.strip()

        # 尝试直接 JSON 解析
        try:
            data = json.loads(clean)
            if isinstance(data, dict) and "tool" in data:
                return data
            # 兼容 OpenAI function calling 格式: {"name": "tool", "arguments": {...}}
            if isinstance(data, dict) and "name" in data:
                return {
                    "tool": data["name"],
                    "args": data.get("arguments", data.get("args", {})),
                }
        except (json.JSONDecodeError, ValueError):
            pass

        # 检测多个 JSON 对象拼接: {"tool":"think",...} {"tool":"xxx",...}
        # 用括号计数找到第一个完整 JSON 对象的结束位置
        if clean.startswith("{") and clean.count("{") > clean.count("}"):
            pass  # 不完整 JSON，跳过
        elif clean.startswith("{"):
            end = self._find_json_end(clean)
            if end is not None and end < len(clean) - 1:
                first_json = clean[: end + 1]
                try:
                    data = json.loads(first_json)
                    norm = self._normalize_tool_call(data)
                    if norm:
                        _log.debug(
                            "parse_multi_json | picked first of concatenated objects"
                        )
                        return norm
                except (json.JSONDecodeError, ValueError):
                    pass

        # 尝试从 markdown code block 中提取
        code_match = _RE_CODE_BLOCK.search(clean)
        if code_match:
            code_content = code_match.group(1).strip()
            try:
                data = json.loads(code_content)
                norm = self._normalize_tool_call(data)
                if norm:
                    return norm
            except (json.JSONDecodeError, ValueError):
                # code block 内 JSON 解析失败，尝试恢复（中文引号等）
                recovered = self._try_recover_tool_call(code_content)
                if recovered:
                    return recovered

        # 尝试找到第一个 { 和最后一个 }
        first_brace = clean.find("{")
        last_brace = clean.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            candidate = clean[first_brace : last_brace + 1]
            try:
                data = json.loads(candidate)
                norm = self._normalize_tool_call(data)
                if norm:
                    return norm
            except (json.JSONDecodeError, ValueError):
                # 花括号提取的 JSON 解析失败，尝试恢复
                recovered = self._try_recover_tool_call(candidate)
                if recovered:
                    return recovered

        # 如果 fallback 开启，把纯文本当作 final_answer
        if self.fallback_on_parse_error and clean:
            # 如果内容看起来像 JSON tool_call 但解析失败了
            if clean.startswith("{") and ('"tool"' in clean or '"name"' in clean):
                # 尝试修复常见 JSON 问题 (中文引号、未转义引号、截断)
                recovered = self._try_recover_tool_call(clean)
                if recovered:
                    return recovered
                _log.warning("agent_parse_fail_json_like | content=%s", clean[:200])
                return {
                    "tool": "think",
                    "args": {"thought": "我的上一次输出格式有误，让我重新组织回复"},
                }
            if not clean.startswith("{"):
                return {"tool": "final_answer", "args": {"text": clean}}

        return None

    @staticmethod
    def _normalize_tool_call(data: Any) -> dict[str, Any] | None:
        """将不同格式的 tool call 统一为 {"tool": ..., "args": ...}。

        支持的格式:
        - 标准: {"tool": "...", "args": {...}}
        - OpenAI: {"name": "...", "arguments": {...}}
        - 弱模型: {"function": "...", "parameters": {...}}
        - 弱模型: {"action": "...", "input": {...}}
        """
        if not isinstance(data, dict):
            return None
        if "tool" in data:
            return data
        # OpenAI function calling 格式: {"name": "tool", "arguments": {...}}
        if "name" in data:
            return {
                "tool": data["name"],
                "args": data.get("arguments", data.get("args", data.get("parameters", {}))),
            }
        # 弱模型常见: {"function": "tool", "parameters": {...}}
        if "function" in data and isinstance(data["function"], str):
            return {
                "tool": data["function"],
                "args": data.get("parameters", data.get("arguments", data.get("args", {}))),
            }
        # 弱模型常见: {"action": "tool", "input": {...}}
        if "action" in data and isinstance(data["action"], str):
            return {
                "tool": data["action"],
                "args": data.get("input", data.get("parameters", data.get("args", {}))),
            }
        return None

    @staticmethod
    def _find_json_end(text: str) -> int | None:
        """找到第一个完整 JSON 对象的结束位置 (括号匹配)。"""
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        return None

    @classmethod
    def _trim_recovered_final_answer_text(cls, content: str) -> str:
        """清理 final_answer.text 的恢复候选，避免把后续字段名拼进正文。"""
        candidate = str(content or "")
        if not candidate:
            return ""

        # 截断掉常见的后续字段开头（例如: ","image_url":）
        field_tail = re.search(
            r'(?:(?<!\\)"\s*,\s*"(?:image_url|image_urls|video_url|audio_file|cover_url|record_b64|pre_ack|action|reason)"\s*:|\\",\\\"(?:image_url|image_urls|video_url|audio_file|cover_url|record_b64|pre_ack|action|reason)\\\"\s*:)',
            candidate,
            flags=re.IGNORECASE,
        )
        if field_tail:
            candidate = candidate[: field_tail.start()]

        # 去掉尾部闭合残片与空白
        candidate = re.sub(r'"\s*\}\s*\}\s*$', "", candidate)
        candidate = candidate.rstrip('"}\n\r\t ')

        # 优先按 JSON 字符串反转义，失败再做最小替换
        try:
            candidate = str(json.loads(f'"{candidate}"'))
        except Exception:
            candidate = (
                candidate.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
            )

        return normalize_text(candidate)

    def _try_recover_tool_call(self, text: str) -> dict[str, Any] | None:
        """尝试从格式有误的 JSON 中恢复 tool_call。

        常见问题:
        - LLM 输出被截断 (不完整的 JSON)
        - text 值中包含未转义的引号
        - 中文引号 \u201c\u201d 混入 JSON 结构
        """
        # 1. 替换中文引号为英文引号后重试
        fixed = text.replace("\u201c", '"').replace("\u201d", '"')
        fixed = fixed.replace("\u2018", "'").replace("\u2019", "'")
        try:
            data = json.loads(fixed)
            norm = self._normalize_tool_call(data)
            if norm:
                return norm
        except (json.JSONDecodeError, ValueError):
            pass

        # 2. 对 final_answer 用正则提取 text 内容，兼容 {"tool":...} / {"name":...}
        m = _RE_FINAL_ANSWER_KEY.search(text)
        if m:
            # 找到 "text" : " 之后的所有内容，去掉尾部的 "}} 等
            tm = _RE_TEXT_KEY.search(text)
            if tm:
                start = tm.end()
                content = self._trim_recovered_final_answer_text(text[start:])
                if content:
                    return {"tool": "final_answer", "args": {"text": content}}

        # 3. 对其他工具，尝试截断修复 (补全 } )
        first_brace = text.find("{")
        if first_brace >= 0:
            candidate = text[first_brace:]
            open_count = candidate.count("{") - candidate.count("}")
            if open_count > 0:
                candidate += "}" * open_count
                try:
                    data = json.loads(candidate)
                    norm = self._normalize_tool_call(data)
                    if norm:
                        return norm
                except (json.JSONDecodeError, ValueError):
                    pass

        return None

    def _compact_data(
        self, data: dict[str, Any], max_items: int = 20
    ) -> dict[str, Any]:
        """压缩工具返回数据，避免 token 爆炸。"""
        result = {}
        for key, value in data.items():
            if isinstance(value, list):
                result[key] = value[:max_items]
                if len(value) > max_items:
                    result[f"{key}_total"] = len(value)
            elif isinstance(value, str) and len(value) > 1000:
                result[key] = value[:1000] + "..."
            else:
                result[key] = value
        return result

    @staticmethod
    def _real_tool_failure_count(steps: list[dict[str, Any]]) -> int:
        """本回合真正失败/被拦的**外部**工具步数量。

        纯结构判定，不看文本语义：
        - `_INTERNAL_STEP_TOOLS` 里的编排步一律不算（它们没有 `ok` 字段，
          算进去会让每一轮都像是有失败）；
        - 有 `ok` 字段就以它为准；
        - 没有 `ok` 但带 `error` / `blocked` 的（权限拦截、unknown_tool 等）
          说明这次工具没跑成，算失败。
        """
        failures = 0
        for step in steps:
            if not isinstance(step, dict):
                continue
            tool_name = normalize_text(str(step.get("tool", ""))).lower()
            if not tool_name or tool_name in _INTERNAL_STEP_TOOLS:
                continue
            if "ok" in step:
                if not bool(step.get("ok")):
                    failures += 1
                continue
            if step.get("error") or step.get("blocked"):
                failures += 1
        return failures

    @staticmethod
    def _last_success_display(steps: list[dict[str, Any]]) -> str:
        for step in reversed(steps):
            if not bool(step.get("ok")):
                continue
            tool_name = normalize_text(str(step.get("tool", ""))).lower()
            if tool_name in _INTERNAL_STEP_TOOLS:
                continue
            display = normalize_text(str(step.get("display", "")))
            if display:
                return display
        return ""

    @staticmethod
    def _last_success_audio_file(
        steps: list[dict[str, Any]], prefer_non_silk: bool = False
    ) -> str:
        for step in reversed(steps):
            if not bool(step.get("ok")):
                continue
            data = step.get("data", {})
            if not isinstance(data, dict):
                continue
            for key in ("audio_file", "audio_path", "audio_file_silk", "silk_path"):
                path = normalize_text(str(data.get(key, "")))
                if path:
                    if prefer_non_silk and path.lower().endswith(".silk"):
                        continue
                    return path
        return ""

    @staticmethod
    def _last_success_video_url(steps: list[dict[str, Any]]) -> str:
        for step in reversed(steps):
            if not bool(step.get("ok")):
                continue
            data = step.get("data", {})
            if not isinstance(data, dict):
                continue
            for key in ("video_url", "video_file", "video_path", "file_path", "path"):
                path = normalize_text(str(data.get(key, "")))
                if path:
                    return path
        return ""

    @staticmethod
    def _last_success_image_urls(steps: list[dict[str, Any]]) -> list[str]:
        for step in reversed(steps):
            if not bool(step.get("ok")):
                continue
            data = step.get("data", {})
            if not isinstance(data, dict):
                continue
            urls: list[str] = []
            image_urls = data.get("image_urls", [])
            if isinstance(image_urls, list):
                for item in image_urls:
                    url = normalize_text(str(item))
                    if url and url not in urls:
                        urls.append(url)
            image_url = normalize_text(str(data.get("image_url", "")))
            if image_url and image_url not in urls:
                urls.insert(0, image_url)
            if urls:
                return urls
        return []

    @classmethod
    def _sanitize_final_text_for_local_media(cls, text: str, media_path: str) -> str:
        content = str(text or "").strip()
        path = normalize_text(media_path)
        if not content or not path or not cls._is_local_media_path(path):
            return content
        delivery_text = "解析好了，正在投递视频。"
        lower_content = content.lower()
        contradiction_markers = (
            "没有“发送视频/上传文件”",
            "没有发送视频",
            "没有上传文件",
            "没有发送类工具",
            "没有发送工具",
            "没有上传工具",
            "没法真的",
            "没法直接",
            "没法发",
            "不能直接",
            "无法直接",
            "只能给你路径",
            "只能把路径",
            "qq 侧可能不预览",
            "qq里可能不能",
            "qq 里可能不能",
            "不是平台 cdn 直链",
            "非平台 cdn 直链",
        )
        has_local_path = bool(_RE_LOCAL_FILE_REF.search(content))
        has_contradiction = any(marker in lower_content for marker in contradiction_markers)
        has_delivery_noise = (
            "直链文件" in lower_content
            or ("cdn" in lower_content and "非平台" in lower_content)
            or ("qq" in lower_content and "预览" in lower_content)
        )
        if has_local_path and (has_contradiction or has_delivery_noise):
            return delivery_text
        slash_path = path.replace("\\", "/")
        variants = {
            path,
            slash_path,
            path.replace("/", "\\"),
            f"file://{path}",
            f"file://{slash_path}",
            f"`{path}`",
            f"`{slash_path}`",
            f"`file://{path}`",
            f"`file://{slash_path}`",
        }
        cleaned = content
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                cleaned = cleaned.replace(variant, "视频文件")
        cleaned = _RE_LOCAL_FILE_REF.sub("视频文件", cleaned)
        cleaned = re.sub(
            r"(?:直链|路径|本地缓存文件路径)\s*(?:在这|是)?\s*[:：]?\s*`?视频文件`?",
            delivery_text,
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"`?视频文件`?\s*`?视频文件`?", "视频文件", cleaned)
        cleaned = normalize_text(cleaned)
        if not cleaned or ("视频文件" in cleaned and has_contradiction):
            return delivery_text
        return cleaned or content

    def _extract_embedded_tool_call_from_text(self, text: str) -> dict[str, Any] | None:
        """从 final_answer 文本中恢复误包裹的工具调用 JSON。"""
        clean = normalize_text(text)
        if not clean:
            return None

        candidates = [clean]
        for block in re.findall(
            r"```(?:json)?\s*(.*?)```", clean, flags=re.DOTALL | re.IGNORECASE
        ):
            block_clean = normalize_text(block)
            if block_clean:
                candidates.append(block_clean)

        for candidate in candidates:
            xml_parsed = self._parse_embedded_invoke_payload(candidate)
            if xml_parsed:
                return xml_parsed
            parsed = self._parse_embedded_tool_payload(candidate)
            if parsed:
                return parsed
            recovered = self._try_recover_tool_call(candidate)
            if recovered:
                return recovered
            first_brace = candidate.find("{")
            last_brace = candidate.rfind("}")
            if first_brace >= 0 and last_brace > first_brace:
                parsed = self._parse_embedded_tool_payload(
                    candidate[first_brace : last_brace + 1]
                )
                if parsed:
                    return parsed
                recovered = self._try_recover_tool_call(
                    candidate[first_brace : last_brace + 1]
                )
                if recovered:
                    return recovered
        return None

    @staticmethod
    def _looks_like_embedded_tool_payload_text(text: str) -> bool:
        """识别明显的工具调用泄漏片段，即使内容已截断或 JSON 不合法。"""
        content = normalize_text(text)
        if not content:
            return False
        if re.search(
            r"</?\s*(function_calls?|invoke|parameter)\b", content, flags=re.IGNORECASE
        ):
            return True
        patterns = (
            r"```(?:json)?\s*\{(?=[\s\S]*?\"(?:name|tool)\"\s*:\s*\"[a-zA-Z0-9_.-]+\")(?:[\s\S]*?\"(?:args|arguments|tool_arguments)\"\s*:)[\s\S]*?(?:```|$)",
            r"^\{\s*\"(?:name|tool)\"\s*:\s*\"[a-zA-Z0-9_.-]+\"(?=[\s\S]*?\"(?:args|arguments|tool_arguments)\"\s*:)[\s\S]*$",
        )
        return any(
            re.search(pattern, content, flags=re.DOTALL | re.IGNORECASE)
            for pattern in patterns
        )

    @staticmethod
    def _normalize_embedded_tool_name(name: str) -> str:
        value = normalize_text(name)
        if not value:
            return ""
        lowered = value.lower()
        alias_map = {
            "search_web": "web_search",
            "web.search": "web_search",
            "websearch": "web_search",
            "analyzeimage": "analyze_image",
            "fetchurl": "fetch_url",
        }
        return alias_map.get(lowered, value)

    def _parse_embedded_invoke_payload(self, payload: str) -> dict[str, Any] | None:
        """兼容模型输出的 XML 风格函数调用:
        <function_calls><invoke name="web_search"><parameter name="query">...</parameter></invoke></function_calls>
        """
        text = normalize_text(payload)
        if not text:
            return None
        invoke_match = re.search(
            r"<invoke\s+name=[\"'](?P<tool>[a-zA-Z0-9_.-]+)[\"'][^>]*>(?P<body>.*?)</invoke>",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if not invoke_match:
            return None
        tool_name = self._normalize_embedded_tool_name(invoke_match.group("tool"))
        if not tool_name:
            return None
        body = invoke_match.group("body") or ""
        args: dict[str, Any] = {}
        for param in re.finditer(
            r"<parameter\s+name=[\"'](?P<key>[^\"']+)[\"'][^>]*>(?P<value>.*?)</parameter>",
            body,
            flags=re.DOTALL | re.IGNORECASE,
        ):
            key = normalize_text(param.group("key"))
            value_raw = param.group("value") or ""
            value = normalize_text(re.sub(r"<[^>]+>", "", value_raw))
            if key and value:
                args[key] = value
        return {"tool": tool_name, "args": args}

    def _parse_embedded_tool_payload(self, payload: str) -> dict[str, Any] | None:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        # 兼容 {"tool_uses":[{"tool_name":"...","tool_arguments":{...}}]}
        tool_uses = data.get("tool_uses")
        if isinstance(tool_uses, list) and tool_uses:
            first = tool_uses[0]
            if isinstance(first, dict):
                name = first.get("tool_name") or first.get("name") or first.get("tool")
                args = first.get(
                    "tool_arguments", first.get("arguments", first.get("args", {}))
                )
                if isinstance(name, str) and name.strip():
                    return {
                        "tool": name.strip(),
                        "args": args if isinstance(args, dict) else {},
                    }

        # 兼容 {"tool_name":"...","tool_arguments":{...}}
        name = data.get("tool_name") or data.get("name") or data.get("tool")
        if isinstance(name, str) and name.strip():
            args = data.get(
                "tool_arguments", data.get("arguments", data.get("args", {}))
            )
            return {
                "tool": name.strip(),
                "args": args if isinstance(args, dict) else {},
            }

        return None

    def _undirected_silent_result(
        self,
        ctx: AgentContext,
        steps: list[dict[str, Any]],
        tool_calls_made: int,
        t0: float,
        reason: str,
    ) -> AgentResult:
        """非指向轮次的兜底出口：只记日志，不外发任何文本或媒体。"""
        _log.info(
            "agent_undirected_fallback_silent | trace=%s | reason=%s | steps=%d",
            ctx.trace_id,
            reason,
            len(steps),
        )
        return AgentResult(
            reply_text="",
            action="reply",
            reason="agent_undirected_silent",
            tool_calls_made=tool_calls_made,
            total_time_ms=self._elapsed(t0),
            steps=steps,
        )

    async def _build_fallback_result(
        self,
        ctx: AgentContext,
        steps: list[dict[str, Any]],
        tool_calls_made: int,
        t0: float,
        reason: str,
    ) -> AgentResult:
        """从已有步骤中提取最佳回复作为兜底。"""
        # 旁听探测轮的兜底一律不外发：没人跟机器人说话，内部故障文案更没理由进群。
        if not self._is_directed_turn(ctx):
            return self._undirected_silent_result(
                ctx, steps, tool_calls_made, t0, reason
            )
        # 找最后一个可直接面向用户展示的步骤。
        for step in reversed(steps):
            display = normalize_text(str(step.get("display", "")))
            if not display or not bool(step.get("ok")):
                continue
            tool_name = normalize_text(str(step.get("tool", ""))).lower()
            if tool_name in _INTERNAL_STEP_TOOLS:
                continue
            if self._skip_raw_tool_display_in_fallback(tool_name, display):
                continue
            if len(display) > 280:
                display = clip_text(display, 280)
            if display:
                video_url = self._last_success_video_url(steps)
                image_urls = self._last_success_image_urls(steps)
                image_url = image_urls[0] if image_urls else ""
                audio_file = self._last_success_audio_file(steps)
                if video_url:
                    display = self._sanitize_final_text_for_local_media(
                        display, video_url
                    )
                return AgentResult(
                    reply_text=display,
                    image_url=image_url,
                    image_urls=image_urls,
                    video_url=video_url,
                    audio_file=audio_file,
                    action="reply",
                    reason=f"agent_fallback_{reason}",
                    tool_calls_made=tool_calls_made,
                    total_time_ms=self._elapsed(t0),
                    steps=steps,
                )
        # 工具失败时，只有工具自己写的那句人话才可以直接转给用户（避免二次 LLM
        # 超时后丢失真实原因）。循环里合成的 "<tool> 失败: <error>" 带 display_synthetic
        # 标记，一律不外发；剩下的先剥机器标识符，剥完不成句的也不外发。
        failure_categories = self._failure_categories_from_steps(steps)
        for step in reversed(steps):
            if bool(step.get("ok")):
                continue
            display = normalize_text(str(step.get("display", "")))
            if not display:
                continue
            tool_name = normalize_text(str(step.get("tool", ""))).lower()
            if self._skip_raw_tool_display_in_fallback(tool_name, display):
                continue
            if tool_name in _INTERNAL_STEP_TOOLS:
                continue
            if bool(step.get("display_synthetic")) or not self._is_user_presentable_failure_text(display):
                _log.warning(
                    "agent_fallback_display_suppressed | trace=%s | tool=%s | category=%s | raw=%s",
                    ctx.trace_id,
                    tool_name,
                    self._classify_tool_failure(str(step.get("error", ""))),
                    clip_text(display, 120),
                )
                continue
            safe_display = self._scrub_internal_state_text(display)
            if len(safe_display) > 280:
                safe_display = clip_text(safe_display, 280)
            return AgentResult(
                reply_text=safe_display,
                action="reply",
                reason=f"agent_fallback_{reason}",
                tool_calls_made=tool_calls_made,
                total_time_ms=self._elapsed(t0),
                steps=steps,
            )

        # 没有可外发的步骤结果 → 按失败类别兜底。
        # 预算已耗尽的原因（模型超时/报错）不再打第二次 LLM，直接用类别兜底句。
        category_reply = (
            self._failure_category_reply(failure_categories)
            if failure_categories
            else _pl.get_message(
                "no_result",
                "我这边刚刚没跑通，你换个说法或稍后再试，我继续处理。",
            )
        )
        ai_reply = ""
        if reason not in self._NO_SECOND_LLM_FALLBACK_REASONS:
            # 只传类别标签，不传工具名也不传错误码 —— 模型看不到内部状态就复述不出来。
            situation = self._build_failure_situation_hint(failure_categories)
            ai_reply = await self._ai_fallback_reply(ctx, situation)
        else:
            _log.info(
                "agent_fallback_skip_second_llm | trace=%s | reason=%s | categories=%s",
                ctx.trace_id,
                reason,
                ",".join(failure_categories) or "none",
            )
        # 出口再剥一次：模型即使凭上下文猜出工具名，也不让它出群。
        fallback_text = self._scrub_internal_state_text(ai_reply) or category_reply
        video_url = self._last_success_video_url(steps)
        image_urls = self._last_success_image_urls(steps)
        image_url = image_urls[0] if image_urls else ""
        audio_file = self._last_success_audio_file(steps)
        if video_url:
            fallback_text = self._sanitize_final_text_for_local_media(
                fallback_text, video_url
            )
        return AgentResult(
            reply_text=fallback_text,
            image_url=image_url,
            image_urls=image_urls,
            video_url=video_url,
            audio_file=audio_file,
            action="reply",
            reason=f"agent_fallback_{reason}",
            tool_calls_made=tool_calls_made,
            total_time_ms=self._elapsed(t0),
            steps=steps,
        )

    @classmethod
    def _classify_tool_failure(cls, error: str) -> str:
        """把 ToolCallResult.error 的机器码归到一个失败类别。

        只看 error 字段（本仓自己写的 ASCII 机器码），不看 display。取不到就
        返回 unknown —— 宁可说"没能完成"，也不把原始状态串带给用户。
        """
        code = normalize_text(error).lower()
        # 只保留 ASCII 机器码部分：`tool_exception: HTTPSConnectionPool(...)` 这类
        # 后半段是异常文本，不该参与分类。
        code = " ".join(re.findall(r"[a-z0-9_:.-]+", code))
        if not code:
            return "unknown"
        for category, markers in cls._TOOL_FAILURE_CATEGORY_MARKERS:
            if any(marker in code for marker in markers):
                return category
        return "unknown"

    @classmethod
    def _failure_categories_from_steps(cls, steps: list[dict[str, Any]]) -> list[str]:
        """按发生顺序取失败步骤的类别，去重后返回。"""
        categories: list[str] = []
        for step in steps:
            if not isinstance(step, dict) or step.get("ok") is not False:
                continue
            category = cls._classify_tool_failure(str(step.get("error", "")))
            if category not in categories:
                categories.append(category)
        return categories

    @classmethod
    def _failure_category_hint(cls, categories: list[str]) -> str:
        """把类别列表转成喂给兜底 LLM 的短标签串（不含工具名、不含错误码）。"""
        labels = [
            cls._TOOL_FAILURE_CATEGORY_LABELS.get(category, cls._TOOL_FAILURE_CATEGORY_LABELS["unknown"])
            for category in categories[:3]
        ]
        return "、".join(labels)

    @classmethod
    def _failure_category_reply(cls, categories: list[str]) -> str:
        """按失败类别取一句给用户的话。零 LLM 调用，供预算已耗尽时使用。"""
        category = categories[0] if categories else "unknown"
        defaults = {
            "timeout": "这件事我这边跑太久了，没等到结果。你稍后再让我试一次。",
            "permission": "这件事我现在没权限做，得让管理员来。",
            "missing_args": "我还缺点关键信息，你把要求再说具体一点我就能做。",
            "blocked": "这个我不能做，换个方向我陪你继续。",
            "unavailable": "这个能力我现在用不了，等能用了我再帮你弄。",
            "not_found": "我找了一圈没找到，你换个说法或者给我多点线索。",
            "upstream": "我要的东西没拿到，稍后再试一次应该就好。",
            "unknown": "这件事我没做成，你换个说法我再来一次。",
        }
        return _pl.get_message(f"tool_failure_{category}", defaults[category])

    @classmethod
    def _scrub_internal_state_text(cls, text: str) -> str:
        """从要发给用户的文本里剥掉机器标识符（工具名 / 错误码 / key=value）。

        只按结构剥：含下划线的 ASCII 标记、`k=v` 状态串。URL 原样保留 ——
        媒体链接要靠它投递。剥完的空洞由调用方判断是否已经不成句。
        """
        content = normalize_text(text)
        if not content:
            return ""
        rewritten = cls._rewrite_outside_urls(content, cls._scrub_machine_tokens)
        return _RE_WHITESPACE_2PLUS.sub(" ", rewritten).strip()

    @staticmethod
    def _rewrite_outside_urls(content: str, rewrite: Any) -> str:
        """把 `rewrite` 只作用在 URL 之外的片段上，URL 原样保留。

        媒体链接要靠 URL 原文投递，任何按结构剥字符的清洗都不能伸进 URL 里。
        """
        parts: list[str] = []
        cursor = 0
        for match in _RE_URL_EXTRACT.finditer(content):
            parts.append(rewrite(content[cursor : match.start()]))
            parts.append(match.group(0))
            cursor = match.end()
        parts.append(rewrite(content[cursor:]))
        return "".join(parts)

    @staticmethod
    def _scrub_machine_tokens(chunk: str) -> str:
        if not chunk:
            return ""
        cleaned = _RE_MACHINE_KEY_VALUE.sub("", chunk)
        cleaned = _RE_MACHINE_IDENTIFIER.sub("", cleaned)
        # 剥完常剩下 "失败: ，" 这类孤立标点，收一下。
        cleaned = re.sub(r"[（(\[【]\s*[）)\]】]", "", cleaned)
        cleaned = re.sub(r"[:：]\s*(?=[，。、！？\s]|$)", "", cleaned)
        return cleaned

    @classmethod
    def _is_user_presentable_failure_text(cls, text: str) -> bool:
        """剥掉机器标识符后还剩不剩一句人话。

        阈值是字符数这种结构量，不是"这句话像不像内部状态"的语义判断。
        """
        scrubbed = cls._scrub_internal_state_text(text)
        if not scrubbed:
            return False
        return len(_RE_CJK_CHAR.findall(scrubbed)) >= _MIN_CJK_FOR_USER_FACING_FAILURE

    @classmethod
    def _skip_raw_tool_display_in_fallback(cls, tool_name: str, text: str) -> bool:
        tool = normalize_text(tool_name).lower()
        content = normalize_text(text)
        if not content:
            return True
        if tool in cls._FALLBACK_RAW_DISPLAY_SKIP_TOOLS:
            return True
        # 中间提取结果经常是英文长段，直接透传会污染群聊体验。
        letters = len(_RE_ASCII_LETTER.findall(content))
        cjk = len(_RE_CJK_CHAR.findall(content))
        if letters >= 40 and cjk <= 6:
            return True
        lower = content.lower()
        if lower.startswith("based on the webpage content"):
            return True
        if "from the webpage content" in lower and "no direct" in lower:
            return True
        return False

    @staticmethod
    def _is_placeholder_media_url(url: str) -> bool:
        return media_utils.is_placeholder_media_url(url)

    @staticmethod
    def _is_local_media_path(url: str) -> bool:
        return media_utils.is_local_media_path(url)

    @staticmethod
    def _normalize_media_url(url: str) -> str:
        value = normalize_text(url).strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https"}:
                return ""
            host = parsed.netloc.lower()
            path = parsed.path or ""
            query = parsed.query or ""
            # 去掉 fragment；query 保留，避免同路径不同资源被误合并。
            return f"{parsed.scheme.lower()}://{host}{path}" + (
                f"?{query}" if query else ""
            )
        except Exception:
            return ""

    @classmethod
    def _url_matches_known_media(cls, candidate: str, known_urls: set[str]) -> bool:
        target = cls._normalize_media_url(candidate)
        if not target:
            return False
        if target in known_urls:
            return True
        for known in known_urls:
            if not known:
                continue
            if target.startswith(known) or known.startswith(target):
                return True
        return False

    @classmethod
    def _collect_urls_from_payload(cls, payload: Any, out: set[str]) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_norm = normalize_text(str(key)).lower()
                if isinstance(value, str):
                    if "url" in key_norm or key_norm in {
                        "source",
                        "link",
                        "image",
                        "video",
                    }:
                        norm = cls._normalize_media_url(value)
                        if norm:
                            out.add(norm)
                    continue
                if isinstance(value, list):
                    for item in value:
                        cls._collect_urls_from_payload(item, out)
                    continue
                if isinstance(value, dict):
                    cls._collect_urls_from_payload(value, out)
            return
        if isinstance(payload, list):
            for item in payload:
                cls._collect_urls_from_payload(item, out)
            return
        if isinstance(payload, str):
            norm = cls._normalize_media_url(payload)
            if norm:
                out.add(norm)

    def _collect_known_media_urls(
        self, steps: list[dict[str, Any]], ctx: AgentContext
    ) -> set[str]:
        known: set[str] = set()
        for raw_text in (ctx.message_text, ctx.reply_to_text):
            if not raw_text:
                continue
            for found in re.findall(
                r"https?://[^\s<>\"]+", raw_text, flags=re.IGNORECASE
            ):
                norm = self._normalize_media_url(found)
                if norm:
                    known.add(norm)
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_data = step.get("data", {})
            if isinstance(step_data, dict) and step_data:
                self._collect_urls_from_payload(step_data, known)
        return known

    @staticmethod
    def _normalize_local_media_path(path: str) -> str:
        return media_utils.normalize_local_media_path(path)

    @classmethod
    def _collect_local_paths_from_payload(cls, payload: Any, out: set[str]) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                key_norm = normalize_text(str(key)).lower()
                if isinstance(value, str):
                    if any(
                        token in key_norm
                        for token in ("path", "file", "url", "image", "video")
                    ):
                        local = cls._normalize_local_media_path(value)
                        if local:
                            out.add(local)
                    continue
                if isinstance(value, list):
                    for item in value:
                        cls._collect_local_paths_from_payload(item, out)
                    continue
                if isinstance(value, dict):
                    cls._collect_local_paths_from_payload(value, out)
            return
        if isinstance(payload, list):
            for item in payload:
                cls._collect_local_paths_from_payload(item, out)
            return
        if isinstance(payload, str):
            local = cls._normalize_local_media_path(payload)
            if local:
                out.add(local)

    @staticmethod
    def _extract_media_refs_from_segments(segments: list[dict[str, Any]]) -> list[str]:
        refs: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            data = seg.get("data", {}) or {}
            if not isinstance(data, dict):
                continue
            for key in ("memory_data_uri", "url", "file", "path"):
                value = normalize_text(str(data.get(key, "")))
                if value:
                    refs.append(value)
        return refs

    def _collect_known_local_media_paths(
        self, steps: list[dict[str, Any]], ctx: AgentContext
    ) -> set[str]:
        known: set[str] = set()
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_data = step.get("data", {})
            if isinstance(step_data, dict) and step_data:
                self._collect_local_paths_from_payload(step_data, known)
        for item in self._extract_media_refs_from_segments(
            ctx.raw_segments
        ) + self._extract_media_refs_from_segments(ctx.reply_media_segments):
            local = self._normalize_local_media_path(item)
            if local:
                known.add(local)
        return known

    @staticmethod
    def _sanitize_profile_summary(summary: str) -> str:
        content = normalize_text(summary)
        if not content:
            return ""
        # 避免把可识别画像统计直接喂给模型，降低隐私泄露概率。
        content = re.sub(
            r"(?:QQ号|qq号|消息数|发言数|发了\d+条消息|凌晨\d+点(?:左右)?活跃|活跃时段|作息规律)[^。；;\n]*[。；;]?",
            "",
            content,
            flags=re.IGNORECASE,
        )
        content = _RE_WHITESPACE_2PLUS.sub(" ", content).strip()
        return content

    @staticmethod
    def _elapsed(t0: float) -> int:
        return int((time.monotonic() - t0) * 1000)

    @staticmethod
    def _looks_like_english_refusal_text(text: str) -> bool:
        content = normalize_text(text).lower()
        if not content:
            return False
        refusal_markers = (
            "i can't",
            "i cannot",
            "i can’t",
            "i'm not able",
            "i’m not able",
            "unable to",
            "cannot help with that request",
            "can't help with that request",
            "text-based ai assistant",
            "as an ai",
            "adult content",
            "sexually explicit",
            "18+",
            "nsfw",
        )
        if not any(marker in content for marker in refusal_markers):
            return False
        cjk_count = sum(1 for ch in content if "\u4e00" <= ch <= "\u9fff")
        alpha_count = sum(1 for ch in content if ch.isalpha())
        return alpha_count > 0 and cjk_count <= 2

    @classmethod
    def _normalize_final_answer_text(cls, text: str) -> str:
        """校验并归一化 final_answer 的文本质量。

        弱模型防护:
        - 英文拒绝归一化
        - 控制字符/乱码检测
        - 纯标点/空白检测
        - 回显工具格式检测
        """
        content = normalize_text(text)
        if not content:
            return ""
        # `_RE_LOCAL_FILE_REF` 带 `(?i)`，它的 Windows 盘符分支 `[A-Z]:[\\/]` 会把
        # 任意 `https://…` 里的 `s://…` 当成 `s:` 盘的路径吃掉，只剩一个 `http`。
        # 所以这条替换必须只作用在 URL 之外 —— 图片/视频链接要靠原文投递。
        content = cls._rewrite_outside_urls(
            content,
            lambda chunk: _RE_LOCAL_FILE_REF.sub("[本地文件路径已隐藏，发送层会直接投递]", chunk),
        )
        content = _RE_SYNTHETIC_USER_PREFIX.sub("", content).strip()
        if cls._looks_like_english_refusal_text(content):
            return "这个请求我不能帮你处理（涉及不当或露骨内容）。你可以换个健康、合规的话题，我继续帮你。"
        # 弱模型防护: 纯标点/空白检测（去掉所有标点和空白后为空）
        stripped = _RE_PUNCTUATION_CJK.sub("", content).strip()
        if not stripped:
            return ""
        # 弱模型防护: 控制字符/乱码比例过高
        control_count = sum(1 for c in content if ord(c) < 32 and c not in ('\n', '\r', '\t'))
        if len(content) > 0 and control_count / len(content) > 0.3:
            return ""
        # 弱模型防护: 回显了工具调用格式到最终回复（不应发给用户）
        trimmed = content.strip()
        if (
            trimmed.startswith("{")
            and trimmed.endswith("}")
            and ('"tool"' in trimmed or '"function"' in trimmed or '"name"' in trimmed)
        ):
            return ""
        # 模型自己写的 final_answer 此前从不过这道清洗，于是
        # `analyze_image 执行超时（>45s）` 被当正文发进群（实测日志 L961，
        # 群友引用可见）。清洗器本来就能剥掉它（输出 `执行超时（>45s）`），
        # 只是没接上。按结构剥机器标识符 / k=v，URL 原样保留。
        return cls._scrub_internal_state_text(content)

    @classmethod
    def _build_failure_situation_hint(cls, categories: list[str]) -> str:
        """给兜底 LLM 的"情况"描述：只有失败类别，没有工具名和错误码。"""
        template = _pl.get_message(
            "agent_fallback_situation",
            "这件事没做成，原因类别是：{categories}。不要提任何工具名、错误码或内部组件。",
        )
        labels = cls._failure_category_hint(categories) or cls._TOOL_FAILURE_CATEGORY_LABELS["unknown"]
        if "{categories}" in template:
            return template.replace("{categories}", labels)
        return f"{template}（{labels}）"

    async def _ai_fallback_reply(self, ctx: AgentContext, error_hint: str) -> str:
        """用一次快速 LLM 调用生成错误场景的自然回复，失败返回空字符串。"""
        try:
            system = _pl.get_message(
                "agent_fallback_reply_system",
                "你是 YuKiKo。你刚刚没把用户要的事做成，需要用简短自然的语气跟用户说一句。"
                "不要用'抱歉'开头，不要太正式，像朋友聊天一样说。一句话就够了。"
                "必须使用简体中文，不要输出英文段落。"
                "禁止说自己是 IDE 助手或说无法扮演当前角色。"
                "只说做不到、以及用户可以怎么换个方式；"
                "绝对不要出现工具名、函数名、参数名、错误码、retcode、接口名、"
                "后端组件名、模型名、开发者或维护者的名字，也不要说"
                "'超时''接口报错''调用失败''代码要改'这类内部状态。",
            )
            memory_lines = [
                f"- {clip_text(normalize_text(item), 80)}"
                for item in ctx.memory_context[-5:]
                if normalize_text(item)
            ]
            memory_block = "\n".join(memory_lines) if memory_lines else "(无)"
            user_msg = (
                f"用户说：{clip_text(ctx.message_text, 200)}\n"
                f"是否私聊：{ctx.is_private}\n"
                f"是否@机器人：{ctx.mentioned}\n"
                f"最近上下文：\n{memory_block}\n\n"
                f"情况：{error_hint}\n\n"
                "请结合上下文用一句简短的话回复用户。"
            )
            raw = await asyncio.wait_for(
                self.model_client.chat_text_with_retry(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_msg},
                    ],
                    max_tokens=100,
                    retries=1,
                    backoff=0.5,
                ),
                timeout=8,
            )
            reply = normalize_text(raw).strip()
            scrubbed = self._scrub_internal_state_text(reply)
            if scrubbed != reply:
                _log.warning(
                    "agent_fallback_reply_scrubbed | trace=%s | raw=%s",
                    ctx.trace_id,
                    clip_text(reply, 120),
                )
            return scrubbed
        except Exception as exc:
            _log.warning(
                "agent_fallback_reply_failed | trace=%s | %s: %s",
                ctx.trace_id,
                type(exc).__name__,
                exc,
            )
            return ""
