"""Agent 工具公共类型定义。

从 agent_tools.py 拆分，所有模块共享的数据结构和类型别名。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolSchema:
    """描述一个可被 Agent 调用的工具。

    input_examples: 可选的完整调用示例，每项是一份 args 字典（不是单参数样例）。
        用途是给多参数 / 枚举值 / 有格式约束的工具一个填法样板，
        让模型看到「几个参数怎么配合」而不只是每个参数各自的说明。
        由注册中心渲染进 description 文本（见 AgentToolRegistry._render_input_examples），
        因此对原生 function calling 和纯文本 prompt 两条路都可见。
        留空即完全不影响导出结果，老注册无需改动。
    """
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    category: str = "general"  # general / napcat / search / media / admin
    group: str = ""  # backward-compat metadata only; not used for local intent routing
    input_examples: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        # 注册处习惯写 list[dict]，统一收成 tuple，避免共享可变默认值被就地改动。
        if isinstance(self.input_examples, (list, tuple)):
            self.input_examples = tuple(
                item for item in self.input_examples if isinstance(item, dict)
            )
        else:
            self.input_examples = ()


@dataclass(slots=True)
class ToolCallResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    display: str = ""  # 给 LLM 看的摘要


@dataclass(slots=True)
class PromptHint:
    """插件注入到 Agent 系统提示的静态文本块。

    section:
        - "rules": 出现在 ## 规则 区域
        - "tools_guidance": 出现在 ## 工具使用指南 区域
        - "context": 出现在 ## 上下文 区域
    priority: 数字越小越靠前，默认 50
    """
    source: str
    section: str
    content: str
    priority: int = 50
    tool_names: tuple[str, ...] = ()


ToolHandler = Callable[..., Awaitable[ToolCallResult]]
ContextProvider = Callable[[dict[str, Any]], str | Awaitable[str]]
