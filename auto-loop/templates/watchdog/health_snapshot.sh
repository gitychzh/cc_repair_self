#!/usr/bin/env bash
# health_snapshot.sh — 进程/容器/端点健康快照
#
# 检查项:
#   1. CC 进程是否在运行
#   2. screen session 是否存在
#   3. 所有基础设施端点是否可达
#   4. Docker 容器数和 healthy 数
#   5. 代理日志最新写入时间
#
# stdout: JSON {claude_pid, screen_present, claude_alive, endpoints, container_count,
#              healthy_containers, infra_ok}
# exit 0: 基础设施健康
# exit 1: 部分组件不健康（仍输出 JSON，由调用方决策）

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lib/log.sh"

# 加载配置
CONFIG_ENV="${SCRIPT_DIR}/../config.env"
if [ -f "${CONFIG_ENV}" ]; then
  source "${CONFIG_ENV}"
fi

SCREEN_NAME="${SCREEN_NAME:-claude}"
COMPOSE_FILE="${COMPOSE_FILE:-/opt/your-infra-dir/docker-compose.yml}"

# ── CC 进程检测 ──
claude_pid=""
# 方式1: 匹配 --permission-mode 标志
if pgrep -f 'claude --permission-mode' >/dev/null 2>&1; then
  claude_pid=$(pgrep -f 'claude --permission-mode' | head -1)
fi
# 方式2: 匹配 node claude-code 进程名
if [ -z "${claude_pid}" ] && pgrep -f 'node.*claude-code' >/dev/null 2>&1; then
  claude_pid=$(pgrep -f 'node.*claude-code' | head -1)
fi
# 方式3: 匹配任何 claude 进程（最宽松）
if [ -z "${claude_pid}" ] && pgrep -f 'claude' >/dev/null 2>&1; then
  claude_pid=$(pgrep -f 'claude' | head -1)
fi

# ── screen 检测 ──
screen_present="no"
if screen -ls 2>/dev/null | grep -q "\.${SCREEN_NAME}"; then
  screen_present="yes"
fi

# ── 端点健康检测 ──
# 动态生成，从 HEALTH_ENDPOINTS 数组读取
# 格式: "name:url"
# 注意: 对于需要认证的端点，会自动添加 HEALTH_AUTH_HEADER

endpoint_results=()
infra_ok="yes"

for entry in "${HEALTH_ENDPOINTS[@]}"; do
  name="${entry%%:*}"
  url="${entry#*:}"
  status="down"

  # 判断是否需要认证 header（LiteLLM 端点需要）
  if echo "${url}" | grep -qE 'health/liveliness|/v1/'; then
    if curl -sf -m 5 -H "${HEALTH_AUTH_HEADER}" "${url}" >/dev/null 2>&1; then
      status="up"
    fi
  else
    if curl -sf -m 5 "${url}" >/dev/null 2>&1; then
      status="up"
    fi
  fi

  endpoint_results+=("${name}:${status}")
  if [ "${status}" != "up" ]; then
    infra_ok="no"
  fi
done

# ── Docker 容器检测 ──
container_count=0
healthy_containers=0
if command -v docker >/dev/null 2>&1; then
  # 计算期望容器中有多少在运行
  for c in "${EXPECTED_CONTAINERS[@]}"; do
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "${c}"; then
      container_count=$(( container_count + 1 ))
    fi
  done
  # 计算 healthy 数
  for c in "${EXPECTED_CONTAINERS[@]}"; do
    if docker ps --filter 'health=healthy' --format '{{.Names}}' 2>/dev/null | grep -qx "${c}"; then
      healthy_containers=$(( healthy_containers + 1 ))
    fi
  done
fi

# 容器数不足 → infra 不OK
min_containers="${MIN_CONTAINERS:-4}"
if [ "${container_count}" -lt "${min_containers}" ]; then
  infra_ok="no"
fi
if [ "${healthy_containers}" -lt "${min_containers}" ]; then
  infra_ok="no"
fi

# ── CC 存活判定 ──
# claude_alive: CC 进程在 AND screen session 在 → 才算真正活着
claude_alive="no"
if [ -n "${claude_pid}" ] && [ "${screen_present}" = "yes" ]; then
  claude_alive="yes"
fi

# ── 输出 JSON ──
endpoint_json=$(printf '"%s":"%s",' "${endpoint_results[@]}" | sed 's/,$//')

python3 - "${claude_pid}" "${screen_present}" "${claude_alive}" "${infra_ok}" "${container_count}" "${healthy_containers}" <<PY
import json, sys
args = sys.argv[1:]
out = {
    "claude_pid": args[0] or None,
    "screen_present": args[1] == "yes",
    "claude_alive": args[2] == "yes",
    "infra_ok": args[3] == "yes",
    "container_count": int(args[4]),
    "healthy_containers": int(args[5]),
}
print(json.dumps(out, ensure_ascii=False))
PY

wd_log "health_snapshot" "claude_pid=${claude_pid:-none}" "screen=${screen_present}" "claude_alive=${claude_alive}" "containers=${healthy_containers}/${container_count}" "infra_ok=${infra_ok}"

if [ "${infra_ok}" != "yes" ]; then
  exit 1
fi
exit 0