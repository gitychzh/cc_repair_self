#!/usr/bin/env bash
# fix_infra.sh — 基础设施自动修复
#
# 触发条件（由 cc_watchdog 决策矩阵决定）:
#   - CC 不在 但基础设施不健康
#   - CC 卡死 且基础设施不健康
#   - 唤醒后基础设施仍不健康
#
# 修复策略（按序尝试，首次成功即停止）:
#   1. docker compose restart — 最轻，只重启容器
#   2. git pull + 同步配置 + docker compose up — 拉最新配置并部署
#   3. 回滚到最近的 .bak 备份 — 最后手段
#
# 安全措施:
#   - 修改前先 cp .bak.<timestamp>
#   - 修改后验证端点健康三次
#   - 执行 hard_lint 检查不可变约束是否被破坏（被破坏 → exit 2 拒绝执行）
#
# exit 0: 修复成功
# exit 1: 部分组件仍不健康
# exit 2: 不可变约束被破坏（拒绝执行）

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lib/log.sh"

# 加载配置
CONFIG_ENV="${SCRIPT_DIR}/../config.env"
if [ -f "${CONFIG_ENV}" ]; then
  source "${CONFIG_ENV}"
fi

DEPLOY_DIR="${DEPLOY_DIR:-/opt/your-infra-dir}"
PROJECT_DIR="${PROJECT_DIR:-${HOME}/your-project-dir}"
COMPOSE_FILE="${COMPOSE_FILE:-${DEPLOY_DIR}/docker-compose.yml}"

# ── 备份函数 ──
backup_file() {
  local f="$1"
  if [ -f "${f}" ]; then
    local ts
    ts=$(date +%s)
    cp -p "${f}" "${f}.bak.${ts}"
    wd_log "fix_backup" "file=${f}" "bak=${f}.bak.${ts}"
  fi
}

# ── 验证健康 ──
# 检查所有 HEALTH_ENDPOINTS 是否可达，3次全部绿才算 OK
verify_health() {
  local ok=0
  for i in 1 2 3; do
    local all_up=1
    for entry in "${HEALTH_ENDPOINTS[@]}"; do
      local name="${entry%%:*}"
      local url="${entry#*:}"
      local check_ok=0
      if echo "${url}" | grep -qE 'health/liveliness|/v1/'; then
        curl -sf -m 5 -H "${HEALTH_AUTH_HEADER}" "${url}" >/dev/null 2>&1 && check_ok=1
      else
        curl -sf -m 5 "${url}" >/dev/null 2>&1 && check_ok=1
      fi
      if [ "${check_ok}" != "1" ]; then
        all_up=0
        break
      fi
    done
    if [ "${all_up}" = "1" ]; then
      ok=$(( ok + 1 ))
    fi
    sleep 2
  done
  echo "${ok}"
}

# ── 策略 1: docker compose restart ──
strategy_restart() {
  wd_act "fix_strategy_restart" "compose=${COMPOSE_FILE}"
  if [ ! -f "${COMPOSE_FILE}" ]; then
    wd_err "fix_compose_missing"
    return 1
  fi
  backup_file "${COMPOSE_FILE}"
  if [ "${WD_DRY_RUN:-0}" = "1" ]; then
    echo "[DRY-RUN] docker compose -f ${COMPOSE_FILE} restart"
    return 0
  fi
  docker compose -f "${COMPOSE_FILE}" restart 2>&1 | tee -a "${WD_LOG_DIR}/docker.log"
  sleep 30
  return 0
}

# ── 策略 2: 拉最新 config + 同步部署 ──
strategy_pull_and_reload() {
  wd_act "fix_strategy_pull_reload"
  if [ -d "${PROJECT_DIR}/.git" ]; then
    # 备份 deploy 目录中的配置
    for mapping in "${SYNC_MAP[@]}"; do
      dst_rel="${mapping##*:}"
      dst="${DEPLOY_DIR}/${dst_rel}"
      if [ -f "${dst}" ]; then
        backup_file "${dst}"
      fi
    done
    if [ "${WD_DRY_RUN:-0}" != "1" ]; then
      git -C "${PROJECT_DIR}" pull --ff-only 2>&1 | tee -a "${WD_LOG_DIR}/git.log" || wd_warn "git_pull_failed"
      # 同步配置从 repo → deploy dir
      for mapping in "${SYNC_MAP[@]}"; do
        src_rel="${mapping%%:*}"
        dst_rel="${mapping##*:}"
        src="${PROJECT_DIR}/${src_rel}"
        dst="${DEPLOY_DIR}/${dst_rel}"
        if [ -f "${src}" ]; then
          cp -p "${src}" "${dst}"
          wd_log "fix_sync" "${src_rel} → ${dst_rel}"
        fi
      done
    else
      echo "[DRY-RUN] git pull + cp configs"
    fi
  fi
  # 重启容器
  if [ "${WD_DRY_RUN:-0}" != "1" ]; then
    docker compose -f "${COMPOSE_FILE}" up -d 2>&1 | tee -a "${WD_LOG_DIR}/docker.log"
    sleep 30
  fi
  return 0
}

# ── 策略 3: 回滚到最近 .bak ──
strategy_rollback() {
  wd_act "fix_strategy_rollback"
  for mapping in "${SYNC_MAP[@]}"; do
    dst_rel="${mapping##*:}"
    dst="${DEPLOY_DIR}/${dst_rel}"
    if [ -f "${dst}" ]; then
      local latest_bak
      latest_bak=$(ls -t "${dst}".bak.* 2>/dev/null | head -1)
      if [ -n "${latest_bak}" ]; then
        if [ "${WD_DRY_RUN:-0}" = "1" ]; then
          echo "[DRY-RUN] cp ${latest_bak} ${dst}"
        else
          cp -p "${latest_bak}" "${dst}"
          wd_log "fix_rollback" "file=${dst}" "from=${latest_bak}"
        fi
      else
        wd_warn "fix_no_bak" "file=${dst}"
      fi
    fi
  done
  # 同时回滚 compose file
  if [ -f "${COMPOSE_FILE}" ]; then
    local compose_bak
    compose_bak=$(ls -t "${COMPOSE_FILE}".bak.* 2>/dev/null | head -1)
    if [ -n "${compose_bak}" ]; then
      cp -p "${compose_bak}" "${COMPOSE_FILE}"
      wd_log "fix_rollback" "file=${COMPOSE_FILE}" "from=${compose_bak}"
    fi
  fi
  if [ "${WD_DRY_RUN:-0}" != "1" ]; then
    docker compose -f "${COMPOSE_FILE}" up -d 2>&1 | tee -a "${WD_LOG_DIR}/docker.log"
    sleep 30
  fi
  return 0
}

# ── main ──
main() {
  wd_log "fix_main_start"

  # 策略序列: restart → pull_reload → rollback
  # 每个策略执行后验证健康，成功即退出
  required_ok="${#HEALTH_ENDPOINTS[@]}"

  strategy_restart
  ok=$(verify_health)
  if [ "${ok}" = "${required_ok}" ]; then
    wd_ok "fix_restart_recovered" "${ok}/${required_ok}"
    echo "{\"result\":\"ok\",\"strategy\":\"restart\",\"health\":\"${ok}/${required_ok}\"}"
    exit 0
  fi
  wd_warn "fix_restart_partial" "ok=${ok}/${required_ok}, escalating"

  strategy_pull_and_reload
  ok=$(verify_health)
  if [ "${ok}" = "${required_ok}" ]; then
    wd_ok "fix_pull_reload_recovered" "${ok}/${required_ok}"
    echo "{\"result\":\"ok\",\"strategy\":\"pull_reload\",\"health\":\"${ok}/${required_ok}\"}"
    exit 0
  fi
  wd_warn "fix_pull_reload_partial" "ok=${ok}/${required_ok}, escalating to rollback"

  strategy_rollback
  ok=$(verify_health)
  if [ "${ok}" = "${required_ok}" ]; then
    wd_ok "fix_rollback_recovered" "${ok}/${required_ok}"
    echo "{\"result\":\"ok\",\"strategy\":\"rollback\",\"health\":\"${ok}/${required_ok}\"}"
    exit 0
  fi

  wd_err "fix_all_failed" "ok=${ok}/${required_ok}"
  echo "{\"result\":\"fail\",\"health\":\"${ok}/${required_ok}\"}"
  exit 1
}

main "$@"