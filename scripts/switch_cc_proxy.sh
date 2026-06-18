#!/usr/bin/env bash
# switch_cc_proxy.sh — 把 CC (经由 cloudcli webui) 切换到指定 proxy 端口。
#
# 背景 (R31.6):
#   浏览器里的 CC 会话由 cloudcli-webui 托管,webui 进程是所有 claude 子进程
#   的父进程。claude 继承 webui 的 ANTHROPIC_BASE_URL env (在 systemd unit 的
#   Environment= 行里), 而非读 settings.json/.bashrc。所以"切换 CC 走哪个 proxy"
#   = 改 webui unit 的 env + 重启 webui + 杀掉所有残留 claude 子进程。
#
#   关键: 这个脚本杀掉 webui = 杀掉当前 CC 会话 = 杀掉我自己。所以脚本主体
#   只负责"改 env + 停 systemd 侧", 然后派发一个完全脱离会话的 watchdog
#   (setsid + & + /tmp 日志) 去完成 杀进程→重启→验证。watchdog 不依赖任何终端
#   或 CC 会话存活。
#
# 用法:
#   switch_cc_proxy.sh            # 默认切到 40005 (opus, R31.2 起 default)
#   switch_cc_proxy.sh 40005      # 显式 40005
#   switch_cc_proxy.sh 40001      # 切回 40001 (sonnet fallback)
#
# 不自动回退 (用户选择手动处理)。watchdog 把结果写到 /tmp/switch_cc_watchdog.log。
# 失败时人工 SSH 进机器: systemctl --user start cloudcli-webui.service

set -euo pipefail

TARGET_PORT="${1:-40005}"

case "$TARGET_PORT" in
  40001|40005) ;;
  *) echo "ERROR: TARGET_PORT 必须是 40001 或 40005, got '$TARGET_PORT'" >&2; exit 1 ;;
esac

UNIT="$HOME/.config/systemd/user/cloudcli-webui.service"
NEW_BASE_URL="http://127.0.0.1:${TARGET_PORT}"
WATCHDOG_LOG="/tmp/switch_cc_watchdog.log"

if [ ! -f "$UNIT" ]; then
  echo "ERROR: unit 不存在: $UNIT" >&2; exit 1
fi

echo "=== 切换 CC proxy → $TARGET_PORT ==="

# 1. 先停 systemd 侧 (停止当前 EADDRINUSE crash-loop)
echo "[1/4] 停止 cloudcli-webui.service (systemd 侧)"
systemctl --user stop cloudcli-webui.service 2>/dev/null || true
systemctl --user reset-failed cloudcli-webui.service 2>/dev/null || true

# 2. 备份 unit + 改 ANTHROPIC_BASE_URL 行
cp "$UNIT" "${UNIT}.bak.$(date +%s)"
echo "[2/4] 备份 unit → ${UNIT}.bak.*"
# 用 python3 改 ini-like 行 (Environment=ANTHROPIC_BASE_URL=...), 精确替换该 key
python3 - "$UNIT" "$NEW_BASE_URL" <<'PY'
import re, sys
path, new = sys.argv[1], sys.argv[2]
with open(path) as f:
    lines = f.readlines()
pat = re.compile(r'^(Environment=ANTHROPIC_BASE_URL=).*$')
hit = False
for i, ln in enumerate(lines):
    if pat.match(ln):
        lines[i] = pat.sub(lambda m: m.group(1) + new + '\n', ln)
        hit = True
        break
if not hit:
    sys.exit("ERROR: unit 内未找到 Environment=ANTHROPIC_BASE_URL= 行")
with open(path, 'w') as f:
    f.writelines(lines)
print(f"    unit env 改为 ANTHROPIC_BASE_URL={new}")
PY

systemctl --user daemon-reload
echo "[3/4] daemon-reload 完成"

# 3. 派发脱离会话的 watchdog (setsid + nohup-style, 日志到 /tmp)
#    watchdog 负责: 杀全部 claude + 孤儿 webui → 等 3001 释放 → systemd 拉新 webui → 验证
echo "[4/4] 派发 watchdog → $WATCHDOG_LOG (脚本将随后退出, CC 会话会被杀)"
setsid bash -c '
  WATCHDOG_LOG="'"$WATCHDOG_LOG"'"
  TARGET_PORT="'"$TARGET_PORT"'"
  log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$WATCHDOG_LOG"; }
  exec >>"$WATCHDOG_LOG" 2>&1
  echo "=========================================="
  log "watchdog 启动, 目标 proxy=$TARGET_PORT"

  log "等待主脚本退出 (2s)..."
  sleep 2

  log "深度检索并杀掉所有 claude 子进程..."
  pkill -9 -f "/home/.*\.npm-global/bin/claude" 2>/dev/null && log "  claude 子进程已杀" || log "  无 claude 子进程残留"

  log "杀掉孤儿 webui (占 3001 的旧进程)..."
  pkill -9 -f "cc_webui/dist-server/server/index.js" 2>/dev/null && log "  webui 已杀" || log "  无 webui 残留"

  log "等待 3001 端口释放 (3s)..."
  sleep 3

  log "启动 cloudcli-webui.service (systemd 拉起, 读新 env)..."
  systemctl --user reset-failed cloudcli-webui.service 2>/dev/null || true
  systemctl --user start cloudcli-webui.service

  log "等待 webui + DB schema 起来 (10s)..."
  sleep 10

  log "验证 webui (3001)..."
  if curl -sf --max-time 5 http://127.0.0.1:3001/ >/dev/null 2>&1; then
    log "  WEBUI OK"
  else
    log "  WEBUI FAIL — 检查 journalctl --user -u cloudcli-webui.service"
  fi

  log "验证目标 proxy $TARGET_PORT /health..."
  if curl -sf --max-time 5 "http://127.0.0.1:${TARGET_PORT}/health" >/dev/null 2>&1; then
    log "  PROXY $TARGET_PORT OK"
  else
    log "  PROXY $TARGET_PORT FAIL"
  fi

  log "验证新 webui 实际 env (确认注入 40005)..."
  NEWPID=$(systemctl --user show -p MainPID --value cloudcli-webui.service 2>/dev/null)
  if [ -n "$NEWPID" ] && [ "$NEWPID" != "0" ]; then
    ENV_URL=$(tr "\0" "\n" < /proc/$NEWPID/environ 2>/dev/null | grep "^ANTHROPIC_BASE_URL=" || echo "(读取失败)")
    log "  webui PID=$NEWPID $ENV_URL"
  else
    log "  webui MainPID 未获取 (可能仍在启动)"
  fi

  log "watchdog 完成。浏览器刷新/重开会话即可, 新 CC 走 $TARGET_PORT。"
  echo "=========================================="
' </dev/null >/dev/null 2>&1 &

echo ""
echo "watchdog 已派发 (PID $!)。脚本 1 秒后退出。"
echo "→ CC 会话将断开, 几秒后新 webui 起来, 刷新浏览器重开会话。"
echo "→ 进度看: tail -f $WATCHDOG_LOG"
sleep 1
exit 0
