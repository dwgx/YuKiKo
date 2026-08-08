"""共享测试工厂：构造能跑完整 handle_message 链路的 YukikoEngine。

背景：16 个文件 35 处 `YukikoEngine.__new__` + 手工塞属性，没有共享工厂。
本模块收敛出 `make_engine()`：handle_message 的真实编排逻辑（去重、白名单、
trigger 评估、_try_agent_path、响应组装、_after_reply）全部走真实现，
只替换外部依赖：
- model_client：`SequencedModelClient` 按顺序弹出预设回复（sequencer），
  并记录每次调用的 messages，供断言 guard_payload 等注入内容；
- tool_registry：`StubToolRegistry`，每个工具名的返回结果可编程；
- admin / safety / memory / markdown / affinity / tools：最小 stub；
- 触碰存储/网络的叶子方法（媒体记忆捕获等）不接线，天然短路。

用法：

    engine = make_engine(
        responses=[
            '{"tool":"web_search","args":{"query":"python"}}',
            '{"tool":"final_answer","args":{"text":"搜索完成，Python 是..."}}',
        ],
        tool_results={"web_search": ToolCallResult(ok=True, data={}, display="ok")},
    )
    msg = make_message(text="帮我搜 python")
    response = await engine.handle_message(msg)

stub 对象挂在 engine._stub_model_client / engine._stub_registry 上供断言。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, defaultdict, deque
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from core.agent import AgentLoop
from core.agent_tools import ToolCallResult
from core.engine import YukikoEngine
from core.engine_types import EngineMessage
from core.trigger import TriggerEngine

_log = logging.getLogger("test.yukiko.conftest")

# ---------------------------------------------------------------------------
# 模型 stub：按顺序弹出预设回复；JSON 字符串 → 假 tool_call，其余 → 纯文本
# ---------------------------------------------------------------------------


class SequencedModelClient:
    """按顺序返回预设响应的模型 stub，并记录每轮收到的 messages。

    messages 记录用于断言「guard_payload 注入」这类只存在于模型输入里的内容。
    """

    enabled = True

    def __init__(self, responses: list[str], native_tools: bool = True):
        self._responses = list(responses)
        self._native_tools = native_tools
        self.messages_seen: list[list[dict[str, Any]]] = []

    def supports_native_tool_calling(self) -> bool:
        return self._native_tools

    @property
    def remaining(self) -> int:
        return len(self._responses)

    def _next(self) -> str:
        if not self._responses:
            raise AssertionError("No more model responses prepared for test")
        return self._responses.pop(0)

    async def chat_text_with_retry(
        self, messages, max_tokens=0, retries=0, backoff=0.0
    ):
        _ = (max_tokens, retries, backoff)
        self.messages_seen.append(list(messages))
        return self._next()

    async def chat_completion_with_retry(
        self, messages, max_tokens=0, tools=None, retries=0, backoff=0.0
    ):
        _ = (max_tokens, tools, retries, backoff)
        self.messages_seen.append(list(messages))
        if not self._native_tools:
            raise AssertionError(
                "chat_completion_with_retry should not be used when native tools are disabled"
            )
        resp = self._next()
        try:
            parsed = json.loads(resp)
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {
                                        "name": parsed.get("tool", "unknown"),
                                        "arguments": json.dumps(
                                            parsed.get("args", {})
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        except (TypeError, ValueError):
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": resp}}
                ]
            }


# ---------------------------------------------------------------------------
# 工具注册表 stub：每个工具名的返回结果可编程 + 记录调用
# ---------------------------------------------------------------------------


class StubToolRegistry:
    """最小 AgentToolRegistry 替身。"""

    def __init__(
        self,
        names: set[str] | None = None,
        results: dict[str, ToolCallResult] | None = None,
    ):
        self._names = set(names or {"web_search", "final_answer", "think"})
        self.results: dict[str, ToolCallResult] = results or {}
        self.calls: list[tuple[str, dict]] = []

    def has_tool(self, name: str) -> bool:
        return name in self._names

    def get_schema(self, name: str):
        _ = name
        return None

    def select_tools_for_intent(self, message_text: str, perm_level: str) -> list[str]:
        _ = (message_text, perm_level)
        return list(self._names)

    def get_schemas_for_prompt_filtered(self, selected_tools: list[str]) -> str:
        return "\n".join(f"- {n}" for n in selected_tools)

    def get_prompt_hints_text(
        self, section: str, tool_names: list[str] | None = None
    ) -> str:
        _ = (section, tool_names)
        return ""

    def list_tools_for_permission(self, permission_level: str = "user") -> list[str]:
        _ = permission_level
        return list(self._names)

    def get_schemas_for_native_tools(self, tool_names: list[str]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": n,
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for n in tool_names
        ]

    def get_dynamic_context(self, payload: dict, tool_names: list[str] | None = None) -> str:
        _ = (payload, tool_names)
        return ""

    async def call(self, name: str, args: dict, context: dict) -> ToolCallResult:
        _ = context
        self.calls.append((name, dict(args)))
        result = self.results.get(name)
        if result is not None:
            return result
        return ToolCallResult(ok=True, data={}, display=f"{name} 执行完成")


# ---------------------------------------------------------------------------
# 组件 stub
# ---------------------------------------------------------------------------


class StubAdmin:
    """白名单 + 命令处理的最小替身。"""

    def __init__(
        self,
        whitelisted_groups: set[int] | None = None,
        non_whitelist_mode: str = "silent",
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.non_whitelist_mode = non_whitelist_mode
        self._whitelisted = set(whitelisted_groups or [])

    def increment_message_count(self) -> None:
        pass

    def is_group_whitelisted(self, group_id: int) -> bool:
        return int(group_id) in self._whitelisted

    def is_admin_command(self, text: str) -> bool:
        _ = text
        return False

    async def handle_command(self, **kwargs) -> str:
        return ""

    def get_high_risk_confirmation_policy(
        self, group_id: int, default_required: bool = True
    ) -> dict[str, Any]:
        return {
            "high_risk_confirmation_required": bool(default_required),
            "source": "test",
            "group_id": int(group_id or 0),
            "overridden": False,
        }


class StubSafety:
    """安全引擎替身：不拦任何消息，输出过滤恒等。"""

    def evaluate(self, **kwargs) -> SimpleNamespace:
        _ = kwargs
        return SimpleNamespace(
            action="none", reason="", should_reply=False, risk_level="low"
        )

    def filter_output(self, text: str, **kwargs) -> str:
        _ = kwargs
        return text

    def is_political_topic(self, text: str) -> bool:
        _ = text
        return False


class StubMemory:
    """记忆引擎替身：所有查询返回空值。"""

    def add_message(self, **kwargs) -> None:
        _ = kwargs

    def write_daily_snapshot(self) -> None:
        pass

    def record_decision(self, **kwargs) -> None:
        _ = kwargs

    def get_conversation_keyword_hints(self, conversation_id, limit=10) -> list:
        _ = (conversation_id, limit)
        return []

    def get_recent_messages(self, conversation_id, limit=25) -> list:
        _ = (conversation_id, limit)
        return []

    def get_recent_texts(self, conversation_id, limit=24) -> list:
        _ = (conversation_id, limit)
        return []

    def search_related(
        self, conversation_id, text, roles=("user",), user_id=None, top_k=None
    ) -> list:
        _ = (conversation_id, text, roles, user_id, top_k)
        return []

    def get_user_profile_summary(self, user_id) -> str:
        _ = user_id
        return ""

    def knowledge_get_user_summary(self, user_id, limit=10) -> str:
        _ = (user_id, limit)
        return ""

    def get_preferred_name(self, user_id) -> str:
        _ = user_id
        return ""

    def get_recent_speakers(self, conversation_id, limit=12) -> list:
        _ = (conversation_id, limit)
        return []

    def get_agent_policies(self, user_id) -> dict:
        _ = user_id
        return {}

    def get_agent_directives(self, user_id) -> list:
        _ = user_id
        return []

    def get_thread_state(self, conversation_id) -> dict:
        _ = conversation_id
        return {}

    def get_message_media_artifacts(self, **kwargs) -> list:
        _ = kwargs
        return []


class StubMarkdown:
    """Markdown 渲染替身：原样返回，不做截断。"""

    max_output_chars = 4000
    max_output_lines = 80

    def render(self, text: str, max_len: int | None = None, max_lines: int | None = None) -> str:
        _ = (max_len, max_lines)
        return text or ""


class StubAffinity:
    """好感度引擎替身：无好感度、无心情。"""

    def __init__(self) -> None:
        self.mood = SimpleNamespace(current="")

    def affinity_prompt_hint(self, user_id) -> str:
        _ = user_id
        return ""

    def mood_prompt_hint(self) -> str:
        return ""

    def record_interaction(self, user_id, quality: float = 1.0) -> None:
        _ = (user_id, quality)


class StubTools:
    """ToolExecutor 替身：只响应 handle_message 侧的媒体记忆接口。"""

    def __init__(self) -> None:
        self.recent_media: dict[str, list[dict[str, Any]]] = {}

    def remember_incoming_media(self, conversation_id: str, raw_segments: list) -> None:
        _ = conversation_id
        _ = raw_segments

    def get_recent_media_for_followup(self, conversation_id: str, media_type: str = "image") -> list:
        _ = (conversation_id, media_type)
        return []


class FakeApiCall:
    """api_call 替身：记录调用，返回固定成功。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, action: str, **params) -> dict[str, Any]:
        self.calls.append((action, dict(params)))
        return {"status": "ok"}


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

_BASE_CONFIG: dict[str, Any] = {
    "bot": {
        "name": "YuKiKo",
        "nicknames": ["30秒"],
        "allow_memory": True,
        "allow_markdown": True,
    },
    "admin": {"enable": True, "super_users": ["10001"], "non_whitelist_mode": "silent"},
    "trigger": {"followup_reply_window_seconds": 30, "followup_max_turns": 2},
    "routing": {},
    "control": {},
    "queue": {"process_timeout_seconds": 120},
    "agent": {
        "enable": True,
        "max_steps": 8,
        "fallback_on_parse_error": True,
        "repeat_tool_guard_enable": True,
    },
    "memory": {"promotion_enable": False},
    "send_rate": {"enable": False},
    "safety": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """浅层 dict 逐键深合并（用于 config 覆盖，不保证任意深度）。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def make_engine(
    *,
    responses: list[str] | None = None,
    tool_names: set[str] | None = None,
    tool_results: dict[str, ToolCallResult] | None = None,
    config: dict[str, Any] | None = None,
    native_tools: bool = True,
    whitelisted_groups: set[int] | None = None,
) -> YukikoEngine:
    """构造能走完整 handle_message → trigger → agent → 响应的 engine。

    - `responses`：SequencedModelClient 按顺序弹出的预置回复。
    - `tool_names` / `tool_results`：StubToolRegistry 的工具集与返回结果。
    - `config`：深合并进基础配置（如 `{"agent": {"repeat_tool_guard_enable": False}}`）。
    - `whitelisted_groups`：StubAdmin 白名单群（默认 {1}）。
    """
    merged_config = _deep_merge(_BASE_CONFIG, config or {})
    engine = YukikoEngine.__new__(YukikoEngine)
    engine.config = merged_config
    engine.logger = logging.getLogger("test.yukiko.engine")
    # 从 config 派生全部阈值属性（与真实 __init__ 同一方法）
    engine._init_from_config()

    # ── 状态容器（对齐 __init__ 中段）──
    engine._async_init_done = True
    engine._async_init_lock = asyncio.Lock()
    engine._reload_lock = asyncio.Lock()
    engine._seen_message_ids: OrderedDict[str, float] = OrderedDict()
    engine._seen_message_ids_max = 200
    engine._pending_fragments: dict[str, Any] = {}
    engine._recent_directed_hints: dict[str, datetime] = {}
    engine._recent_search_cache: dict[str, Any] = {}
    engine._runtime_group_chat_cache = defaultdict(
        lambda: deque(maxlen=engine.runtime_group_cache_max_messages)
    )
    engine._media_artifact_index: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    engine._media_artifact_index_max = 500
    engine._agent_conversation_locks: dict[str, asyncio.Lock] = {}
    engine._agent_conversation_locks_max = 500
    engine._last_reply_state: dict[str, Any] = {}
    engine._last_resume_token: dict[str, str] = {}
    engine._promotion_counters: dict[str, int] = {}
    engine._group_member_name_cache: dict[int, Any] = {}
    engine._group_member_name_cache_max = 200
    engine._runtime_webui_bridge: dict[str, Any] = {}
    engine.verbosity = "normal"
    engine._verbosity_group_overrides: dict[str, str] = {}
    engine.output_style_instruction = ""
    engine._group_output_style_overrides: dict[str, str] = {}

    # ── 媒体记忆捕获参数（__init__ 里在 _init_from_config 之后手工设置）──
    memory_cfg = merged_config.get("memory", {})
    if not isinstance(memory_cfg, dict):
        memory_cfg = {}
    engine._memory_media_capture_enable = bool(
        memory_cfg.get("media_memory_enable", True)
    )
    try:
        max_images = int(memory_cfg.get("media_memory_max_images_per_message", 4))
    except (TypeError, ValueError):
        max_images = 4
    engine._memory_media_max_images_per_message = max(1, min(8, max_images))
    try:
        capture_timeout = float(
            memory_cfg.get("media_memory_capture_timeout_seconds", 6.0)
        )
    except (TypeError, ValueError):
        capture_timeout = 6.0
    engine._memory_media_capture_timeout_seconds = max(1.0, min(15.0, capture_timeout))
    try:
        profile_chars = int(memory_cfg.get("profile_summary_max_chars", 800))
    except (TypeError, ValueError):
        profile_chars = 800
    engine._profile_summary_max_chars = max(200, min(3000, profile_chars))
    try:
        memory_context_chars = int(memory_cfg.get("memory_context_max_chars", 1600))
    except (TypeError, ValueError):
        memory_context_chars = 1600
    engine._memory_context_max_chars = max(200, min(6000, memory_context_chars))
    try:
        related_memories_chars = int(
            memory_cfg.get("related_memories_max_chars", 1200)
        )
    except (TypeError, ValueError):
        related_memories_chars = 1200
    engine._related_memories_max_chars = max(200, min(6000, related_memories_chars))

    # ── 组件 ──
    engine.admin = StubAdmin(
        whitelisted_groups=whitelisted_groups or {1},
        non_whitelist_mode="silent",
    )
    engine.trigger = TriggerEngine(
        trigger_config=merged_config["trigger"],
        bot_config=merged_config["bot"],
    )
    engine.safety = StubSafety()
    engine.memory = StubMemory()
    engine.markdown = StubMarkdown()
    engine.affinity = StubAffinity()
    engine.tools = StubTools()

    # ── model + agent（stub 挂在 engine 上供断言）──
    model_client = SequencedModelClient(responses or [], native_tools=native_tools)
    registry = StubToolRegistry(
        names=tool_names or {"web_search", "sum", "music_play", "final_answer", "think"},
        results=tool_results,
    )
    agent = AgentLoop(
        model_client=model_client,
        tool_registry=registry,
        config=merged_config,
    )
    agent.high_risk_control_enable = False
    agent._build_system_prompt = lambda ctx: "system prompt"
    agent._build_user_message = lambda ctx: ctx.message_text
    engine.model_client = model_client
    engine.agent = agent
    engine._stub_model_client = model_client
    engine._stub_registry = registry
    return engine


def make_message(**overrides) -> EngineMessage:
    """构造 EngineMessage，默认是白名单群里被 @ 的普通消息。

    user_name 用纯数字（QQ 号形态），避开 _inject_user_name 的前缀注入，
    让 reply_text 断言不受名字改写影响。
    """
    base = EngineMessage(
        conversation_id="group:1:user:10086",
        user_id="10086",
        user_name="10086",
        group_id=1,
        bot_id="200",
        message_id="m-test-1",
        is_private=False,
        mentioned=True,
        text="你好",
        timestamp=datetime.now(UTC),
        trace_id="e2e-test",
        sender_role="member",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


__all__ = [
    "FakeApiCall",
    "SequencedModelClient",
    "StubAdmin",
    "StubAffinity",
    "StubMarkdown",
    "StubMemory",
    "StubSafety",
    "StubToolRegistry",
    "StubTools",
    "make_engine",
    "make_message",
]
