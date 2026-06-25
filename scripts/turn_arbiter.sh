#!/usr/bin/env bash
# turn_arbiter.sh — 交替优化轮次仲裁守护（R41-2 框架）
#
# cron 每 5 分钟唤起。读 configs/NEXT_ROUND.md 顶部的 TURN 标志位，
# 判断"轮到本机执行吗"。轮到才唤起完整 headless agent；否则秒退。
#
# 机制：
#   - MY_ID 由 hostname 推断（opcsname→cc1, opc2sname→cc2）
#   - TURN 行: <!-- TURN: next_actor=ccX last_actor=ccY last_commit=HASH round=RX -->
#   - next_actor==MY_ID 且无活动 turn lock → 唤起 agent
#   - agent 完成（或超时）后，翻转 next_actor 为对方并 commit push
#   - flock 防止本机多实例并发；git pull 防止两机并发抢改
#
# 用法：
#   ./turn_arbiter.sh            # 正常模式（cron 用）
#   ./turn_arbiter.sh --dry-run  # 只打印判断结果，不唤起 agent
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
NEXT_ROUND="$REPO/configs/NEXT_ROUND.md"
LOCK_FILE="$REPO/.turn.lock"
LOG_FILE="$REPO/logs/turn_arbiter.log"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.npm-global/bin/claude}"
ROUND_TIMEOUT="${ROUND_TIMEOUT:-1500}"   # 25 分钟（< 30 分钟，防跨轮重叠）

mkdir -p "$REPO/logs" 2>/dev/null
touch "$LOG_FILE"
[ -f "$LOG_FILE" ] && [ "$(wc -c <"$LOG_FILE")" -gt 50000 ] && \
    { tail -100 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"; }

ts()  { date -Iseconds; }
log() { echo "$(ts) [$1] $2" >>"$LOG_FILE"; }

# --- 识别本机身份 ---
HOST="$(hostname)"
case "$HOST" in
    opcsname)    MY_ID="cc1"; OTHER_ID="cc2" ;;
    opc2sname)   MY_ID="cc2"; OTHER_ID="cc1" ;;
    *) log "FATAL" "unknown hostname '$HOST', cannot determine MY_ID"; exit 2 ;;
esac

# --- 单实例锁（本机防并发）---
exec 9>"$LOCK_FILE" || { log "FATAL" "cannot open lock"; exit 2; }
flock -n 9 || { log "SKIP" "another arbiter instance running on $MY_ID"; exit 0; }

# --- 拉最新接力（避免基于过期的 next_actor 决策）---
cd "$REPO" || { log "FATAL" "cannot cd $REPO"; exit 2; }
git pull --rebase --autostash >>"$LOG_FILE" 2>&1 || log "WARN" "git pull failed, continue anyway"

# --- 解析 TURN 行 ---
[ -f "$NEXT_ROUND" ] || { log "FATAL" "NEXT_ROUND.md missing"; exit 2; }
TURN_LINE="$(grep -m1 '^<!-- TURN:' "$NEXT_ROUND" || true)"
if [ -z "$TURN_LINE" ]; then
    log "FATAL" "no TURN marker line in NEXT_ROUND.md (expected <!-- TURN: next_actor=... -->)"
    exit 2
fi
next_actor="$(echo "$TURN_LINE" | sed -n 's/.*next_actor=\([a-z0-9]*\).*/\1/p')"
last_commit="$(echo "$TURN_LINE" | sed -n 's/.*last_commit=\([a-z0-9]*\).*/\1/p')"
round="$(echo "$TURN_LINE" | sed -n 's/.*round=\([A-Za-z0-9-]*\).*/\1/p')"

if [ -z "$next_actor" ]; then
    log "FATAL" "cannot parse next_actor from TURN line: $TURN_LINE"
    exit 2
fi

# --- 判断轮次 ---
if [ "$next_actor" != "$MY_ID" ]; then
    log "SKIP" "not my turn (next_actor=$next_actor, I am $MY_ID)"
    exit 0
fi

# 轮到我。但先防"对方刚 push 但我还没拉到"——再确认 last_commit 在本地存在
if ! git merge-base --is-ancestor "$last_commit" HEAD 2>/dev/null && [ "$last_commit" != "HEAD" ]; then
    log "WARN" "last_commit $last_commit not in HEAD, re-pulling"
    git pull --rebase --autostash >>"$LOG_FILE" 2>&1 || true
fi

DRY_RUN="${1:-}"
if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "DRY-RUN: my turn (MY_ID=$MY_ID, next_actor=$next_actor, round=$round)"
    echo "  would invoke headless agent for $ROUND_TIMEOUT s"
    log "DRYRUN" "my turn confirmed, would invoke agent (round=$round)"
    exit 0
fi

# --- 唤起 headless agent ---
log "INFO" "my turn (round=$round, last_commit=$last_commit) — invoking agent (timeout ${ROUND_TIMEOUT}s)"
PROMPT="你是交替优化框架的 $MY_ID 执行者。按 memory/cron-optimization-loop.md 流程执行一轮：
1. SSH 进对方机器（$OTHER_ID），采对方 docker logs/metrics 数据。
2. 数据驱动地优化**对方**配置（只改对方，绝不改本机）。每轮≤1 个改动，多轮积累。
3. rebuild 对方受影响容器，curl 验证 200。
4. 更新 configs/NEXT_ROUND.md（含翻转 TURN 行 next_actor=$OTHER_ID）+ DEPLOY_STATUS + memory。
5. git pull --rebase --autostash && git add -A && git commit && git push。
6. 铁律：只改对方不改自己；拿不准查 CLAUDE.md/docs；网络问题用各自 mihomo；稳定优先。
务必在 25 分钟内完成一轮。无数据支撑就只翻 next_actor 并记录'本轮无修改'。"

timeout --preserve-status -s KILL "$ROUND_TIMEOUT" \
    "$CLAUDE_BIN" -p --dangerously-skip-permissions --add-dir "$REPO" "$PROMPT" \
    >>"$LOG_FILE" 2>&1
RC=$?

# --- 翻转 next_actor（无论 agent 成功/失败/超时，都翻，避免卡死同一方）---
# agent 自己应在 Step 4 翻转；这里兜底，只在它没翻时补翻。
NEW_TURN_LINE="<!-- TURN: next_actor=$OTHER_ID last_actor=$MY_ID last_commit=$(git rev-parse --short HEAD 2>/dev/null) round=$round -->"
CURRENT_NEXT="$(grep -m1 '^<!-- TURN:' "$NEXT_ROUND" | sed -n 's/.*next_actor=\([a-z0-9]*\).*/\1/p' || true)"
if [ "$CURRENT_NEXT" = "$MY_ID" ]; then
    # agent 没翻，兜底翻
    sed -i "s|^<!-- TURN:.*-->|$NEW_TURN_LINE|" "$NEXT_ROUND"
    git add "$NEXT_ROUND"
    git -c user.name=claude -c user.email=claude@local commit -m "$round (arbiter兜底翻转 next_actor→$OTHER_ID, rc=$RC)" >>"$LOG_FILE" 2>&1
    git push >>"$LOG_FILE" 2>&1 || true
    log "INFO" "arbiter fallback-flipped next_actor→$OTHER_ID (agent rc=$RC, did not flip itself)"
else
    log "INFO" "agent already flipped next_actor (rc=$RC)"
fi

log "INFO" "round end rc=$RC"
exit 0
