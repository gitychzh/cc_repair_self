# cron-optimization-loop.md — 交替优化循环详细流程（R41-2+）

> **R41-2 重写**：从旧的"优化本机"（R35）改为"交替优化对方"。
> 执行体是 `scripts/turn_arbiter.sh` 唤起的 headless agent（`claude -p`）。
> 仲裁依据 `configs/NEXT_ROUND.md` 顶部的 TURN 标志位。

## 核心铁律（不可违反）

1. **只改对方，绝不改本机**——自己改自己易崩溃，程序就停了。
2. **每轮 ≤ 1 个改动**，多轮积累。稳定优先于激进。
3. **所有改动必须有日志数据或文档支撑**——拿不准立即查 CLAUDE.md/docs。
4. **轮次只认 TURN 标志位**——`next_actor != me` 就 exit 0，不偷跑。
5. **完成后必须翻转 `next_actor` 为对方并 push**——否则循环卡死。

## 身份与访问

| 我（执行者） | 本机 hostname | 对方 | SSH 到对方 |
|---|---|---|---|
| cc1 | opcsname | cc2 | `ssh -p 222 opc2_uname@100.109.57.26` |
| cc2 | opc2sname | cc1 | `ssh -p 222 opc_uname@100.109.153.83` |

仓库路径（两机相同）：`~/cc_ps/cc_repair_self`；部署目录：`/opt/cc-infra`。

## 每轮流程

### Step 0: 仲裁门（turn_arbiter.sh 已做，agent 启动即代表通过）
- `turn_arbiter.sh` 已确认 `next_actor == MY_ID` 并唤起本 agent。
- 本机 flock + git pull 已完成。进入 Step 1。

### Step 1: 前置检查（在**对方**机器上）
```bash
# SSH 进对方
# 检查对方 proxy 健康（不健康就跳过本轮，交给对方自愈）
ssh $OTHER "curl -sf http://127.0.0.1:40005/health && curl -sf http://127.0.0.1:40001/health"
```
任一不健康 → 只在 NEXT_ROUND 记录"对方 unhealthy，本轮跳过"，翻 next_actor，exit。

### Step 2: 采集**对方**数据（不是本机）
```bash
ssh $OTHER "docker logs --since 30m auth_to_api_40005 2>/dev/null | tail -200"
ssh $OTHER "docker logs --since 30m auth_to_api_40001 2>/dev/null | tail -200"
ssh $OTHER "docker logs --since 24h auth_to_api_40005 2>&1 | grep -cE 'ABORT|NO-FALLBACK'"
# Hermes 流量低时也看一眼
ssh $OTHER "docker exec cc_postgres psql -U litellm -d hermes_logs -c 'SELECT count(*) FROM hm_requests'"
# 对方 quota
ssh $OTHER 'bash ~/cc_ps/cc_repair_self/scripts/check_quota_balance.sh 2>/dev/null || echo no-quota-script'
```

### Step 3: 分析 + 决策（数据驱动）
读数据找**一个**可优化点：
- 有明确错误模式（如某错误重复）→ 修根因，有日志证据。
- 有参数偏离安全边界 → 调参，有范围依据（见 CLAUDE.md "可调整参数"表）。
- 无数据支撑 → **不改**，只翻 next_actor + 记录"本轮无修改"。

**质疑自己**：改动是否可逆？是否碰了不可变更约束（variant IDs / rpm / 端口 / 容器名）？是否一次改太多？

### Step 4: 执行（改**对方**）
1. `ssh $OTHER "cp /opt/cc-infra/docker-compose.yml /opt/cc-infra/docker-compose.yml.bak.R$ROUND-\$(date +%Y%m%d-%H%M)"`（备份）
2. 改对方 `/opt/cc-infra/` 下配置（最小改动）。
3. `ssh $OTHER "cd /opt/cc-infra && docker compose up -d --build --force-recreate <container>"`
4. 同步到对方仓库：`ssh $OTHER "cp /opt/cc-infra/docker-compose.yml ~/cc_ps/cc_repair_self/configs/"`

### Step 5: 验证
```bash
ssh $OTHER "curl -s -o /dev/null -w 'HTTP=%{http_code} time=%{time_total}s\n' --max-time 60 \
  -X POST http://127.0.0.1:40005/v1/messages \
  -H 'x-api-key: sk-litellm-local' -H 'anthropic-version: 2023-06-01' \
  -d '{\"model\":\"glm5.1\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}],\"max_tokens\":10}'"
```
非 200 或 >30s → 回滚（恢复备份 + recreate），记入 NEXT_ROUND，翻 next_actor，exit。

### Step 6: 记录 + 翻转 + push
1. 更新 `configs/NEXT_ROUND.md`：
   - 顶部 TURN 行翻转：`next_actor=<OTHER> last_actor=<ME> last_commit=<新hash> round=R<next>`
   - 追加本轮成果（问题/根因/修复/验证，附日志数据）
2. 更新 `configs/DEPLOY_STATUS.md`（简短一行）。
3. 更新本机 memory（仅关键非显然结论，按 memory 规范）。
4. **在本机仓库** commit push（不在对方机器 push，避免弄乱对方 git）：
   ```bash
   git pull --rebase --autostash
   git add -A
   git -c user.name=claude -c user.email=claude@local commit -m "R<next>: <改动摘要>"
   git push
   ```
   若 push 被拒（对方 agent 同时 push），`git pull --rebase` 重试，不强行 `-f`。

## 评判标准（每轮对照）
更少报错、更快请求、超低延迟、稳定优先。改动前后的日志数据对比要写进 NEXT_ROUND。

## 边界与禁忌
- ❌ 绝不修改本机 cc-infra 配置（`/opt/cc-infra` 本机侧）。
- ❌ 绝不碰 CLAUDE.md "不可变更约束"表里的项（variant IDs / rpm=1 / 端口 / 容器名 / PROXY_ROLE）。
- ❌ 不做截断/压缩、不转 429→529、proxy 不做 retry（见 CLAUDE.md 关键原则）。
- ❌ 不迁大路径（如 NV integrate→NVCF pexec）——代码级改动单独一轮做。
- ✅ 网络问题可用各自 mihomo（对方代理：cc2 用 7894-7899）。
- ✅ 拿不准查文档，不猜。
