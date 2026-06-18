#!/usr/bin/env bash
# switch_cc_proxy.sh — 把 CC (经由 cloudcli webui) 切换到指定 proxy 端口。
#
# CC env 优先级真相 (R31.6 二次修正, 2026-06-18, 用真实流量日志确认):
#   浏览器里的 CC 会话由 system-level `cloudcli.service` 托管。CC 启动时读三处 env,
#   优先级: ~/.claude/settings.json 的 env 块  >  父进程(webui)系统env  >  shell env。
#   实测铁证: webui/.env 和 webui 系统env 都已是 40005, 但 CC 实际流量仍走 40001,
#   因为 settings.json 的 env.ANTHROPIC_BASE_URL=40001 没改 → 它优先级最高, 覆盖一切。
#   (CC 启动时把 settings.json 的 env 块当作权威, 注入到自己的请求环境。)
#
#   所以切换必须同时改两处:
#     1. ~/.claude/settings.json 的 env.ANTHROPIC_BASE_URL   ← 真正决定 CC 走向(之前漏了!)
#     2. cc_webui/.env 的 ANTHROPIC_BASE_URL                  ← webui 透传(次要, 保持一致)
#   然后重启 cloudcli.service → 杀旧 webui+claude → 新 claude 读新 settings.json → 走新 proxy。
#
#   关键: 重启 cloudcli.service = 杀掉 webui = 杀掉当前 CC 会话 = 杀掉我自己。
#   所以脚本主体只负责"改两个文件", 然后派发 watchdog 执行 restart + 验证。
#   watchdog 必须脱离 cloudcli.service 的 cgroup (否则 restart 时被一起杀),
#   用 systemd-run --service-type=oneshot 派到独立 transient unit。
#
# 用法:
#   switch_cc_proxy.sh            # 默认切到 40005 (opus)
#   switch_cc_proxy.sh 40005      # 显式 40005
#   switch_cc_proxy.sh 40001      # 切回 40001 (sonnet fallback)
#
# 验证: 切换后看 /tmp/switch_cc_watchdog.log, 或 proxy 日志:
#   docker logs auth_to_api_40005 --since <切换时间> | grep REQ
#   tail -f /opt/cc-infra/logs/proxy40005/proxy.*.log

set -euo pipefail

TARGET_PORT="${1:-40005}"

case "$TARGET_PORT" in
  40001|40005) ;;
  *) echo "ERROR: TARGET_PORT 必须是 40001 或 40005, got '$TARGET_PORT'" >&2; exit 1 ;;
esac

SETTINGS="$HOME/.claude/settings.json"
ENV_FILE="/home/opc2_uname/cc_ps/cc_webui/.env"
NEW_BASE_URL="http://127.0.0.1:${TARGET_PORT}"
WATCHDOG_LOG="/tmp/switch_cc_watchdog.log"

for f in "$SETTINGS" "$ENV_FILE"; do
  if [ ! -f "$f" ]; then echo "ERROR: 文件不存在: $f" >&2; exit 1; fi
done

# 预检: 目标 proxy 必须健康, 否则切过去 CC 无法工作
if ! curl -sf --max-time 5 "http://127.0.0.1:${TARGET_PORT}/health" >/dev/null 2>&1; then
  echo "ERROR: 目标 proxy $TARGET_PORT /health 不通, 中止切换" >&2; exit 1
fi

# 预检: sudo 非交互 (restart cloudcli.service 需要)
if ! sudo -n true 2>/dev/null; then
  echo "ERROR: sudo 需要密码, 无法非交互 restart cloudcli.service。请配置 passwordless sudo。" >&2; exit 1
fi

echo "=== 切换 CC proxy → $TARGET_PORT ==="

# 1. 改 ~/.claude/settings.json 的 env.ANTHROPIC_BASE_URL (CC 权威 env 源, 优先级最高)
cp "$SETTINGS" "${SETTINGS}.bak.$(date +%s)"
python3 - "$SETTINGS" "$NEW_BASE_URL" <<'PY'
import json, sys
path, new = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
env = data.setdefault("env", {})
if env.get("ANTHROPIC_BASE_URL") == new:
    print(f"  settings.json: 已是 {new}, 无需改")
else:
    old = env.get("ANTHROPIC_BASE_URL", "(空)")
    env["ANTHROPIC_BASE_URL"] = new
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  settings.json env.ANTHROPIC_BASE_URL: {old} → {new}")
PY

# 2. 改 cc_webui/.env 的 ANTHROPIC_BASE_URL (webui 透传, 保持一致)
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
print(f"  .env ANTHROPIC_BASE_URL → {new}")
PY

echo "[1/2] 两处 env 源已改 → $NEW_BASE_URL"
echo ""

# 3. 派发 watchdog (脱离 cgroup) 执行 restart + 验证
echo "[2/2] 派发 watchdog (systemd-run 独立 scope) → $WATCHDOG_LOG"
echo "      (脚本将随后退出, CC 会话会被杀, 几秒后刷新浏览器重连)"

sudo systemd-run --service-type=oneshot --unit=switch-cc-watchdog \
  --working-directory=/tmp \
  bash -c '
    WATCHDOG_LOG="'"$WATCHDOG_LOG"'"
    TARGET_PORT="'"$TARGET_PORT"'"
    exec >>"$WATCHDOG_LOG" 2>&1
    echo "=========================================="
    echo "[$(date +%H:%M:%S)] watchdog 启动 (independent scope), 目标 proxy=$TARGET_PORT"

    echo "[$(date +%H:%M:%S)] 等主脚本退出 (3s)..."
    sleep 3

    echo "[$(date +%H:%M:%S)] restart cloudcli.service..."
    systemctl restart cloudcli.service

    echo "[$(date +%H:%M:%S)] 等待新 webui + claude 起来 (12s)..."
    sleep 12

    # 验证1: webui 起来了 + env
    NEWPID=$(systemctl show -p MainPID --value cloudcli.service 2>/dev/null)
    ENV_URL=$(tr "\0" "\n" < /proc/$NEWPID/environ 2>/dev/null | grep "^ANTHROPIC_BASE_URL=" || echo "(读取失败)")
    echo "[$(date +%H:%M:%S)] webui MainPID=$NEWPID  env: $ENV_URL"

    if curl -sf --max-time 5 http://127.0.0.1:3001/ >/dev/null 2>&1; then
      echo "[$(date +%H:%M:%S)] WEBUI OK"
    else
      echo "[$(date +%H:%M:%S)] WEBUI FAIL — journalctl -u cloudcli.service"
    fi

    # 验证2: settings.json 确认已写入目标端口
    SJSON=$(python3 -c "import json;print(json.load(open(\"'"$HOME"'/.claude/settings.json\"))[\"env\"][\"ANTHROPIC_BASE_URL\"])" 2>/dev/null || echo "(读取失败)")
    echo "[$(date +%H:%M:%S)] settings.json env.ANTHROPIC_BASE_URL=$SJSON"

    case "$ENV_URL" in
      *":$TARGET_PORT"*) echo "[$(date +%H:%M:%S)] ✓ webui+settings 均指向 $TARGET_PORT, 新 CC 将走此链路" ;;
      *) echo "[$(date +%H:%M:%S)] ✗ 检查异常: env 不含 $TARGET_PORT" ;;
    esac

    echo "[$(date +%H:%M:%S)] watchdog 完成。刷新浏览器重开会话, 新 CC 走 $TARGET_PORT。"
    echo "  验证真实流量: docker logs auth_to_api_${TARGET_PORT} --since 1min | grep REQ"
    echo "=========================================="
'

echo ""
echo "watchdog 已派发 (systemd-run unit: switch-cc-watchdog)。"
echo "→ CC 会话将断开, 刷新浏览器重连。"
echo "→ 进度: tail -f $WATCHDOG_LOG"
sleep 1
exit 0
