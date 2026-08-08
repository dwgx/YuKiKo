"""配置类型 schema + 启动校验（A1）。

单一真相声明：config 的类型约束。`ConfigManager.load()` 在环境变量替换与解密之后
调用 `validate_config()`，类型不匹配 / 必填缺失只记 warning，不阻断启动 ——
避免刚上线的配置漂移直接挂掉。

schema 结构: 点路径 -> ((允许的类型...), 是否必填)。
类型粒度是 bool/int/str/list/dict；int 接受 int（拒绝 bool），float 接受 int/float。
"""
from __future__ import annotations

from typing import Any

# 路径全部相对 config 根（不带 `config.` 前缀）。
# 顶层段必填；叶键由模板回填保证存在，一律可选，避免重复告警。
CONFIG_SCHEMA: dict[str, tuple[tuple[type, ...], bool]] = {
    # admin 段（必填）
    'admin': ((dict,), True),
    'admin.non_whitelist_mode': ((str,), False),
    'admin.super_admin_qq': ((str,), False),
    'admin.super_users': ((list,), False),
    'admin.whitelist_groups': ((list,), False),
    # affinity 段
    'affinity': ((dict,), False),
    'affinity.checkin_base_reward': ((int, float), False),
    'affinity.checkin_streak_bonus': ((int, float), False),
    'affinity.decay_per_day': ((int, float), False),
    'affinity.enable': ((bool,), False),
    'affinity.interaction_reward': ((int, float), False),
    'affinity.storage_dir': ((str,), False),
    # agent 段（必填）
    'agent': ((dict,), True),
    'agent.allow_silent_on_llm_error': ((bool,), False),
    'agent.enable': ((bool,), False),
    'agent.fallback_on_parse_error': ((bool,), False),
    'agent.high_risk_control': ((dict,), False),
    'agent.high_risk_control.categories': ((list,), False),
    'agent.high_risk_control.default_require_confirmation': ((bool,), False),
    'agent.high_risk_control.description_patterns': ((list,), False),
    'agent.high_risk_control.enable': ((bool,), False),
    'agent.high_risk_control.pending_ttl_seconds': ((int,), False),
    'agent.high_risk_control.tool_name_patterns': ((list,), False),
    'agent.high_risk_control.use_confirm_token': ((bool,), False),
    'agent.llm_step_timeout_seconds': ((int,), False),
    'agent.llm_step_timeout_seconds_after_tool': ((int,), False),
    'agent.max_consecutive_think': ((int,), False),
    'agent.max_same_tool_call': ((int,), False),
    'agent.max_steps': ((int,), False),
    'agent.max_tokens': ((int,), False),
    'agent.navigator_obvious_tool_timeout_seconds': ((int,), False),
    'agent.navigator_preflight_plain_text': ((bool,), False),
    'agent.navigator_retry_model': ((str,), False),
    'agent.prefer_router_for_directed_plain_text': ((bool,), False),
    'agent.preferred_name_prompt': ((str,), False),
    'agent.queue_timeout_margin_seconds': ((int,), False),
    'agent.repeat_tool_guard_enable': ((bool,), False),
    'agent.runtime_rules': ((str,), False),
    'agent.tool_args_log_max_chars': ((int,), False),
    'agent.tool_timeout_seconds': ((int,), False),
    'agent.tool_timeout_seconds_media': ((int,), False),
    'agent.total_timeout_seconds': ((int,), False),
    # api 段（必填）
    'api': ((dict,), True),
    'api.api_key': ((str,), False),
    'api.base_url': ((str,), False),
    'api.endpoint_type': ((str,), False),
    'api.fallback_models': ((list,), False),
    'api.max_tokens': ((int,), False),
    'api.model': ((str,), False),
    'api.provider': ((str,), False),
    'api.rank_failover': ((bool,), False),
    'api.temperature': ((int, float), False),
    'api.timeout_seconds': ((int,), False),
    # audit 段（必填）
    'audit': ((dict,), True),
    'audit.enable': ((bool,), False),
    # bot 段（必填）
    'bot': ((dict,), True),
    'bot.allow_image': ((bool,), False),
    'bot.allow_markdown': ((bool,), False),
    'bot.allow_non_to_me': ((bool,), False),
    'bot.allow_search': ((bool,), False),
    'bot.humanization_profile': ((dict,), False),
    'bot.humanization_profile.empathy': ((int, float), False),
    'bot.humanization_profile.humor': ((int, float), False),
    'bot.humanization_profile.initiative': ((int, float), False),
    'bot.humanization_profile.intimacy_pace': ((int, float), False),
    'bot.humanization_profile.jealousy': ((int, float), False),
    'bot.humanization_profile.tsundere': ((int, float), False),
    'bot.humanization_profile.vulnerability': ((int, float), False),
    'bot.humanization_profile.warmth': ((int, float), False),
    'bot.kaomoji_enable': ((bool,), False),
    'bot.language': ((str,), False),
    'bot.max_reply_chars': ((int,), False),
    'bot.max_reply_chars_proactive': ((int,), False),
    'bot.mention_only_ai_prompt': ((str,), False),
    'bot.mention_only_ai_system_prompt': ((str,), False),
    'bot.mention_only_reply_mode': ((str,), False),
    'bot.mention_only_reply_template': ((str,), False),
    'bot.mention_only_reply_template_with_name': ((str,), False),
    'bot.multi_image_interval_ms': ((int,), False),
    'bot.multi_image_max_count': ((int,), False),
    'bot.multi_reply_chat_max_chars': ((int,), False),
    'bot.multi_reply_chat_max_chunks': ((int,), False),
    'bot.multi_reply_chat_max_lines': ((int,), False),
    'bot.multi_reply_enable': ((bool,), False),
    'bot.multi_reply_interval_ms': ((int,), False),
    'bot.multi_reply_max_chars': ((int,), False),
    'bot.multi_reply_max_chunks': ((int,), False),
    'bot.multi_reply_max_lines': ((int,), False),
    'bot.name': ((str,), False),
    'bot.napcat_media_stage_dir': ((str,), False),
    'bot.nicknames': ((list,), False),
    'bot.private_chat_mode': ((str,), False),
    'bot.private_chat_whitelist': ((list,), False),
    'bot.relationship_boundary_reply_template': ((str,), False),
    'bot.relationship_commitment_min_interactions': ((int,), False),
    'bot.relationship_commitment_min_level': ((int,), False),
    'bot.relationship_commitment_private_only': ((bool,), False),
    'bot.relationship_commitment_terms': ((list,), False),
    'bot.relationship_hard_boundary_enabled': ((bool,), False),
    'bot.relationship_progressive_enable': ((bool,), False),
    'bot.reply_with_at': ((bool,), False),
    'bot.reply_with_quote': ((bool,), False),
    'bot.sanitize_banned_phrases': ((list,), False),
    'bot.short_ping_phrases': ((list,), False),
    'bot.short_ping_require_directed': ((bool,), False),
    'bot.video_send_strategy': ((str,), False),
    'bot.voice_send_max_seconds': ((int,), False),
    'bot.voice_send_music_disable_split': ((bool,), False),
    'bot.voice_send_music_force_full': ((bool,), False),
    'bot.voice_send_split_enable': ((bool,), False),
    'bot.voice_send_split_max_segments': ((int,), False),
    'bot.voice_send_try_full_first': ((bool,), False),
    # chat_split 段（必填）
    'chat_split': ((dict,), True),
    'chat_split.mode': ((str,), False),
    # control 段（必填）
    'control': ((dict,), True),
    'control.chat_mode': ((str,), False),
    'control.emoji_level': ((str,), False),
    'control.knowledge_block_speculative': ((bool,), False),
    'control.knowledge_block_tool_echo': ((bool,), False),
    'control.knowledge_learning': ((str,), False),
    'control.knowledge_max_per_turn': ((int,), False),
    'control.knowledge_min_confidence': ((int, float), False),
    'control.knowledge_require_explicit_user_fact': ((bool,), False),
    'control.login_backlog_import_enable': ((bool,), False),
    'control.login_backlog_import_include_private': ((bool,), False),
    'control.login_backlog_import_lookback_hours': ((int,), False),
    'control.login_backlog_import_max_conversations': ((int,), False),
    'control.login_backlog_import_max_messages_per_conversation': ((int,), False),
    'control.login_backlog_import_max_pages_per_conversation': ((int,), False),
    'control.login_backlog_import_min_interval_seconds': ((int,), False),
    'control.login_backlog_import_only_unread': ((bool,), False),
    'control.login_backlog_llm_summary_enable': ((bool,), False),
    'control.memory_recall_level': ((str,), False),
    'control.send_rate_profile': ((str,), False),
    'control.split_mode': ((str,), False),
    'control.undirected_policy': ((str,), False),
    # emotion 段（必填）
    'emotion': ((dict,), True),
    'emotion.emoji_probability': ((int, float), False),
    'emotion.enable': ((bool,), False),
    'emotion.strike_cooldown_seconds': ((int,), False),
    'emotion.strike_threshold': ((int, float), False),
    'emotion.warn_cooldown_seconds': ((int,), False),
    'emotion.warn_threshold': ((int, float), False),
    # image_gen 段
    'image_gen': ((dict,), False),
    'image_gen.custom_allow_terms': ((list,), False),
    'image_gen.custom_block_terms': ((list,), False),
    'image_gen.default_model': ((str,), False),
    'image_gen.default_size': ((str,), False),
    'image_gen.enable': ((bool,), False),
    'image_gen.max_prompt_length': ((int,), False),
    'image_gen.models': ((list,), False),
    'image_gen.nsfw_filter': ((bool,), False),
    'image_gen.post_review_enable': ((bool,), False),
    'image_gen.post_review_fail_closed': ((bool,), False),
    'image_gen.post_review_max_tokens': ((int,), False),
    'image_gen.post_review_model': ((str,), False),
    'image_gen.prompt_review_enable': ((bool,), False),
    'image_gen.prompt_review_fail_closed': ((bool,), False),
    'image_gen.prompt_review_max_tokens': ((int,), False),
    'image_gen.prompt_review_model': ((str,), False),
    # knowledge_update 段
    'knowledge_update': ((dict,), False),
    'knowledge_update.llm_extractor_enable': ((bool,), False),
    'knowledge_update.llm_timeout_seconds': ((int,), False),
    # media 段（必填）
    'media': ((dict,), True),
    'media.asr': ((dict,), False),
    'media.asr.compute_type': ((str,), False),
    'media.asr.device': ((str,), False),
    'media.asr.enable': ((bool,), False),
    'media.asr.model_size': ((str,), False),
    'media.asr.timeout_seconds': ((int,), False),
    # memory 段（必填）
    'memory': ((dict,), True),
    'memory.embedding_retention_days': ((int,), False),
    'memory.enable_daily_log': ((bool,), False),
    'memory.enable_vector_memory': ((bool,), False),
    'memory.max_context_messages': ((int,), False),
    'memory.privacy_filter': ((bool,), False),
    'memory.retrieve_top_k': ((int,), False),
    'memory.summary_every_n_messages': ((int,), False),
    'memory.vector_dim': ((int,), False),
    # music 段（必填）
    'music': ((dict,), True),
    'music.allow_insecure_api_base': ((bool,), False),
    'music.api_base': ((str,), False),
    'music.api_bases': ((list,), False),
    'music.artist_guard_allow_mismatch_fallback': ((bool,), False),
    'music.artist_guard_enable': ((bool,), False),
    'music.break_limit_enable': ((bool,), False),
    'music.cache_dir': ((str,), False),
    'music.cache_keep_files': ((int,), False),
    'music.enable': ((bool,), False),
    'music.local_source_enable': ((bool,), False),
    'music.max_voice_duration_seconds': ((int,), False),
    'music.timeout_seconds': ((int,), False),
    'music.trial_max_duration_ms': ((int,), False),
    'music.unblock_api_base': ((str,), False),
    'music.unblock_enable': ((bool,), False),
    'music.unblock_sources': ((str,), False),
    'music.unreachable_cooldown_seconds': ((int,), False),
    'music.upstream_budget_seconds': ((int,), False),
    # output 段（必填）
    'output': ((dict,), True),
    'output.group_overrides': ((dict,), False),
    'output.group_style_overrides': ((dict,), False),
    'output.style_instruction': ((str,), False),
    'output.token_saving': ((bool,), False),
    'output.verbosity': ((str,), False),
    # prompt_control 段（必填）
    'prompt_control': ((dict,), True),
    'prompt_control.enable': ((bool,), False),
    'prompt_control.global_prefix': ((str,), False),
    'prompt_control.global_suffix': ((str,), False),
    'prompt_control.low_iq_mode': ((bool,), False),
    'prompt_control.persona_override': ((str,), False),
    # queue 段（必填）
    'queue': ((dict,), True),
    'queue.cancel_previous_mode': ((str,), False),
    'queue.cancel_previous_on_interrupt_request': ((bool,), False),
    'queue.cancel_previous_on_new': ((bool,), False),
    'queue.download_process_timeout_seconds': ((int,), False),
    'queue.group_concurrency': ((int,), False),
    'queue.group_isolate_by_user': ((bool,), False),
    'queue.message_ttl_seconds': ((int,), False),
    'queue.process_timeout_seconds': ((int,), False),
    'queue.single_inflight_per_conversation': ((bool,), False),
    'queue.smart_interrupt_cross_user_enable': ((bool,), False),
    'queue.smart_interrupt_enable': ((bool,), False),
    'queue.smart_interrupt_min_pending': ((int,), False),
    'queue.smart_interrupt_require_directed': ((bool,), False),
    'queue.smart_interrupt_same_user_enable': ((bool,), False),
    'queue.video_process_timeout_seconds': ((int,), False),
    'queue.web_process_timeout_seconds': ((int,), False),
    # routing 段（必填）
    'routing': ((dict,), True),
    'routing.ai_gate_min_confidence': ((int, float), False),
    'routing.followup_fast_path_enable': ((bool,), False),
    'routing.followup_min_confidence': ((int, float), False),
    'routing.fragment_join_enable': ((bool,), False),
    'routing.min_confidence': ((int, float), False),
    'routing.mode': ((str,), False),
    'routing.non_directed_min_confidence': ((int, float), False),
    'routing.trust_ai_fully': ((bool,), False),
    'routing.zero_threshold_disables_undirected': ((bool,), False),
    # safety 段（必填）
    'safety': ((dict,), True),
    'safety.custom_allow_terms': ((list,), False),
    'safety.custom_block_terms': ((list,), False),
    'safety.group_profiles': ((dict,), False),
    'safety.output_sensitive_words': ((dict,), False),
    'safety.political_allow_terms': ((list,), False),
    'safety.political_deflect_enable': ((bool,), False),
    'safety.political_deflect_reply': ((str,), False),
    'safety.political_terms': ((list,), False),
    'safety.profile': ((str,), False),
    'safety.scale': ((int,), False),
    # search 段（必填）
    'search': ((dict,), True),
    'search.allow_private_network': ((bool,), False),
    'search.enable': ((bool,), False),
    'search.max_image_results': ((int,), False),
    'search.max_results': ((int,), False),
    'search.scrape': ((dict,), False),
    'search.scrape.llm_max_tokens': ((int,), False),
    'search.scrape.max_text_len': ((int,), False),
    'search.scrape.timeout_seconds': ((int,), False),
    'search.searxng_base': ((str,), False),
    'search.timeout_seconds': ((int,), False),
    'search.tool_interface': ((dict,), False),
    'search.tool_interface.browser_enable': ((bool,), False),
    'search.tool_interface.enable': ((bool,), False),
    'search.tool_interface.github_api_base': ((str,), False),
    'search.tool_interface.github_enable': ((bool,), False),
    'search.tool_interface.github_token': ((str,), False),
    'search.tool_interface.web_fetch_max_chars': ((int,), False),
    'search.tool_interface.web_fetch_max_pages': ((int,), False),
    'search.tool_interface.web_fetch_timeout_seconds': ((int,), False),
    'search.video_resolver': ((dict,), False),
    'search.video_resolver.cookies_from_browser': ((str,), False),
    'search.video_resolver.download_max_mb': ((int,), False),
    'search.video_resolver.download_timeout_seconds': ((int,), False),
    'search.video_resolver.enable': ((bool,), False),
    'search.video_resolver.metadata_timeout_seconds': ((int,), False),
    'search.video_resolver.parse_api_base': ((str,), False),
    'search.video_resolver.parse_api_enable': ((bool,), False),
    'search.video_resolver.require_audio_for_send': ((bool,), False),
    'search.video_resolver.resolve_total_timeout_seconds': ((int,), False),
    'search.video_resolver.search_analysis_max_duration_seconds': ((int,), False),
    'search.video_resolver.search_max_duration_seconds': ((int,), False),
    'search.video_resolver.search_send_max_duration_seconds': ((int,), False),
    'search.video_resolver.validate_direct_url': ((bool,), False),
    'search.vision': ((dict,), False),
    'search.vision.enable': ((bool,), False),
    'search.vision.fallback_models': ((list,), False),
    'search.vision.max_tokens': ((int,), False),
    'search.vision.model_supports_image': ((str,), False),
    'search.vision.native_blocks_enable': ((bool,), False),
    'search.vision.native_max_image_bytes': ((int,), False),
    'search.vision.native_max_images': ((int,), False),
    'search.vision.route_text_model_to_local': ((bool,), False),
    'search.vision.temperature': ((int, float), False),
    'search.vision.timeout_seconds': ((int,), False),
    # search_followup 段（必填）
    'search_followup': ((dict,), True),
    'search_followup.enable': ((bool,), False),
    'search_followup.max_choices': ((int,), False),
    'search_followup.number_choice_enable': ((bool,), False),
    'search_followup.resend_enable': ((bool,), False),
    'search_followup.rotate_choice_enable': ((bool,), False),
    'search_followup.ttl_minutes': ((int,), False),
    # send_rate 段（必填）
    'send_rate': ((dict,), True),
    'send_rate.enable': ((bool,), False),
    'send_rate.max_per_window': ((int,), False),
    'send_rate.profile': ((str,), False),
    'send_rate.warn_threshold': ((int,), False),
    'send_rate.window_seconds': ((int,), False),
    # trigger 段（必填）
    'trigger': ((dict,), True),
    'trigger.active_session_free_window_seconds': ((int,), False),
    'trigger.active_session_score_bonus': ((int, float), False),
    'trigger.active_session_timeout_minutes': ((int,), False),
    'trigger.ai_listen_enable': ((bool,), False),
    'trigger.ai_listen_interval_seconds': ((int,), False),
    'trigger.ai_listen_keyword_enable': ((bool,), False),
    'trigger.ai_listen_keyword_pass_enable': ((bool,), False),
    'trigger.ai_listen_keywords': ((list,), False),
    'trigger.ai_listen_max_probes_per_hour': ((int,), False),
    'trigger.ai_listen_min_keyword_hits': ((int,), False),
    'trigger.ai_listen_min_messages': ((int,), False),
    'trigger.ai_listen_min_score': ((int, float), False),
    'trigger.ai_listen_min_unique_users': ((int,), False),
    'trigger.busy_window_seconds': ((int,), False),
    'trigger.delegate_undirected_min_signal': ((int, float), False),
    'trigger.delegate_undirected_to_ai': ((bool,), False),
    'trigger.followup_max_turns': ((int,), False),
    'trigger.followup_reply_window_seconds': ((int,), False),
    'trigger.media_only_allow_in_followup': ((bool,), False),
    'trigger.media_only_requires_directed': ((bool,), False),
    'trigger.overload_enable': ((bool,), False),
    'trigger.overload_min_messages': ((int,), False),
    'trigger.overload_min_unique_users': ((int,), False),
    'trigger.overload_notice_cooldown_seconds': ((int,), False),
    'trigger.overload_pause_seconds': ((int,), False),
    # video_analysis 段
    'video_analysis': ((dict,), False),
    'video_analysis.acfun': ((dict,), False),
    'video_analysis.acfun.cookie': ((str,), False),
    'video_analysis.acfun.enable': ((bool,), False),
    'video_analysis.acfun.timeout_seconds': ((int,), False),
    'video_analysis.bilibili': ((dict,), False),
    'video_analysis.bilibili.bili_jct': ((str,), False),
    'video_analysis.bilibili.comments_top_n': ((int,), False),
    'video_analysis.bilibili.danmaku_top_n': ((int,), False),
    'video_analysis.bilibili.enable': ((bool,), False),
    'video_analysis.bilibili.sessdata': ((str,), False),
    'video_analysis.douyin': ((dict,), False),
    'video_analysis.douyin.cookie': ((str,), False),
    'video_analysis.douyin.enable': ((bool,), False),
    'video_analysis.keyframe_count': ((int,), False),
    'video_analysis.keyframe_max_dimension': ((int,), False),
    'video_analysis.keyframe_quality': ((int,), False),
    'video_analysis.kuaishou': ((dict,), False),
    'video_analysis.kuaishou.cookie': ((str,), False),
    'video_analysis.kuaishou.enable': ((bool,), False),
    'video_analysis.qzone': ((dict,), False),
    'video_analysis.qzone.cookie': ((str,), False),
    'video_analysis.qzone.enable': ((bool,), False),
}


# 布尔是 int 的子类，必须单独判定，否则 True 会被 int 接受。
def _type_ok(value: Any, expected: tuple[type, ...]) -> bool:
    if bool in expected:
        return isinstance(value, bool)
    for typ in expected:
        if typ is int and isinstance(value, int) and not isinstance(value, bool):
            return True
        if typ is float and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if typ is not int and typ is not float and isinstance(value, typ):
            return True
    return False


def _type_names(expected: tuple[type, ...]) -> str:
    return "|".join(t.__name__ for t in expected)


class ConfigValidationError(RuntimeError):
    """strict 模式下配置校验失败时抛出，用于阻断启动 / 热重载。"""


def _get_dotpath(config: dict[str, Any], dotpath: str) -> tuple[bool, Any]:
    """沿点路径取配置值。返回 (是否存在, 值)。中间节点不是 dict 视为不存在。"""
    node: Any = config
    for key in dotpath.split("."):
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def validate_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    """按 CONFIG_SCHEMA 校验配置。

    返回 issue 列表，每项:
        {"kind": "type_mismatch" | "missing", "path", "expected", "actual", "value"}

    不抛异常；调用方决定如何记录（约定启动时只记 warning，不阻断启动）。
    """
    issues: list[dict[str, Any]] = []
    for dotpath, (expected, required) in CONFIG_SCHEMA.items():
        present, value = _get_dotpath(config, dotpath)
        if not present:
            if required:
                issues.append(
                    {
                        "kind": "missing",
                        "path": dotpath,
                        "expected": _type_names(expected),
                        "actual": "missing",
                        "value": None,
                    }
                )
            continue
        if not _type_ok(value, expected):
            issues.append(
                {
                    "kind": "type_mismatch",
                    "path": dotpath,
                    "expected": _type_names(expected),
                    "actual": type(value).__name__,
                    "value": value,
                }
            )
    return issues
