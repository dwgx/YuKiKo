#!/usr/bin/env bash
# NapCat (macOS) 一键接线：写 OneBot 反向 ws 配置 + 用 --no-sandbox 重启 QQ。
#
# 为什么需要这个脚本（2026-08-06 实测排查结论）：
#
# 1. NapCat 只在 QQ 带 `--no-sandbox` 启动时才加载。注入点
#    ~/Library/Containers/com.tencent.qq/Data/Documents/loadNapCat.js 第一行是
#      const loadNapcat = process.argv.includes('--no-sandbox');
#    不带这个参数就直接走原版 QQ 启动流程，NapCat 完全不加载 —— 表现为机器人
#    零 qq_recv、8081 只有 LISTEN 没有 ESTABLISHED。从 Finder/Dock 双击打开 QQ
#    永远不会带这个参数，所以图形化启动 = NapCat 不工作。
#
# 2. 反向 ws 目标写在 config/onebot11_<QQ号>.json 里。该文件缺失时 NapCat
#    即使加载了也没有连接目标。实测排查时该文件不存在，配置目录只有 napcat.json。
#
# 用法:
#   bash scripts/napcat_macos.sh            # 写配置 + 重启 QQ
#   bash scripts/napcat_macos.sh --config   # 只写配置，不动 QQ
#   bash scripts/napcat_macos.sh --status   # 只看当前状态
#
# 注意：重启 QQ 会关闭当前 QQ 窗口。多数情况下 QQ 会自动恢复登录态，
# 但如果本机登录凭据过期，需要手机扫码——脚本会提示但无法代替你完成。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QQ_APP="/Applications/QQ.app"
QQ_BIN="$QQ_APP/Contents/MacOS/QQ"
NAPCAT_DIR="$HOME/Library/Containers/com.tencent.qq/Data/Documents/napcat"
# macOS 版 NapCat 读的是 Application Support 下这个目录，**不是** Documents/napcat/config。
# 后者是 Windows 发行版的残留（同目录下有 .exe / .bat / .dll），写进去完全无效 ——
# 2026-08-06 我第一次排查就写错了地方，白重启一次 QQ。
CONFIG_DIR="$HOME/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/NapCat/config"
LEGACY_CONFIG_DIR="$NAPCAT_DIR/config"
LOADER="$HOME/Library/Containers/com.tencent.qq/Data/Documents/loadNapCat.js"
ENV_FILE="$ROOT_DIR/.env"

log() { printf '[napcat] %s\n' "$*"; }
die() { printf '[napcat] 错误: %s\n' "$*" >&2; exit 1; }

read_env() {
  # 从 .env 取值，不 source（.env 里可能有不适合执行的内容）
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  grep -m1 "^${key}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r\n' || true
}

detect_uin() {
  # 优先用日志里出现过的 bot QQ 号；否则用已有的 onebot11_*.json 文件名
  local log_file="$ROOT_DIR/storage/logs/yukiko.log"
  local uin=""
  if [[ -f "$log_file" ]]; then
    uin="$(grep -oE 'bot=[0-9]{5,}' "$log_file" 2>/dev/null | head -1 | cut -d= -f2 || true)"
  fi
  if [[ -z "$uin" ]]; then
    local existing
    existing="$(ls "$CONFIG_DIR"/onebot11_*.json 2>/dev/null | head -1 || true)"
    if [[ -n "$existing" ]]; then
      uin="$(basename "$existing" .json | sed 's/^onebot11_//')"
    fi
  fi
  printf '%s' "$uin"
}

show_status() {
  log "── 状态 ──"
  if [[ -x "$QQ_BIN" ]]; then log "QQ 可执行文件: 存在"; else log "QQ 可执行文件: 缺失 ($QQ_BIN)"; fi
  if [[ -f "$LOADER" ]]; then log "NapCat 注入点: 存在"; else log "NapCat 注入点: 缺失 —— 需要先跑 NapCatInstaller"; fi

  local main_field
  main_field="$(grep -o '"main"[^,]*' "$QQ_APP/Contents/Resources/app/package.json" 2>/dev/null || true)"
  if [[ "$main_field" == *loadNapCat* ]]; then
    log "package.json 注入: 已生效"
  else
    log "package.json 注入: 未生效 —— NapCat 不会被加载"
  fi

  if [[ -f "$LEGACY_CONFIG_DIR/onebot11_"*.json ]] 2>/dev/null; then
    log "注意: $LEGACY_CONFIG_DIR 下有 onebot 配置，但 macOS 版不读那里（Windows 残留目录）"
  fi

  # NapCat 是否真的加载了：它加载后会起自己的 WebUI（默认 6099）
  if lsof -nP -iTCP:6099 -sTCP:LISTEN >/dev/null 2>&1; then
    log "NapCat 运行时: 已加载（6099 在监听）"
  else
    log "NapCat 运行时: 未加载 —— QQ 大概没带 --no-sandbox"
  fi

  local pids
  pids="$(pgrep -f "$QQ_BIN" 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    log "QQ 进程: 未运行"
  else
    local pid_main
    pid_main="$(printf '%s' "$pids" | head -1)"
    if ps -p "$pid_main" -o command= 2>/dev/null | grep -q -- '--no-sandbox'; then
      log "QQ 进程: 运行中 (PID $pid_main) 带 --no-sandbox → NapCat 会加载"
    else
      log "QQ 进程: 运行中 (PID $pid_main) **不带 --no-sandbox** → NapCat 不加载"
    fi
  fi

  local uin cfg
  uin="$(detect_uin)"
  if [[ -n "$uin" ]]; then
    cfg="$CONFIG_DIR/onebot11_${uin}.json"
    if [[ -f "$cfg" ]]; then
      log "OneBot 配置: 存在 ($(basename "$cfg"))"
      if grep -q '"enable": true' "$cfg" 2>/dev/null; then
        log "  反向 ws: 已启用 → $(grep -o '"url"[^,]*' "$cfg" | head -1 | cut -d'"' -f4)"
      else
        log "  反向 ws: 未启用"
      fi
    else
      log "OneBot 配置: 缺失 ($(basename "$cfg")) —— NapCat 没有连接目标"
    fi
  else
    log "OneBot 配置: 无法确定 QQ 号（日志里没有 bot= 记录，配置目录也没有现成文件）"
  fi

  local port
  port="$(read_env PORT)"; port="${port:-8081}"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    local established
    established="$(lsof -nP -iTCP:"$port" 2>/dev/null | grep -c ESTABLISHED || true)"
    log "机器人端口 $port: 监听中，ESTABLISHED=$established"
    [[ "${established:-0}" -gt 0 ]] && log "  → NapCat 已连上" || log "  → NapCat 未连上"
  else
    log "机器人端口 $port: 无监听 —— 先启动机器人 (bash start.sh)"
  fi
}

config_is_correct() {
  # 已有配置是否已经指向本机机器人且启用。是则不动它 —— 盲目覆盖会丢掉
  # 用户在 NapCat WebUI 里做的其它设置。
  local target="$1" port="$2"
  [[ -f "$target" ]] || return 1
  python3 - "$target" "$port" <<'PYEOF'
import json, sys
from pathlib import Path
try:
    conf = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)
port = sys.argv[2]
clients = ((conf.get("network") or {}).get("websocketClients") or [])
want = f"/onebot/v11/ws"
for c in clients:
    if not c.get("enable"):
        continue
    url = str(c.get("url", ""))
    if want in url and f":{port}" in url:
        sys.exit(0)
sys.exit(1)
PYEOF
}

write_config() {
  [[ -d "$NAPCAT_DIR" ]] || die "NapCat 未安装: $NAPCAT_DIR 不存在"
  mkdir -p "$CONFIG_DIR"

  local uin token port
  uin="$(detect_uin)"
  [[ -n "$uin" ]] || die "无法确定 QQ 号。先让机器人连上过一次，或手工建 config/onebot11_<QQ号>.json"
  token="$(read_env ONEBOT_ACCESS_TOKEN)"
  port="$(read_env PORT)"; port="${port:-8081}"

  local target="$CONFIG_DIR/onebot11_${uin}.json"

  if config_is_correct "$target" "$port"; then
    log "配置已正确指向 ws://127.0.0.1:${port}/onebot/v11/ws，跳过写入"
    log "  （不覆盖，避免丢掉你在 NapCat WebUI 里的其它设置）"
    return 0
  fi

  if [[ -f "$target" ]]; then
    cp "$target" "${target}.bak"
    log "已备份原配置: $(basename "$target").bak"
  fi

  # 字段与默认值取自 napcat.mjs 里 websocketClients 的 schema（2026-08-06 核对）
  python3 - "$target" "$token" "$port" <<'PYEOF'
import json, sys
from pathlib import Path

target, token, port = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
conf = {
    "network": {
        "httpServers": [],
        "httpSseServers": [],
        "httpClients": [],
        "websocketServers": [],
        "websocketClients": [
            {
                "name": "yukiko",
                "enable": True,
                "url": f"ws://127.0.0.1:{port}/onebot/v11/ws",
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "reconnectInterval": 5000,
                "token": token,
                "debug": False,
                "heartInterval": 30000,
            }
        ],
        "plugins": [],
    },
    "musicSignUrl": "",
    "enableLocalFile2Url": False,
    "parseMultMsg": True,
}
target.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"[napcat] 已写入配置: {target.name}")
print(f"[napcat]   url   = ws://127.0.0.1:{port}/onebot/v11/ws")
print(f"[napcat]   token = {'已设置' if token else '空（机器人若要求 token 会连不上）'}")
PYEOF
}

restart_qq() {
  [[ -x "$QQ_BIN" ]] || die "找不到 QQ 可执行文件: $QQ_BIN"

  local pids
  pids="$(pgrep -f "$QQ_BIN" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    log "关闭当前 QQ（优雅退出）..."
    osascript -e 'tell application "QQ" to quit' >/dev/null 2>&1 || true
    local waited=0
    while pgrep -f "$QQ_BIN" >/dev/null 2>&1 && [[ $waited -lt 12 ]]; do
      sleep 1; waited=$((waited + 1))
    done
    if pgrep -f "$QQ_BIN" >/dev/null 2>&1; then
      log "优雅退出超时，强制结束"
      pkill -f "$QQ_BIN" 2>/dev/null || true
      sleep 2
    fi
  fi

  log "用 --no-sandbox 启动 QQ（这是 NapCat 加载的唯一条件）..."
  nohup "$QQ_BIN" --no-sandbox >/dev/null 2>&1 &
  disown 2>/dev/null || true

  log "等待 NapCat 连上机器人..."
  local port established waited=0
  port="$(read_env PORT)"; port="${port:-8081}"
  while [[ $waited -lt 60 ]]; do
    established="$(lsof -nP -iTCP:"$port" 2>/dev/null | grep -c ESTABLISHED || true)"
    if [[ "${established:-0}" -gt 0 ]]; then
      # 变量必须加花括号：后面紧跟全角括号时 zsh 会把它并进变量名，
      # 报 `established?: unbound variable`。ASCII 分隔符没有这个问题。
      log "已连上（ESTABLISHED=${established}，用了 ${waited}s）"
      return 0
    fi
    sleep 3; waited=$((waited + 3))
  done

  log "60s 内没连上。可能原因："
  log "  1. QQ 需要重新登录（手机扫码）—— 看一下 QQ 窗口"
  log "  2. 机器人没在跑 —— 端口 $port 是否在监听"
  log "  3. token 不匹配 —— 对比 .env 的 ONEBOT_ACCESS_TOKEN 与配置里的 token"
  return 1
}

set_auto_login() {
  # 让 NapCat 启动时自动选中该账号登录，免去每次扫码。
  #
  # 前提：本机存在该账号的已保存凭据（QQ 的 global/nt_db/login.db，加密不可读）。
  # 有凭据 -> 直接自动登录；没有 -> 仍会停在登录界面，扫码一次之后凭据落盘，
  # 从下次起自动生效。所以这个设置两种情况下都是对的，只是首次可能仍需扫码。
  local uin="$1"
  local webui="$CONFIG_DIR/webui.json"
  [[ -f "$webui" ]] || { log "跳过自动登录设置: webui.json 不存在"; return 0; }

  python3 - "$webui" "$uin" <<'PYEOF'
import json, shutil, sys
from pathlib import Path

path, uin = Path(sys.argv[1]), sys.argv[2]
try:
    conf = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"[napcat] webui.json 解析失败，跳过自动登录设置: {exc}")
    sys.exit(0)

current = str(conf.get("autoLoginAccount") or "")
if current == uin:
    print(f"[napcat] 自动登录已是 {uin}，跳过")
    sys.exit(0)

backup = path.with_suffix(".json.bak")
if not backup.exists():
    shutil.copy2(path, backup)
    print(f"[napcat] 已备份 {backup.name}")

conf["autoLoginAccount"] = uin
path.write_text(json.dumps(conf, ensure_ascii=False, indent=4), encoding="utf-8")
print(f"[napcat] 自动登录账号: {current or '(空)'} -> {uin}")
PYEOF
}

case "${1:-}" in
  --status) show_status ;;
  --config)
    write_config
    set_auto_login "$(detect_uin)"
    echo; show_status
    ;;
  ""|--all)
    write_config
    set_auto_login "$(detect_uin)"
    echo; restart_qq || true
    echo; show_status
    ;;
  *) die "未知参数: $1（可用: --status / --config / 不带参数=全做）" ;;
esac
