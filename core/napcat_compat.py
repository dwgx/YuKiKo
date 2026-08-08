from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

_log = logging.getLogger("yukiko.napcat_compat")

NAPCAT_ID_KEYS = frozenset(
    {
        "bot_id",
        "group_id",
        "group_openid",
        "message_id",
        "operator_id",
        "peer_id",
        "qq",
        "self_id",
        "target_id",
        "target_user_id",
        "user_id",
        "user_openid",
    }
)
NAPCAT_API_ALIASES: dict[str, str] = {
    "send_group_message": "send_group_msg",
    "send_private_message": "send_private_msg",
    "get_user_info": "get_stranger_info",
    "get_message": "get_msg",
    "delete_message": "delete_msg",
    "get_group_notice": "_get_group_notice",
    "send_group_notice": "_send_group_notice",
    "delete_group_notice": "_del_group_notice",
    "set_group_sign": "send_group_sign",
    # NapCat 统一戳一戳 API（group_poke / friend_poke → send_poke）
    # 注意: 旧版 group_poke / friend_poke 仍然可用，但推荐用 send_poke
}
_VERSION_PART_RE = re.compile(r"\d+")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_STRING_ID_VERSION_FLOOR = (4, 8, 115)

# 蓝图 §9.5：早期 NapCat 的 get_group_file_url 收 `{file_id, group}`，现版
# schema 收 `{group_id, file_id}`（NapCat main 源码 GetGroupFileUrl.ts）。
# 两种拼写都收，统一归一成现版 group_id，老的 group 拼写不会被新 NapCat 拒。
NAPCAT_API_PARAM_ALIASES: dict[str, dict[str, str]] = {
    "get_group_file_url": {"group": "group_id"},
}

# QQ CDN 图片 URL 约 2h 过期（蓝图 §9.4 / §9.7-5），过期后 NapCat 报 url expired。
IMAGE_URL_TTL_SECONDS = 2 * 60 * 60


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def napcat_file_uri_to_path(value: Any) -> Path | None:
    """Parse a local file:// URI into a Path.

    NapCat also uses file://<id> as an internal resource identifier; that form
    is not a local filesystem path and intentionally returns None.
    """
    raw = _clean_text(value)
    if not raw.lower().startswith("file://"):
        return None
    parsed = urlparse(raw)
    if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
        if not parsed.path or parsed.path == "/":
            return None
        local_raw = f"//{parsed.netloc}{unquote(parsed.path)}"
    else:
        local_raw = unquote(parsed.path or "")
    if re.match(r"^/[A-Za-z]:/", local_raw):
        local_raw = local_raw[1:]
    if not local_raw:
        return None
    return Path(local_raw)


def build_napcat_file_reference(path_like: Any, *, require_exists: bool = False) -> str:
    """Return the file reference format YuKiKo sends to NapCat message segments.

    HTTP(S), base64, data URL, and existing file:// references pass through.
    Local files become absolute file:// URIs, which is the most stable form for
    OneBot/NapCat media segments.
    """
    source = _clean_text(path_like)
    if not source:
        return ""
    lower_source = source.lower()
    if lower_source.startswith(("file://", "http://", "https://", "base64://", "data:")):
        return source
    if _URI_SCHEME_RE.match(source) and not re.match(r"^[A-Za-z]:[\\/]", source):
        return source
    try:
        path = Path(source).expanduser().resolve()
        if path.exists():
            if require_exists and not path.is_file():
                return ""
            return path.as_uri()
        if require_exists:
            return ""
    except Exception:
        if require_exists:
            return ""
    normalized = source.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", normalized):
        normalized = f"/{normalized}"
    elif not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return f"file://{normalized}"


def normalize_napcat_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return None
    text = _clean_text(value)
    return text or None


def _normalize_value(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        normalized: dict[Any, Any] = {}
        for raw_key, item in value.items():
            key = _clean_text(raw_key)
            if key in NAPCAT_ID_KEYS:
                normalized_id = normalize_napcat_id(item)
                normalized[raw_key] = normalized_id if normalized_id is not None else item
                continue
            normalized[raw_key] = _normalize_value(item, parent_key=key)
        return normalized
    if isinstance(value, list):
        return [_normalize_value(item, parent_key=parent_key) for item in value]
    if parent_key in NAPCAT_ID_KEYS:
        normalized_id = normalize_napcat_id(value)
        if normalized_id is not None:
            return normalized_id
    return value


def _apply_api_param_aliases(api: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """按 API 的参数别名表重命名顶层参数（如 get_group_file_url 的 group → group_id）。

    目标键已显式出现时丢弃冗余别名键，绝不覆盖显式值。
    """
    aliases = NAPCAT_API_PARAM_ALIASES.get(api)
    if not aliases:
        return kwargs
    normalized: dict[str, Any] = {}
    for raw_key, value in kwargs.items():
        target = aliases.get(_clean_text(raw_key))
        if target is not None:
            if target not in kwargs and target not in normalized:
                normalized[target] = value
            continue
        normalized[raw_key] = value
    return normalized


def normalize_napcat_api_kwargs(api: str, kwargs: Mapping[str, Any] | None) -> dict[str, Any]:
    source = _apply_api_param_aliases(api, dict(kwargs or {}))
    return _normalize_value(source)


def resolve_napcat_api_name(api: str) -> str:
    text = _clean_text(api)
    if not text:
        return ""
    return NAPCAT_API_ALIASES.get(text, text)


async def call_napcat_api(
    api_call: Callable[..., Awaitable[Any]],
    api: str,
    **kwargs: Any,
) -> Any:
    resolved_api = resolve_napcat_api_name(api)
    return await api_call(resolved_api, **normalize_napcat_api_kwargs(resolved_api, kwargs))


async def call_napcat_bot_api(bot: Any, api: str, **kwargs: Any) -> Any:
    return await call_napcat_api(bot.call_api, api, **kwargs)


# ── QQ CDN 图片 URL 过期（约 2h）检测与刷新 ──


def parse_image_url_expiry_ts(url: Any) -> int | None:
    """从 QQ CDN 图片 URL 的 `t=` 参数里取出过期时间戳（epoch 秒）。

    取不到可解析的时间戳时返回 None —— 普通 http 直链没有 t= 参数，
    检测函数据此对它永远判「未过期」，绝不误伤。
    """
    parsed = urlparse(_clean_text(url))
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key != "t":
            continue
        text = _clean_text(value)
        if not text:
            continue
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return int(text, 16)
        except ValueError:
            continue
    return None


def is_qq_image_url_expired(
    url: Any,
    *,
    now: int | None = None,
    ttl_seconds: int = IMAGE_URL_TTL_SECONDS,
) -> bool:
    """判断 QQ CDN 图片 URL 是否已过期（`t=` 时间戳早于 TTL 边界）。

    没有可解析的 `t=` 时间戳时返回 False —— 宁可漏报也不误报，
    避免把普通 http 直链误判成过期。发送路径在发送前调用它决定要不要刷新。
    """
    ts = parse_image_url_expiry_ts(url)
    if ts is None:
        return False
    return (now if now is not None else int(time.time())) - ts > ttl_seconds


def extract_image_url_from_msg_payload(payload: Any) -> str:
    """从 get_msg 回包里取出第一条 image 段的 url（用于刷新过期 URL）。"""
    outer = payload if isinstance(payload, dict) else {}
    data = outer.get("data") if isinstance(outer.get("data"), dict) else outer
    segments = data.get("message") if isinstance(data.get("message"), list) else None
    for segment in segments or []:
        if not isinstance(segment, dict) or segment.get("type") != "image":
            continue
        seg_data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        url = _clean_text(seg_data.get("url"))
        if url:
            return url
        file_ref = _clean_text(seg_data.get("file"))
        if file_ref.lower().startswith(("http://", "https://")):
            return file_ref
    return ""


async def refresh_expired_image_url(
    url: Any,
    api_call: Callable[..., Awaitable[Any]] | None,
    *,
    message_id: str = "",
) -> str:
    """尽力刷新 QQ CDN 图片 URL（约 2h 过期，蓝图 §9.4）。

    - 给了 message_id：`get_msg{message_id}` 从原消息段取回新鲜 url。
    - url 是 `file://<hash>` 内部资源引用：`get_image{file}` 换成本地路径
      （本地路径不随 rkey 过期，等价于刷新）。
    - 其余情况（或刷新失败）原样返回，绝不抛异常 —— 发送路径把它当尽力而为，
      失败仍按原 URL 发送。
    """
    source = _clean_text(url)
    if not source or api_call is None:
        return source
    if message_id:
        try:
            payload = await api_call("get_msg", message_id=message_id)
            refreshed = extract_image_url_from_msg_payload(payload)
            if refreshed:
                return refreshed
        except Exception as exc:
            _log.warning(
                "image_url_refresh_get_msg_failed | message_id=%s | %s",
                message_id,
                exc,
            )
    if source.lower().startswith("file://"):
        try:
            payload = await api_call("get_image", file=source)
        except Exception as exc:
            _log.warning("image_url_refresh_get_image_failed | src=%s | %s", source, exc)
            return source
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        for key in ("file", "file_path", "path", "local_path"):
            candidate = _clean_text(data.get(key))
            if candidate:
                return candidate
    return source


def extract_napcat_version_info(payload: Any) -> dict[str, str]:
    version = payload if isinstance(payload, dict) else {}
    data = version.get("data")
    if isinstance(data, dict):
        version = data

    def pick(*keys: str) -> str:
        for key in keys:
            text = _clean_text(version.get(key, ""))
            if text:
                return text
        return ""

    return {
        "app_name": pick("app_name", "name"),
        "app_version": pick("app_version", "version", "napcat_version", "plugin_version"),
        "protocol_version": pick("protocol_version", "protocol", "onebot_version"),
    }


def parse_napcat_version(value: Any) -> tuple[int, ...]:
    text = _clean_text(value)
    if not text:
        return ()
    parts = _VERSION_PART_RE.findall(text)
    if not parts:
        return ()
    return tuple(int(part) for part in parts[:4])


def napcat_prefers_string_ids(version_payload: Any) -> bool | None:
    meta = extract_napcat_version_info(version_payload)
    version_tuple = parse_napcat_version(meta.get("app_version", ""))
    if not version_tuple:
        return None
    return version_tuple >= _STRING_ID_VERSION_FLOOR


def collect_linux_runtime_diagnostics() -> dict[str, Any]:
    system_name = platform.system().lower() or os.name.lower()
    home = Path.home()
    shell_paths = [
        Path("/opt/QQ/resources/app/app_launcher/napcat/napcat.mjs"),
        Path("/opt/QQ/qq"),
        Path("/usr/bin/napcat"),
        Path("/usr/local/bin/napcat"),
        home / "NapCat.Shell" / "napcat" / "napcat.mjs",
        home / "NapCat.Shell" / "napcat.mjs",
    ]
    service_units = [
        Path("/etc/systemd/system/napcat.service"),
        Path("/usr/lib/systemd/system/napcat.service"),
        Path("/lib/systemd/system/napcat.service"),
    ]
    binaries = {
        "napcat": shutil.which("napcat") or "",
        "qq": shutil.which("qq") or "",
        "ffmpeg": shutil.which("ffmpeg") or "",
        "ffprobe": shutil.which("ffprobe") or "",
        "node": shutil.which("node") or "",
        "npm": shutil.which("npm") or "",
    }
    existing_shell_paths = [str(path) for path in shell_paths if path.exists()]
    existing_service_units = [str(path) for path in service_units if path.exists()]
    return {
        "platform": system_name,
        "is_linux": system_name == "linux",
        "binaries": binaries,
        "ffmpeg_ready": bool(binaries["ffmpeg"]),
        "ffprobe_ready": bool(binaries["ffprobe"]),
        "media_stack_ready": bool(binaries["ffmpeg"] and binaries["ffprobe"]),
        "napcat_command_ready": bool(binaries["napcat"]),
        "qq_command_ready": bool(binaries["qq"]),
        "shell_install_paths": existing_shell_paths,
        "service_units": existing_service_units,
        "shell_install_detected": bool(existing_shell_paths or binaries["napcat"]),
    }


def build_napcat_diagnostics(
    *,
    status_payload: Any,
    version_payload: Any,
    bot_self_id: str = "",
    bot_id: str = "",
) -> dict[str, Any]:
    status = status_payload if isinstance(status_payload, dict) else {}
    version_meta = extract_napcat_version_info(version_payload)
    linux = collect_linux_runtime_diagnostics()
    return {
        "bot": {
            "bot_id": _clean_text(bot_id),
            "self_id": _clean_text(bot_self_id),
        },
        "runtime": {
            "online": bool(status.get("online", False)),
            "good": bool(status.get("good", False)),
            "status": status,
        },
        "version": version_meta,
        "compatibility": {
            "normalized_id_keys": sorted(NAPCAT_ID_KEYS),
            "string_id_normalization_active": True,
            "string_id_preferred_by_version": napcat_prefers_string_ids(version_payload),
        },
        "linux": linux,
    }
