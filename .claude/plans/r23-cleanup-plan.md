# R23: 清理远程主机冗余容器、配置和日志

## 目标
删除容器 42001 (dsv4p_uni42001)、41003 (glm5.1_test41003) 及所有相关配置、日志、脚本引用，只保留：
- 40001/40002 (proxy gateway)
- 41001 (ms_uni41001 统一 LiteLLM)
- cc_postgres (数据库)

## 需要修改/删除的内容

### 1. docker-compose.yml — 删除两个容器定义
- 删除 `glm5.1_test41003` 整个 service (行109-164)
- 删除 `dsv4p_uni42001` 整个 service (行166-221)
- 删除 postgres `POSTGRES_MULTIPLE_DATABASES` 中的 `litellm_dsv4p,litellm_glm51_test`（只保留 `litellm_glm51`）
- 更新注释：移除41003/42001相关的注释行

### 2. 删除配置文件（git rm）
- `configs/litellm-glm51-test/config.yaml` — 41003的配置，839行
- `configs/litellm-dsv4p/config.yaml` — 42001的配置，923行

### 3. 删除空日志目录
- `configs/logs/litellm-dsv4p/` — 空目录（只有git未track的空目录）
- `configs/logs/litellm-glm51-test/` — 同上
  注意：这些目录不在git中，但docker-compose.yml有volume映射到它们，删除容器定义后自然不再需要

### 4. postgres init-db.sh — 无需修改
- `POSTGRES_MULTIPLE_DATABASES` 由 docker-compose.yml 的 env var 控制，init-db.sh 只是遍历它
- 只需在 docker-compose.yml 中修改 env var，init-db.sh 不变

### 5. proxy.py / gateway — 无需修改代码逻辑
- proxy.py 和 gateway/app.py, config.py 中没有硬编码41003/42001路由
- gateway/app.py注释提到"42001"需更新注释
- proxy.py 已经只路由到 ms_uni41001，无需改代码

### 6. scripts/deploy.sh — 清理引用
- 删除 `dsv4p_uni42001` 和 `glm5.1_test41003` 的 restart 分支 (行30-35)
- 删除 41003 直接测试 (行71-76)

### 7. scripts/health_check.sh — 清理引用
- 删除 42001 health check (行31-34)
- 删除 `LITELLM_DSV4P_HEALTHY` 变量及相关逻辑
- 更新 CONTAINERS_HEALTHY 分母 (4→3 或动态)

### 8. scripts/backup_config.sh — 清理引用
- 删除 `/opt/cc-infra/litellm-dsv4p/config.yaml` (行20)

### 9. scripts/rollback.sh — 清理引用
- 删除 litellm-dsv4p config restore (行49-52)
- 删除 dsv4p_uni42001 restart (行82-85)

### 10. scripts/sync_config.sh — 清理引用
- 删除 `configs/litellm-dsv4p/config.yaml` (行21)
- 删除 `configs/litellm-glm51-test/config.yaml` (行22)

### 11. CLAUDE.md — 更新架构描述
- 删除架构图中的41003和42001行
- 更新 Docker container names 列表
- 更新 port assignments
- 更新项目文件结构（删除 litellm-glm51-test 和 litellm-dsv4p 行）
- 删除 restart commands 中 41003/42001 相关部分

### 12. DEPLOY_STATUS.md — 更新部署状态
- 删除架构图中的41003/42001行
- 删除 Containers 表中的 41003/42001 行
- 删除 R20 变体缩减部分（41003已不存在）
- 删除 R21 中"41003/42001 retained for fallback"的描述
- 删除关于 litellm-dsv4p/litellm-glm51-test 空日志目录的描述
- 更新 opc2_uname 验证中的容器列表
- 更新所有41003/42001引用

### 13. configs/proxy/gateway/app.py — 更新注释
- 删除注释中"42001 LiteLLM"的描述（行7）

### 14. README.md — 更新
- 更新架构图（删除42001）
- 更新端口表
- 更新历史记录中41002→42001的引用

### 15. 删除旧分析日志（可选清理）
- `logs/` 目录下的 round_1~7 分析 JSON — 这些是历史记录，保留
- `configs/logs/` 下的 proxy metrics — 保留（历史数据）

### 16. 删除备份文件
- `backups/` 目录下所有备份 — 每个备份都包含 `litellm-dsv4p/config.yaml`
- 但这些是 git 未track 的，本地备份。全部删除

### 17. 删除 PLAN.md 和 plan.md
- `PLAN.md` — R20的旧计划，已过时，全部关于41003
- `plan.md` — R21旧计划，已过时
- `.claude/plan.md` — R21旧计划

### 18. 清理 configs/logs 下的空目录和旧文件
- `configs/logs/litellm-dsv4p/` — 删除空目录
- `configs/logs/litellm-glm51/` — 也是空目录，删除
- `configs/logs/round_5_analysis.json` — git tracked 的旧分析，保留

## 执行顺序

1. git rm 删除配置文件
2. 修改 docker-compose.yml
3. 修改所有 scripts
4. 修改 CLAUDE.md
5. 修改 DEPLOY_STATUS.md
6. 修改 README.md
7. 修改 gateway/app.py 注释
8. 删除 PLAN.md, plan.md, .claude/plan.md
9. 删除 backups 目录内容
10. 删除空日志目录 (configs/logs/litellm-dsv4p/, litellm-glm51/)
11. 更新 .gitignore 排除 litellm-dsv4p 和 litellm-glm51-test 目录
12. git add + commit

## 注意事项
- **不要**删除 postgres init-db.sh 本身（它只是遍历 env var 中的数据库列表）
- **不要**修改 proxy.py 和 gateway 的路由逻辑（它们已经只指向 ms_uni41001）
- **不要**删除 configs/litellm-glm51/config.yaml（这是唯一活跃的 LiteLLM 配置）
- 41003/42001 的 PostgreSQL 数据库（litellm_glm51_test, litellm_dsv4p）在远程机器上需要手动 DROP，但这不在本仓库范围