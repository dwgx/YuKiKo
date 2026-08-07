# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current takeover entry

Read [`TAKEOVER-2026-08-07.md`](TAKEOVER-2026-08-07.md) before relying on any status, test count,
live-run claim, or next-step order. [`HANDOFF.md`](HANDOFF.md) is only the short router to that page;
the older long handoff is archived under `docs/archive/`. Do not treat historical logs in
`MIGRATION_TODO.md` or `NEXT-SESSION-PLAN.md` as current verification.

## Overview

YuKiKo is a NoneBot2 + OneBot V11 QQ bot (Python 3.11+) with an LLM agent, **~179 built-in
agent tools** (plus ~12 more from plugins), a multi-provider model layer, a plugin system, and
a React admin panel. It
reaches QQ through NapCat over reverse WebSocket at `/onebot/v11/ws`. Comments, logs, and
docs are primarily Simplified Chinese — match that.

## Commands

```bash
# Environment (creates .venv, installs pinned deps, repairs a broken venv)
python scripts/deploy.py

# Run (start.sh/.bat repair the venv and rebuild webui/dist when stale)
bash start.sh                 # macOS/Linux
.\start.bat                   # Windows
python main.py                # direct, needs an activated venv
python main.py --setup        # force the interactive CLI config wizard

# Tests (pytest.ini: testpaths=tests, pythonpath=., asyncio_mode=auto)
python -m pytest -q
python -m pytest tests/test_router_media_fallback_regression.py -q
python -m pytest tests/test_engine_bot_strategy_regression.py::EngineBotStrategyDirectiveTests -q

# Lint / format (pyproject.toml: line-length 120, target py312)
ruff check .
ruff format .

# WebUI
bash build-webui.sh           # install-if-needed + build (build-webui.bat on Windows)
cd webui && npm run dev       # dev server :3000, proxies /api/webui → 127.0.0.1:8081

# Broad self-checks before touching prompts / routing / agent / API strategy
python scripts/project_takeover_selfcheck.py   # agent_deep_selfcheck + key regressions
python scripts/agent_deep_selfcheck.py
```

Linux deploys install a `yukiko` CLI (`scripts/yukiko_manager.sh`): `status`, `logs`,
`doctor --strict`, `backup`, `update --fast`, `restart`, `uninstall`.

## Startup modes (`main.py`)

Missing `config/config.yml` → serve the WebUI setup wizard, falling back to the CLI wizard
when `webui/dist` is absent. `--setup` / `setup` in argv → force the CLI wizard. Otherwise:
`nonebot.init()`, register the OneBot V11 adapter, `create_engine()` + `register_handlers()`
from `app.py`, then mount the WebUI router and serve `webui/dist` as an SPA on NoneBot's
ASGI app.

## Message pipeline

`app.py` OneBot handler → build `EngineMessage` → `GroupQueueDispatcher.submit()`
(`core/queue.py`: per-conversation serialization, smart interrupt of superseded turns, TTL
expiry, overload notices) → `YukikoEngine.handle_message()` (`core/engine.py`) → response
rendered back into OneBot segments by `app.py` / `app_helpers.py`.

Inside `handle_message`: message-id dedupe → group whitelist gate (`core/admin.py`) →
remember incoming media for later "sent image, then asked" follow-ups → fragmented-message
merge → `TriggerEngine` (attention gate; `not_directed` / `overload_notice` return here) →
`_try_agent_path` (`AgentLoop` + `PromptNavigator`) → `RouterEngine` (`_route_with_failover`)
**only as fallback** → `EngineResponse`.

**The agent runs first, not the router.** `RouterEngine` is the legacy path now: it is reached
only when `agent.enable` / `model_client.enabled` is off, or when `_try_agent_path` returns
`None` (agent timeout, parse failure, or an empty reply that is not deliberate silence). In the
live deployment every turn takes the agent path — `navigator_tool_scope` fires on every turn in
a running log while `router_decision` never appears.

`_self_check_decision` is **deleted** (commit `37f60d4`, together with
`_normalize_decision_with_tool_policy`). It used to be a local 13-rule veto over the LLM router.
No local veto layer is left: "should I speak in this group at all" is the model's call, expressed
as an empty `final_answer`, and structural facts (image segments, URLs, who was @'d, permission
level) are fed in as evidence instead of overriding the model. Two tombstone comments in
`core/engine.py` mark the former sites — grep `_self_check_decision` and you get only those. The
five `self_check.*` config keys were removed from all three sources in `7e5e83e`; the `at_other`
hard veto went with them.

What still gates replies is numeric, not semantic, and worth knowing before chasing a "why did it
stay quiet" bug:

- `TriggerEngine` returns `not_directed` for undirected group messages, and `handle_message`
  ignores them **before any model call**. So "decides on its own to chime in" is not live yet.
- the `routing.*` threshold chain on the fallback router side (`non_directed_min_confidence`,
  `ai_gate_min_confidence`, `followup_min_confidence`), plus `non_directed_threshold_disabled`
  when the threshold is 0 and `router_low_confidence` below it.

Both are admin-configured numbers, not keyword rules — leave them alone when removing heuristics.

Outbound sending in `app.py` carries its own protections: semantic text splitting
(`core/chat_splitter.py`), token-bucket rate limiting, per-group send blocking on repeated
API errors, and bot-level send suspension. Keep these intact when touching send paths.

## Two distinct tool systems

Do not conflate them:

- **Router methods** — `ToolExecutor.execute()` in `core/tools.py`, dispatched by `action`
  string (`search`, `generate_image`, `music_search`, `music_play`, `music_play_by_id`,
  `bilibili_audio_extract`, `get_group_member_count`, `plugin_call`, `send_segment`, …).
  One-shot, chosen by the router, no reasoning loop.
- **Agent tools** — `AgentToolRegistry` (`core/agent_tools_registry.py`), invoked
  iteratively by `AgentLoop.run()` in `core/agent.py` via native tool calling.

`core/agent_tools.py` is a **re-export hub only** — keep it that way. Implementations live in
`core/agent_tools_{napcat,search,media,admin,utility,memory,knowledge,social,web}.py`.
`core/tools_{video,vision,search,github,music_exec,ai_method}.py` hold the heavy router-side
implementations. `PromptNavigator` (`core/prompt_navigator.py`) gates which agent tools are
visible per intent and permission level.

Registration is three calls in `YukikoEngine.__init__`, not one — `register_builtin_tools`
(`core/agent_tools_registry.py`), then `register_enhanced_tools` (`core/enhanced_tools.py`:
affinity, card builder, image gen), then `register_sticker_tools` (`core/agent_tools_utility.py`).
Counting tools with only the first call undercounts by ~18. Some registrations are
config-gated: `search.tool_interface.github_enable` off means the github tools are not
registered at all (`4318d2c` — a registered-but-disabled tool burns a whole reasoning turn).

**Per turn the model sees one section's tools, never all 179.** `scoped_tools()` intersects the
active section with the visible set; live logs show 3–18 schemas per turn. Budget arithmetic that
assumes the full registry is wrong by an order of magnitude.

## Configuration

Three layers: `.env` (secrets, host/port, `ONEBOT_ACCESS_TOKEN`, `WEBUI_TOKEN`, per-provider
API keys) → `config/config.yml` (generated, gitignored) → `plugins/config/<name>.yml`
(overrides `config.yml`'s `plugins.<name>`).

`config/templates/master.template.yml` is the canonical schema, with `config:` and
`prompts:` roots. `core/config_templates.py` merges it, self-heals missing keys on upgrade,
and backfills the template from `_built_in_config_defaults()` / `_built_in_prompts_defaults()`.

**Adding a config key requires updating both the template and the built-in defaults** —
writing only to a local `config.yml` means upgraded installs never see it.

Prompts live in `config/prompts.yml`, read through `core/prompt_loader.py` dot-path getters
and composed by `core/system_prompts.py` + `core/prompt_policy.py`. Never hardcode
user-facing prompt text in engine code. Note `config/prompts.yml` **is git-tracked** —
`.gitignore` only covers `config/config.yml`. It is not runtime state.

`strip_heuristic_prompt_lists` (`core/config_templates.py`) now runs on all three load paths
(built-in defaults, `load_prompts_template()`, `prompt_loader.reload()`) since `562a3bd`. It
**deletes** any key whose name ends in `_cues` / `_patterns` / `_regexes` / `_tokens` and whose
value is a list, then rewrites the file. So a keyword list added to `prompts.yml` disappears on
next boot, logged as `prompt_cue_lists_pruned`. That is deliberate — moving a word list from
Python to YAML is not progress. Same-named keys holding prose are untouched.

Menu / navigator changes need **all three sources** edited together:
`default_prompt_navigator_payload()` (`core/prompt_navigator.py`), the template, and
`config/prompts.yml`. The template wins over the Python payload at runtime, while
`_merge_with_defaults` only backfills missing keys and never overwrites — so editing one source
can leave behaviour correct on this machine and broken on a fresh install, or vice versa.

## Hot reload

`/yukibot` (or `/yukiko`) in chat triggers `YukikoEngine.reload_config()`, which re-reads
config and prompts and rebuilds only lightweight components (admin, safety, emotion,
personality, trigger, thinking, router, tools, plugins) — `ModelClient`, `MemoryEngine`, and
other heavyweight state deliberately survive. `apply_config_patch()` (WebUI / admin config
edits) writes the patch then delegates to `reload_config()`;
`refresh_runtime_policy_components()` is the lighter in-memory path used by admin
behavior-mode switches, rebuilding only trigger and router. New stateful components must be
wired into these methods or they will silently serve stale config.

## Model layer

`services/model_client.py` wraps per-provider clients (`openai`, `anthropic`, `deepseek`,
`gemini`, `qwen`, `moonshot`, `mistral`, `zhipu`, `xai`, `openrouter`, `siliconflow`,
`skiapi`, `newapi`, `openai_compatible`). `_invoke_with_failover()` walks a fallback chain
distinguishing fatal, transient, and unsupported-method errors; `supports_vision_input()` /
`supports_native_tool_calling()` gate capability-dependent paths. A new provider means a
client module plus registration in `_resolve_provider_config()`.

## Plugins

`core/plugin_registry.py` auto-discovers `Plugin` classes in `plugins/*.py` (skipping `_`-
prefixed and `__init__.py`). Lifecycle: discover → optional `needs_setup()` /
`interactive_setup()` → instantiate → `async setup(config, context)` → `handle(message,
context)` → `async teardown()`. Via `context.agent_tool_registry`, plugins register tools,
`PromptHint`s, and context providers without core edits. `plugins/example_plugin.py` is both
template and reference documentation.

## WebUI

Backend: `core/webui.py` (`APIRouter` at `/api/webui`) plus `webui_auth_routes.py`,
`webui_log_routes.py`, `webui_cookie_routes.py`, `webui_setup_support.py` (setup wizard),
sharing `WebUIRouteContext` (`core/webui_route_context.py`) for auth and engine access.
`webui_chat_helpers.py` is helpers only, no router. Auth is `WEBUI_TOKEN`-based.
Frontend: React 18 + Vite + TypeScript + Tailwind + HeroUI in `webui/src`, base `/webui/`.
A missing `webui/dist` returns 503 with build instructions rather than failing startup.

## Permissions

`AgentLoop._resolve_permission_level()` returns `super_admin` (listed in
`admin.super_users`), `group_admin` (owner/admin role **in a whitelisted group**), or `user`.
High-risk tools (bans, config mutation) route through `_guard_high_risk_tool_call()` and
require explicit confirmation.

## Conventions and gotchas

- `app.py` + `app_helpers.py` are one logical unit. `app.py` calls
  `_app_helpers.bind_runtime_dependencies(...)` then `from app_helpers import *`; helpers
  reach `app.py`-owned runtime objects through injected globals, never by importing `app`.
  This avoids a circular import — preserve the pattern.
- Tests are `unittest.TestCase` classes executed by pytest. Engine tests construct
  `YukikoEngine.__new__(YukikoEngine)` and set attributes by hand, since the real
  constructor needs models and network. Follow that when adding engine tests.
- Logging is structured single-line: `event_name | trace=%s | key=%s`. Thread
  `EngineMessage.trace_id` through router, agent, and tool layers.
- `core/engine.py` (~7.4k lines) and `core/agent.py` (~5.4k) are too large to read whole —
  grep for the specific method. Both shrank by ~1.1k lines each in `37f60d4` / `af6fe30`, so
  **any line number written down before those commits is wrong**. Grep, never slice by
  remembered offset: these files are dense runs of adjacent `def`s, and editing the wrong one
  by stale offset produces no syntax error, just a silently wrong function.
- `requirements.txt` pins exact versions. Some Ruff ignores are deliberate (`F401`/`E402`
  for re-export hubs, `F821` for `app_helpers.py`) — do not "fix" them.
- Runtime state goes under `storage/` (or `YUKIKO_DATA_DIR`) via `PathResolver`
  (`core/paths.py`). `storage/`, `config/config.yml`, `plugins/config/*.yml`, and `.env` are
  gitignored.
- Extra docs: `docs/zh-CN/ARCHITECTURE.md` (design rationale), `docs/PLUGIN_GUIDE.md`,
  `docs/zh-CN/TAKEOVER_IMPROVEMENT_PLAN.md`.
