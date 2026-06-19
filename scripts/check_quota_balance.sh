#!/usr/bin/env bash
# check_quota_balance.sh — 查询 7 个 MS_KEY 的剩余额度，验证 2D round-robin 分配是否均衡。
#
# 原理：ModelScope 响应头 Modelscope-Ratelimit-Model-Requests-Remaining
#       = 每个 (key, model) 的当日剩余请求数（每日上限 200/id）。
#       若 7 个 key 剩余额度接近 → 证明 proxy 的 variant×key 绝对顺序循环正确；
#       若差异明显 → 循环逻辑有问题（某 key 被偏用），需深挖 upstream.py/config.py。
#
# 用法：bash scripts/check_quota_balance.sh
# 退出码：0=均衡（spread ≤ 5），1=不均衡（spread > 5，需排查）
#
# 注意：每个 key 调一次会消耗 1 次额度（共 7 次）。3s 间隔避免 burst throttle。

set -u
set -o pipefail

COMPOSE_FILE="${CC_COMPOSE_FILE:-/opt/cc-infra/docker-compose.yml}"
MODEL="${MS_QUOTA_MODEL:-ZHIPUAI/GLM-5.2}"
BASE="https://api-inference.modelscope.cn/v1/chat/completions"
INTERVAL=3   # 请求间隔，避免 RPM burst throttle

if [ ! -r "$COMPOSE_FILE" ]; then
  echo "[ERR] compose file 不可读: $COMPOSE_FILE" >&2
  exit 2
fi

# 从 docker-compose.yml 抽取 MS_KEY1..7
mapfile -t KEYS < <(grep -oE 'MS_KEY[0-9]+: ms-[a-f0-9-]+' "$COMPOSE_FILE" \
                     | awk '{print $2}' | sort -u)

if [ "${#KEYS[@]}" -ne 7 ]; then
  echo "[ERR] 从 $COMPOSE_FILE 只解析到 ${#KEYS[@]} 个 MS_KEY（期望 7）" >&2
  exit 2
fi

echo "=== quota balance check @ $(date -Iseconds) ==="
echo "model=$MODEL  (limit=200/id/day)"

declare -a REM
for i in "${!KEYS[@]}"; do
  kn=$((i+1))
  hdr=$(curl -s -m 30 -D - -o /dev/null \
        -H "Authorization: Bearer ${KEYS[$i]}" -H "Content-Type: application/json" \
        -X POST "$BASE" \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"q\"}],\"max_tokens\":1}" 2>/dev/null || true)
  rem=$(echo "$hdr" | grep -i '^Modelscope-Ratelimit-Model-Requests-Remaining:' \
          | tr -d '\r' | awk '{print $2}')
  if [ -z "${rem:-}" ]; then
    # 429 时不返回 remaining header；尝试从 ratelimit-requests（账号级）兜底
    rem=$(echo "$hdr" | grep -i '^Modelscope-Ratelimit-Requests-Remaining:' \
            | tr -d '\r' | awk '{print $2}')
    rem="${rem:-ERR}"
  fi
  REM+=("$rem")
  printf "  key%s: remaining=%s\n" "$kn" "$rem"
  sleep "$INTERVAL"
done

# 计算极差（忽略 ERR）
nums=()
for r in "${REM[@]}"; do [ "$r" != "ERR" ] && nums+=("$r"); done
if [ "${#nums[@]}" -eq 0 ]; then
  echo "[ERR] 无可用 remaining 值（可能全部被 burst throttle）" >&2
  exit 2
fi
max=$nums; min=$nums
for n in "${nums[@]}"; do
  (( n > max )) && max=$n
  (( n < min )) && min=$n
done
spread=$(( max - min ))

echo "  max=$max  min=$min  spread=$spread"

if [ "$spread" -le 5 ]; then
  echo "[OK] 均衡（spread ≤ 5）→ 2D round-robin 绝对顺序正确"
  exit 0
else
  echo "[WARN] 不均衡（spread=$spread > 5）→ 某些 key 被偏用，需深挖 upstream.py / config.py / rr_counter.json"
  exit 1
fi
