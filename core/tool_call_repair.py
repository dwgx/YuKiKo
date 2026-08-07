"""Phase 3b：validate-then-repair 工具调用修复层（WorkBuddy 式）。

覆盖开源模型 tool call 解析失败的高发形态。修复是「扩展语义 + 透明标记」：
- 返回 (修复后的 args, repairs 列表)，调用方把 repairs 记进日志/遥测；
- 不做拒绝、不做语义改写；无法修复的保持原样。

优先级（Hermes 错误回喂 > repair > _normalize_tool_args 猜参）在 AgentLoop 里编排，
本模块只做纯函数修复。零第三方依赖。
"""

from __future__ import annotations

import json
from typing import Any

# 关系不变量：这些工具缺参数时补默认值（P2）。
_RELATION_DEFAULTS: dict[str, dict[str, Any]] = {
    "read_file": {"offset": 0, "limit": 2000},
    "read_local_file": {"offset": 0, "limit": 2000},
}


def _schema_type_map(schema: dict[str, Any] | None) -> dict[str, str]:
    """从工具 schema 提取 {参数名: 期望 JSON 类型}。"""
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}
    type_map: dict[str, str] = {}
    for name, spec in props.items():
        if isinstance(spec, dict):
            value_type = spec.get("type")
            if isinstance(value_type, str):
                type_map[str(name)] = value_type
    return type_map


def repair_tool_call(
    args: Any,
    *,
    schema: dict[str, Any] | None = None,
    tool_name: str = "",
) -> tuple[Any, list[str]]:
    """对模型产生的工具参数做通用修复，返回 (修复后 args, repairs)。

    快通道：args 非 dict 时零开销返回原样（修复只作用于 dict 参数对象）。
    """
    if not isinstance(args, dict):
        return args, []
    type_map = _schema_type_map(schema)
    repairs: list[str] = []

    # ① 剥离 null：null 参数不传（很多 schema 不允许 null）。
    cleaned = {key: value for key, value in args.items() if value is not None}
    if len(cleaned) != len(args):
        repairs.append("stripped_null")

    # ③ 单键对象解包（先于②④）：{ "参数": {...} } 的内层键与 schema 匹配时解包。
    if len(cleaned) == 1:
        only_value = next(iter(cleaned.values()))
        if isinstance(only_value, dict):
            matched = [key for key in only_value if key in type_map]
            if matched:
                cleaned = only_value
                repairs.append("unwrapped_single_key_object")

    # ② 字符串化 JSON 数组转真数组（必须先于④）。
    for key, expected in type_map.items():
        value = cleaned.get(key)
        if expected == "array" and isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    cleaned[key] = parsed
                    repairs.append(f"json_array_string:{key}")
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

    # ④ 裸值包成单元素数组。
    for key, expected in type_map.items():
        value = cleaned.get(key)
        if expected == "array" and value is not None and not isinstance(value, list):
            cleaned[key] = [value]
            repairs.append(f"wrapped_single:{key}")

    # P2 关系不变量：已知工具缺参补默认（扩展语义 + 透明 _note）。
    defaults = _RELATION_DEFAULTS.get(tool_name)
    if defaults:
        for field, default_value in defaults.items():
            if field not in cleaned:
                cleaned[field] = default_value
                repairs.append(f"default_{field}")

    return cleaned, repairs
