# Round R35.7 — 2026-06-21

## R35.7: Stale Container Deployment Fix + Code Bug Fixes

### 发现：所有 4 个 proxy 容器运行旧版代码

R35.5/R35.6/R35.6+ 声称在源码中修复了多个问题，但 **容器从未 rebuild**，导致：

1. **40003 passthrough-proxy**: `is_quota_exhaustion()` 仍用旧版关键词匹配 → 140 次 `429_quota_exhausted`（应为 `429_rate_limit`）→ `all_non_quota_429=False` → **retry-after:180 发给 OpenClaw** → OpenClaw 卡住（R35.6 根因仍在生效！）
2. **40002 codex-proxy**: 同样用旧版关键词匹配 → retry-after:30 → Codex CLI 同样的不对称 bug
3. **40001/40005 cc-proxy**: `is_quota_exhaustion()` 已更新为 `return False`，但 `MODEL_UPSTREAMS` 仍包含 `dsv4p` gateway（R35.5 删除）
4. **所有容器**: Ghost-ABORT/Ghost-Stream/Ghost-Collect 修复未生效，metrics 仍显示 `status=200` 遮掩失败

**根因**：代码 commit 到 git 但未执行 `sync_config.sh` + `docker compose up -d --build --force-recreate`。源码变了 ≠ 容器变了。

### 修复 Action 1: Rebuild all containers (两次)

1. 第一次 rebuild：`sync_config.sh` → rebuild 40001/40002/40003/40005 — 修复 is_quota_exhaustion + dsv4p removal + Ghost-ABORT
2. 清理 40003 stale dsv4p rr_counter: `{"dsv4p": 6, "glm5.1": 301}` → `{"glm5.1": 301}`
3. 第二次 rebuild：代码 bug 修复后 rebuild 全部 5 个容器（含 dispatcher）

### 修复 Action 2: Code Bug Fixes (5 个 bug)

#### BUG 11 (高): PROXY_TIMEOUT NameError in stream.py — 所有 3 个 proxy
- **问题**: stream.py line 300/437 引用 `PROXY_TIMEOUT` 但 import 中未包含 → stream timeout 时 NameError crash
- **影响**: 如果 stream 阶段发生 timeout，proxy 会 crash（无响应返回给 CC → CC ConnectionRefused 卡死）
- **修复**: 在 cc-proxy/passthrough-proxy/codex-proxy 的 stream.py import 行添加 `PROXY_TIMEOUT`
- **数据支撑**: 代码审查发现，NameError 在 Python 中是确定性 crash

#### BUG 7 (中): operator precedence — thinking_budget + input overflow 误分类
- **问题**: `convert_error()` 中 `"range of input length" or ("invalidparameter"... ) and "thinking_budget" not in msg_lower` → `thinking_budget` guard 只覆盖 `invalidparameter` 分支，不覆盖 `range of input length` 分支
- **影响**: 如果 ModelScope 返回同时含 `range of input length` + `thinking_budget` 的错误，会被分类为 `invalid_request_error`（CC 停止）而非 `api_error`（CC 重试）
- **修复**: 重新括号化：`(("range of input length"... ) or ("invalidparameter"... )) and "thinking_budget" not in msg_lower`
- **数据支撑**: `is_input_overflow()` 函数已有正确 guard（覆盖两分支），但 `convert_error()`/`format_openai_error_upstream()` 不一致

#### BUG 1/13 (高-预防性): KeyError on key_idx for NV entries
- **问题**: passthrough/codex error_mapping.py + handlers.py 用 `a['key_idx']` 直接访问，NV entries 有 `nv_key_idx` 而非 `key_idx` → KeyError crash
- **影响**: 当前 NV_NUM_KEYS=0 不会触发，但未来 NV 重启时立即 crash
- **修复**: 改为 `a.get('key_idx', a.get('nv_key_idx', 0))` 模式（cc-proxy 已有此模式）
- **数据支撑**: 预防性修复，基于代码逻辑审查

#### BUG 2/3/4 (中-预防性): upstream.py classification checks 缺少 NV error types
- **问题**: `all_429`/`all_non_quota_429`/`has_conn_err` 不包含 NV error types → NV 重启后分类错误
- **修复**: 添加 `429_nv_rate_limit`/`NVConnectionRefusedError`/`NVConnectionError` 到所有 3 个 proxy 的 upstream.py
- **数据支撑**: 预防性修复，保持 MS/NV 分类逻辑一致

#### Dispatcher BUG 1 (高): _send_err missing close_connection
- **问题**: dispatcher `_send_err()` 不设 `close_connection=True` → HTTP/1.1 keep-alive → client 复用 dead connection → 第二请求也失败
- **修复**: 在 `_send_err()` 中添加 `self.close_connection = True` + `Connection: close` header
- **数据支撑**: HTTP/1.1 规范：keep-alive 是默认行为，错误后应主动关闭连接

### 部署验证 (17:40 CST)
- 40005 health: ✅ `{"status":"ok","proxy_role":"cc","gateways":{"glm5.1":...},"port":40005}`（dsv4p gateway 已移除）
- 40001 health: ✅ 同上
- 40003 health: ✅ `{"status":"ok","proxy_role":"passthrough","gateways":{"glm5.1":...},"port":40003}`（dsv4p gateway 已移除）
- 40002 health: ✅ 同上
- 40000 health: ✅ dispatcher 正常
- curl 40005: ✅ 200
- curl 40003: ✅ 200
- PROXY_TIMEOUT import: ✅ 所有 3 proxy 已添加
- dispatcher close_connection: ✅ 已添加

### 关键教训（长期知识）

**代码变更 ≠ 部署变更**。每次代码 commit 后必须：
1. `bash scripts/sync_config.sh`（同步源码到 /opt/cc-infra）
2. `cd /opt/cc-infra && docker compose up -d --build --force-recreate <containers>`
3. curl smoke test 验证

否则 git 上的"修复"只是纸面修复，容器仍运行旧版代码。

## 参数现状 (40001=40005 mirror, 40003 unified)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False(all ports, now actually deployed) | PROXY_TIMEOUT import in stream.py(fixed) | dispatcher close_connection(fixed)

## 下轮待办
- 监控 40003 新 metrics 格式（error_type 应全部为 `429_rate_limit`，不再有 `429_quota_exhausted`）
- 监控 ABORT metrics 是否正确记录（status=429/502 代替 status=0/200）
- 监控 Ghost-Stream/Ghost-Collect 修复效果（stream error → status=502 代替 200）
- 监控 PROXY_TIMEOUT import 是否消除 stream.py NameError（如有 stream timeout 事件）
- throttle 1.5→1.0 测试（需有人值守）
