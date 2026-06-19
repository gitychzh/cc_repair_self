# 远程 opc_uname CC 基础设施升级计划

## 目标
将远程 opc_uname (100.109.153.83:222) 的 CC 链路升级到与本地 opc2_uname 同等架构：
- 添加 :40000 dispatcher（按 model 字段路由 opus→40005 / sonnet→40001）
- 添加 :40005 cc-proxy (primary, opus 默认走这里)
- 将 :40001 改为 fallback (sonnet 走这里)
- 切换所有 glm5.2 → glm5.1（ModelScope 下架）
- dsv4p 仍瘫痪，40003 暂保留但不做改动
- LiteLLM 41001 config 全量切换到 glm5.1

## 当前 vs 目标架构对比

### 远程当前（旧架构 R29）
```
CC → :40001 cc-proxy (单一入口, 无 dispatcher)
         → ms_uni41001 (glm5.2 v×k) → ModelScope [全部 400!]
```
- 没有 dispatcher、没有 40005
- proxy 代码是合并版（codex.py 还在）
- 没有 throttle (MIN_OUTBOUND_INTERVAL_S)
- 没有 rr_counter.json 持久化
- VARIANT_IDS = glm5.2（全停）
- ROLE_DEFAULT_UPSTREAM = {"cc":"glm5.2"}

### 远程目标（R31.7 + glm5.1）
```
CC → :40000 dispatcher (按 model 字段路由)
      ├── opus/未知 → :40005 cc-proxy (primary) → ms_uni41001 (glm5.1 v×k)
      └── sonnet    → :40001 cc-proxy (fallback) → ms_uni41001 (glm5.1 v×k)
:40002 codex-proxy (保留)
:40003 openai-proxy (保留, dsv4p 仍停但容器正常)
```
- dispatcher 按请求 body 的 model 字段路由
- 40005=primary (opus 默认), 40001=fallback (sonnet)
- proxy 代码沿用现有合并版，只需改 config.py 的映射
- 新增 throttle (MIN_OUTBOUND_INTERVAL_S=2.0)
- 新增 rr_counter.json 持久化
- VARIANT_IDS 改为 GLM-5.1 系列
- ROLE_DEFAULT_UPSTREAM 改为 {"cc":"glm5.1"}
- MODEL_MAP 中 claude-* → glm5.1

## 需要修改的文件（在远程 /opt/cc-infra）

### 1. LiteLLM config.yaml (/opt/cc-infra/litellm-glm51/config.yaml)
- 70 个 glm5.2v×k deployment → 改为 glm5.1v×k（变体 ID GLM-5.1 系列）
- model_name 从 `glm5.2v1k1` → `glm5.1v1k1`
- model 从 `openai/ZHIPUAI/GLM-5.2` → `openai/ZHIPUAI/GLM-5.1`
- 70 个 dsv4pv×k deployment 暂不改（虽然停了，但保留结构方便恢复）
- router_settings 中 glm5.2 model group → glm5.1

### 2. proxy gateway/config.py (/opt/cc-infra/proxy/gateway/config.py)
- GLM51_VARIANT_IDS: 全部改为 GLM-5.1 系列（10 变体）
- ROLE_DEFAULT_UPSTREAM: "glm5.2" → "glm5.1"
- MODEL_UPSTREAMS key: "glm5.2" → "glm5.1"
- VARIANT_IDS key: "glm5.2" → "glm5.1"
- MODEL_MAP: claude-* → "glm5.1"（而非 glm5.2）
- 新增 MIN_OUTBOUND_INTERVAL_S 和 throttle_outbound()
- 新增 rr_counter.json 持久化逻辑
- NUM_VARIANTS key: "glm5.2" → "glm5.1"

### 3. 新增 dispatcher (/opt/cc-infra/proxy/dispatcher/)
- 新建目录 proxy/dispatcher/
- Dockerfile（与本地相同）
- gateway_main.py（路由规则 opus→40005, sonnet→40001）

### 4. docker-compose.yml (/opt/cc-infra/docker-compose.yml)
- 新增 auth_to_api_40000 (dispatcher)
- 新增 auth_to_api_40005 (cc-proxy primary)
- auth_to_api_40001 改为 fallback（注释说明）
- 40005 构建仍用 ./proxy（远程没有 cc-proxy 拆分目录，保留合并版）
- 40005 LOG_DIR 改为 /app/logs（独立日志）
- 新增 MIN_OUTBOUND_INTERVAL_S=2.0 env var

### 5. CC settings.json (~/.claude/settings.json)
- ANTHROPIC_BASE_URL: http://127.0.0.1:40001 → http://127.0.0.1:40000
- model: "glm5.2_cc" → "glm5.1_cc"
- contextWindow / autoCompactWindow 保持 170000/155000

### 6. .bashrc / .profile env
- 确保 ANTHROPIC_BASE_URL=http://127.0.0.1:40000（CC startup check 用 shell env）

## 执行步骤

### Step 1: 备份
```bash
cd /opt/cc-infra && bash scripts/backup_config.sh
```

### Step 2: 创建 dispatcher 目录和文件
```bash
mkdir -p /opt/cc-infra/proxy/dispatcher
# 写入 Dockerfile 和 gateway_main.py
```

### Step 3: 修改 LiteLLM config.yaml
- 用脚本批量替换 glm5.2 → glm5.1 (model_name + model)
- 用脚本批量替换 ZHIPUAI/GLM-5.2 → ZHIPUAI/GLM-5.1 (等变体)

### Step 4: 修改 proxy gateway/config.py
- 批量替换 glm5.2 → glm5.1
- 替换 VARIANT_IDS 内容为 GLM-5.1 系列
- 添加 throttle_outbound 和 rr_counter 持久化逻辑（从本地复制）

### Step 5: 修改 docker-compose.yml
- 添加 40000 dispatcher 和 40005 cc-proxy primary
- 添加 MIN_OUTBOUND_INTERVAL_S=2.0 等新 env var
- 40001 改为 fallback 标注

### Step 6: 修改 CC settings + shell env
- ANTHROPIC_BASE_URL → :40000
- model → glm5.1_cc

### Step 7: rebuild + 启动
```bash
cd /opt/cc-infra && docker compose up -d --build --force-recreate
```

### Step 8: 测试
- curl :40000/v1/messages (opus → 40005 → glm5.1)
- curl :40000/v1/messages (sonnet → 40001 → glm5.1)
- curl :40005/v1/messages (直接测试 primary)
- curl :40001/v1/messages (直接测试 fallback)
- curl :40000/health (dispatcher 健康检查)

### Step 9: 验证 CC 能工作
- 重启 CC 进程（或让用户手动重启）

## 关键注意事项
- 远程 proxy 代码是合并版（codex.py 还在），不像本地已拆分到 cc-proxy/。我们**不做拆分**（改动太大），只是改 config.py 里的映射和新增 throttle/persist。
- dispatcher 的 DISPATCH_PRIMARY 和 DISPATCH_FALLBACK 用容器网络名（auth_to_api_40005:40005 / auth_to_api_40001:40001），不是 127.0.0.1。
- 40005 和 40001 共享 ms_uni41001 + 相同 glm5.1 变体 → quota 共享（与本地一致）。
- rr_counter.json 在 40005 和 40001 的日志目录里各自独立（需要分卷挂载）。
- dsv4p 全停但容器结构保留，不删除 deployment（方便恢复）。
