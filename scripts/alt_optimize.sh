#!/usr/bin/env bash
# alt_optimize.sh — 交替优化守护（R43+ 新框架）
#
# 设计（用户 2026-06-25 重新定义）：
#   - 身份以 IP 为准: cc1=100.109.153.83, cc2=100.109.57.26
#   - 两机各跑此脚本（cron 每 5 分钟）
#   - 查 GitHub origin/main 最新 commit 的 author
#   - 若最新 commit **不是本机用户**提交的 → 对方刚做完一轮 → 该我接手
#   - 唤起新 claude -p headless session 跑一轮（每轮新 session，不续旧）
#   - 只对**远程**优化（我改 cc2；cc2 改我），不改本机
#   - 每轮收尾: 提炼归纳去重 + 更新 DEPLOY_STATUS/NEXT_ROUND + commit push
#
# 交替判据: git author == 本机 whoami → 是我的提交 → 不动; 否则 → 接手
# (git user.name 全局配置: cc1=opc_uname, cc2=opc2_uname)
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
LOG_FILE="$REPO/logs/alt_optimize.log"
LOCK_FILE="$REPO/.alt_optimize.lock"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.npm-global/bin/claude}"
ROUND_TIMEOUT="${ROUND_TIMEOUT:-1500}"   # 25 分钟

mkdir -p "$REPO/logs" 2>/dev/null
touch "$LOG_FILE"
[ -f "$LOG_FILE" ] && [ "$(wc -c <"$LOG_FILE")" -gt 50000 ] && \
    { tail -100 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"; }

ts()  { date -Iseconds; }
log() { echo "$(ts) [$1] $2" >>"$LOG_FILE"; }

# --- 本机身份（以 whoami 为准，与 git user.name 对齐）---
ME="$(whoami)"
case "$ME" in
    opc_uname)   MY_ID="cc1"; OTHER_ID="cc2"; OTHER_SSH="ssh -p 222 opc2_uname@100.109.57.26" ;;
    opc2_uname)  MY_ID="cc2"; OTHER_ID="cc1"; OTHER_SSH="ssh -p 222 opc_uname@100.109.153.83" ;;
    *) log "FATAL" "unknown user '$ME'"; exit 2 ;;
esac

# --- 单实例锁（本机防并发，含旧常驻 agent 若也走此脚本）---
exec 9>"$LOCK_FILE" || { log "FATAL" "cannot open lock"; exit 2; }
flock -n 9 || { log "SKIP" "another instance running on $MY_ID"; exit 0; }

cd "$REPO" || { log "FATAL" "cannot cd $REPO"; exit 2; }

# --- 拉远程最新（只 fetch 不 merge，先看 author）---
git fetch origin main >>"$LOG_FILE" 2>&1 || { log "WARN" "git fetch failed, skip"; exit 0; }

# --- 交替判据: origin/main 最新 commit 的 author ---
LATEST_AUTHOR="$(git log -1 --format='%an' origin/main 2>/dev/null)"
LATEST_HASH="$(git log -1 --format='%h' origin/main 2>/dev/null)"
LATEST_SUBJECT="$(git log -1 --format='%s' origin/main 2>/dev/null)"

if [ -z "$LATEST_AUTHOR" ]; then
    log "FATAL" "cannot read latest commit author from origin/main"
    exit 2
fi

DRY_RUN="${1:-}"
if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "DRY-RUN: MY_ID=$MY_ID ME=$ME"
    echo "  latest origin/main: $LATEST_HASH by '$LATEST_AUTHOR' — $LATEST_SUBJECT"
    if [ "$LATEST_AUTHOR" = "$ME" ]; then
        echo "  → my own commit, NOT my turn (skip)"
    else
        echo "  → other's commit, MY TURN (would invoke agent)"
    fi
    exit 0
fi

# --- 判定 ---
if [ "$LATEST_AUTHOR" = "$ME" ]; then
    # 最新是我自己提交的 → 我刚做完一轮，等对方
    log "SKIP" "latest commit ($LATEST_HASH) is mine ($ME), wait for $OTHER_ID"
    exit 0
fi

# 最新是对方提交的 → 该我接手。先 pull 对方的工作到本地
git pull --rebase --autostash >>"$LOG_FILE" 2>&1 || { log "WARN" "git pull failed, continue anyway" }

# --- 确定下一轮版本号 ---
# 从最新 commit subject 提取 R<number>，+1
LAST_ROUND="$(echo "$LATEST_SUBJECT" | grep -oE 'R[0-9]+(-[0-9]+)?' | head -1 || true)"
if [ -n "$LAST_ROUND" ]; then
    BASE_ROUND="$(echo "$LAST_ROUND" | grep -oE '^[R]?[0-9]+' | tr -d 'R')"
    # 子轮号: R43-1 → 43, 子 1
    SUB_ROUND="$(echo "$LAST_ROUND" | sed -n 's/^[R]*[0-9]*-\([0-9]\+\)$/\1/p')"
    if [ -n "$SUB_ROUND" ]; then
        NEXT_ROUND="R${BASE_ROUND}-$((SUB_ROUND + 1))"
    else
        NEXT_ROUND="R$((BASE_ROUND + 1))"
    fi
else
    NEXT_ROUND="R43"
fi

log "INFO" "MY TURN — latest=$LATEST_HASH by $LATEST_AUTHOR, invoking $NEXT_ROUND (timeout ${ROUND_TIMEOUT}s)"

# --- 唤起新 headless session（每轮新 session，不续旧）---
# 注意: agent 只负责分析+改文件+更新文档, **不做 git commit/push**。
# commit/push 由本脚本在 agent 退出后统一执行, 强制用 $ME 作者,
# 保证交替判据(author==whoami)永远可靠, 不依赖 agent 守纪律。
PROMPT="你是交替优化框架的 $MY_ID（用户 $ME）。这是 $NEXT_ROUND 轮。按 memory/cron-optimization-loop.md 流程执行：

身份: 你是 $MY_ID，远程对方是 $OTHER_ID。访问对方: $OTHER_SSH
铁律: 只改对方，绝不改本机。每轮 ≤1 个改动，多轮积累，稳定优先。
评判: 更少 429/报错、更高成功率、更低延迟、更快请求。

流程:
1. SSH 进对方，采对方 docker logs (40001/40002/40005/ms_uni41001/hm40006) 与 metrics。
2. 数据驱动找**一个**可优化点（有日志证据）。拿不准查 CLAUDE.md/docs。
3. 改对方 /opt/cc-infra 配置（最小改动，备份原文件），rebuild 对方容器，curl 验证 200。
4. 收尾（必须）: 提炼+归纳+去重本轮结论；更新 configs/DEPLOY_STATUS.md 和 configs/NEXT_ROUND.md。
5. **不要 git commit/push** —— 本脚本会在你退出后统一提交。你只改文件即可。

务必 25 分钟内完成。无数据支撑就只更新 NEXT_ROUND 记'本轮无修改'，让脚本提交。"

timeout --preserve-status -s KILL "$ROUND_TIMEOUT" \
    "$CLAUDE_BIN" -p --dangerously-skip-permissions --add-dir "$REPO" "$PROMPT" \
    >>"$LOG_FILE" 2>&1
RC=$?

# --- 脚本统一 commit + push（强制用 $ME 作者，保证判据可靠）---
cd "$REPO" || exit 0
git pull --rebase --autostash >>"$LOG_FILE" 2>&1 || true
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    git add -A >>"$LOG_FILE" 2>&1
    # 用本机真实用户名提交（交替判据的生命线）
    git -c user.name="$ME" -c user.email="${ME}@$(hostname -s).local" \
        commit -m "$NEXT_ROUND: $MY_ID→$OTHER_ID 优化轮 (agent rc=$RC)" >>"$LOG_FILE" 2>&1
    # push 带重试（对方可能同时 push）
    for i in 1 2 3; do
        if git push >>"$LOG_FILE" 2>&1; then
            log "INFO" "$NEXT_ROUND committed+pushed by $ME (attempt $i)"
            break
        fi
        log "WARN" "push failed attempt $i, rebase retry"
        git pull --rebase --autostash >>"$LOG_FILE" 2>&1 || true
    done
else
    log "INFO" "$NEXT_ROUND no changes to commit (agent rc=$RC)"
fi

log "INFO" "$NEXT_ROUND end rc=$RC"
exit 0
