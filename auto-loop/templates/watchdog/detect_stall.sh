#!/usr/bin/env bash
# detect_stall.sh — 判定 CC session 是否卡死
#
# 原理: 检查 CC session jsonl 文件的 mtime。
#       如果最新 jsonl 超过 STALL_THRESHOLD_SEC 秒无新写入 → 判定为卡死。
#
# stdout: JSON {stalled:bool, latest_session, mtime_age_sec, last_event_type, file_count, threshold_sec}
# exit 0: 正常输出
# exit 1: session 目录不存在（不报警）
# exit 2: session 目录存在但没有任何 jsonl

set -u

# ── 配置加载 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lib/log.sh"

# 从 config.env 加载（如果存在）
CONFIG_ENV="${SCRIPT_DIR}/../config.env"
if [ -f "${CONFIG_ENV}" ]; then
  source "${CONFIG_ENV}"
fi

SESSION_DIR="${CC_SESSION_DIR:-${HOME}/.claude/projects/-home-your-user-your-project-dir}"
STALL_THRESHOLD_SEC="${STALL_THRESHOLD_SEC:-600}"

if [ ! -d "${SESSION_DIR}" ]; then
  wd_warn "session_dir_missing" "path=${SESSION_DIR}"
  echo '{"stalled":false,"reason":"session_dir_missing","file_count":0}'
  exit 1
fi

shopt -s nullglob
files=("${SESSION_DIR}"/*.jsonl)
shopt -u nullglob

if [ "${#files[@]}" -eq 0 ]; then
  wd_warn "no_session_files" "path=${SESSION_DIR}"
  echo '{"stalled":false,"reason":"no_session_files","file_count":0}'
  exit 2
fi

now=$(date +%s)
latest_file=""
latest_mtime=0
all_ages=()
for f in "${files[@]}"; do
  mt=$(stat -c %Y "$f" 2>/dev/null || echo 0)
  age=$(( now - mt ))
  all_ages+=("${age}")
  if [ "${mt}" -gt "${latest_mtime}" ]; then
    latest_mtime="${mt}"
    latest_file="$f"
  fi
done

latest_basename=$(basename "${latest_file}" .jsonl)
latest_age=$(( now - latest_mtime ))

# 找 last event type: 读最后一行
last_event_type="unknown"
if [ -s "${latest_file}" ]; then
  last_line=$(tail -n 1 "${latest_file}" 2>/dev/null || true)
  if [ -n "${last_line}" ]; then
    last_event_type=$(echo "${last_line}" | python3 -c '
import json,sys
try:
    o=json.loads(sys.stdin.read())
    print(o.get("type","unknown"))
except Exception:
    print("malformed")
')
  fi
fi

stalled=false
if [ "${latest_age}" -ge "${STALL_THRESHOLD_SEC}" ]; then
  stalled=true
fi

# 输出 JSON — 用 stdin 喂 ages，避免 shell 数组展开到 python -c 的转义陷阱
python3 - "${latest_basename}" "${latest_file}" "${latest_age}" "${last_event_type}" "${STALL_THRESHOLD_SEC}" "${#files[@]}" "${all_ages[@]}" <<'PY'
import json, sys
args = sys.argv[1:]
latest_session = args[0]
latest_path    = args[1]
latest_age     = int(args[2])
last_event     = args[3]
threshold      = int(args[4])
file_count     = int(args[5])
ages           = [int(x) for x in args[6:]]
out = {
    "stalled": latest_age >= threshold,
    "latest_session": latest_session,
    "latest_path": latest_path,
    "mtime_age_sec": latest_age,
    "last_event_type": last_event,
    "threshold_sec": threshold,
    "file_count": file_count,
    "all_session_ages": ages,
}
print(json.dumps(out, ensure_ascii=False))
PY

wd_log "detect_stall" "stalled=${stalled}" "session=${latest_basename}" "age=${latest_age}s" "last_event=${last_event_type}" "files=${#files[@]}"
exit 0