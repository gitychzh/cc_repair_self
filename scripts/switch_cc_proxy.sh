#!/usr/bin/env bash
# switch_cc_proxy.sh — 把 CC (经由 cloudcli webui) 切换到指定 proxy 端口。
#
# 进程链真相 (R31.6 修正, 2026-06-18):
#   浏览器里的 CC 会话由 system-level `cloudcli.service` 托管, 它 ExecStart
#   /usr/bin/node cc_webui/dist-server/server/index.js 并 EnvironmentFile=.../cc_webui/.env。
#   webui 进程 fork 出 claude 子进程, claude-sdk.js:133 `sdkOptions.env={...process.env}`
#   把 webui 的 env (含 .env 里的 ANTHROPIC_BASE_URL) 原样透传给 claude。
#   所以 claude 的 ANTHROPIC_BASE_URL 唯一来源 = cc_webui/.env。
#   (注: 还有另一个 user-level cloudcli-webui.service 是没在用的影子, 别改它。)
#
#   切换 = 改 .env 的 ANTHROPIC_BASE_URL → sudo systemctl restart cloudcli.service
#   → 杀旧webui+claude → 新webui读新.env → 新claude走新proxy。
#
#   关键: 重启 cloudcli.service = 杀掉 webui = 杀掉当前 CC 会话 = 杀掉我自己。
#   所以脚本主体只负责"改.env", 然后派发一个完全脱离会话的 watchdog (setsid + /tmp日志)
#   去执行 restart + 验证。watchdog 不依赖任何终端或 CC 会话存活。
#
# 用法:
#   switch_cc_proxy.sh            # 默认切到 40005 (opus, R31.2 起 default)
#   switch_cc_proxy.sh 40005      # 显式 40005
#   switch_cc_proxy.sh 40001      # 切回 40001 (sonnet fallback)
#
# 不自动回退 (用户选择手动处理)。watchdog 把结果写到 /tmp/switch_cc_watchdog.log。
# 失败时人工 SSH 进机器: sudo systemctl restart cloudcli.service

set -euo pipefail

TARGET_PORT="${1:-40005}"

case "$TARGET_PORT" in
  40001|40005) ;;
  *) echo "ERROR: TARGET_PORT 必须是 40001 或 40005, got '$TARGET_PORT'" >&2; exit 1 ;;
esac

ENV_FILE="/home/opc2_uname/cc_ps/cc_webui/.env"
NEW_BASE_URL="http://127.0.0.1:${TARGET_PORT}"
WATCHDOG_LOG="/tmp/switch_cc_watchdog.log"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: .env 不存在: $ENV_FILE" >&2; exit 1
fi

# 预检: 目标 proxy 必须健康, 否则切过去 CC 无法工作
if ! curl -sf --max-time 5 "http://127.0.0.1:${TARGET_PORT}/health" >/dev/null 2>&1; then
  echo "ERROR: 目标 proxy $TARGET_PORT /health 不通, 中止切换" >&2; exit 1
fi

echo "=== 切换 CC proxy → $TARGET_PORT ==="

# 1. 备份 .env + 改 ANTHROPIC_BASE_URL 行 (system-level cloudcli.service 的 EnvironmentFile)
cp "$ENV_FILE" "${ENV_FILE}.bak.$(date +%s)"
python3 - "$ENV_FILE" "$NEW_BASE_URL" <<'PY'
import re, sys
path, new = sys.argv[1], sys.argv[2]
with open(path) as f:
    lines = f.readlines()
pat = re.compile(r'^(ANTHROPIC_BASE_URL=).*$')
hit = False
for i, ln in enumerate(lines):
    if pat.match(ln.strip()):
        lines[i] = pat.sub(lambda m: m.group(1) + new + '\n', ln)
        hit = True
        break
if not hit:
    sys.exit("ERROR: .env 内未找到 ANTHROPIC_BASE_URL= 行")
with open(path, 'w') as f:
    f.writelines(lines)
print(f"[1/3] .env 已改: ANTHROPIC_BASE_URL={new}")
PY

# 2. sudo 非交互预检
if ! sudo -n true 2>/dev/null; then
  echo "ERROR: sudo 需要密码, 无法非交互 restart cloudcli.service。请配置 passwordless sudo。" >&2; exit 1
fi

# 3. 派发 watchdog 执行 restart + 验证。
#    关键教训: watchdog 不能在 cloudcli.service 的 cgroup 内, 否则 restart cloudcli.service
#    会把它和 webui/claude 一起杀 (它即使 setsid 脱离了终端会话, 仍在同一 cgroup)。
#    解法: 用 systemd-run --scope 派到独立 transient unit (switch-cc-watchdog.scope),
#    完全脱离 cloudcli.service 的 cgroup, restart 杀不到它, 才能活到 restart 之后做验证。
echo "[2/3] 派发 watchdog (systemd-run 独立 scope) → $WATCHDOG_LOG (脚本将随后退出, CC 会话会被杀)"

sudo systemd-run --service-type=oneshot --unit=switch-cc-watchdog \
  --working-directory=/tmp \
  bash -c '
    WATCHDOG_LOG="'"$WATCHDOG_LOG"'"
    TARGET_PORT="'"$TARGET_PORT"'"
    exec >>"$WATCHDOG_LOG" 2>&1
    echo "=========================================="
    echo "[$(date +%H:%M:%S)] watchdog 启动 (independent scope), 目标 proxy=$TARGET_PORT"

    # 改 .env 已由主脚本完成, 这里只需 restart service
    echo "[$(date +%H:%M:%S)] 等主脚本退出 (2s)..."
    sleep 2

    echo "[$(date +%H:%M:%S)] restart cloudcli.service (杀旧webui+claude, 拉新webui读新.env)..."
    systemctl restart cloudcli.service

    echo "[$(date +%H:%M:%S)] 等待新 webui 起来 (10s)..."
    sleep 10

    NEWPID=$(systemctl show -p MainPID --value cloudcli.service 2>/dev/null)
    echo "[$(date +%H:%M:%S)] 新 webui MainPID=$NEWPID"

    # 验证 webui env (确认注入了目标端口)
    ENV_URL=$(tr "\0" "\n" < /proc/$NEWPID/environ 2>/dev/null | grep "^ANTHROPIC_BASE_URL=" || echo "(读取失败)")
    echo "[$(date +%H:%M:%S)] 新 webui env: $ENV_URL"

    # 验证 webui 端口
    if curl -sf --max-time 5 http://127.0.0.1:3001/ >/dev/null 2>&1; then
      echo "[$(date +%H:%M:%S)] WEBUI OK"
    else
      echo "[$(date +%H:%M:%S)] WEBUI FAIL — journalctl -u cloudcli.service 查原因"
    fi

    case "$ENV_URL" in
      *":$TARGET_PORT"*) echo "[$(date +%H:%M:%S)] ✓ 切换成功, 新 webui 走 $TARGET_PORT" ;;
      *) echo "[$(date +%H:%M:%S)] ✗ 切换未生效 (env 不含 $TARGET_PORT)" ;;
    esac

    echo "[$(date +%H:%M:%S)] watchdog 完成。刷新浏览器重开会话, 新 CC 走 $TARGET_PORT。"
    echo "=========================================="
'

echo "[3/3] watchdog 已派发 (systemd-run unit: switch-cc-watchdog)。脚本 1 秒后退出。"
echo "→ CC 会话将断开, 几秒后新 webui 起来, 刷新浏览器重开会话。"
echo "→ 进度: tail -f $WATCHDOG_LOG"
sleep 1
exit 0
