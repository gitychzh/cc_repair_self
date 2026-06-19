# R31.6 计划：拆分 40002/40003 + CC 切到 40005 的正式重启脚本

## 背景事实（已核实）

### webui/cc 进程链真相
- 浏览器现在连的是 **PID 798**（孤儿 webui，6/16 启动，`/usr/bin/node cc_webui/dist-server/server/index.js`，占 3001 端口）。它是 3 个 claude 子进程（457011/1060041/1670307）的父进程。
- systemd `cloudcli-webui.service` 因 **EADDRINUSE 3001**（被孤儿 798 占）一直 crash-loop（restart counter 33508）。
- claude 子进程继承 webui 的 env → 798 的 `ANTHROPIC_BASE_URL=40001` 注入到所有子进程 → 这才是 CC 走 40001 的根因。改 settings.json/.bashrc 对运行进程无效。
- `Linger=yes` ✓（断开后用户进程仍存活，安全）。

### env 来源
- service 的 `ANTHROPIC_BASE_URL` 写在 unit 文件的 `Environment=` 行（line 17），非 EnvironmentFile。
- 改这一行 → `daemon-reload` → `restart` service = 新 webui 走新 env。

---

## Part A：拆分 40002(codex)/40003(passthrough) 为独立子目录

**策略：复制隔离不删码**（零 bug 风险，物理隔离即达成核心诉求"改一个不再全炸"）。

### A1. 新建 `configs/proxy/codex-proxy/`
复制共享树 `configs/proxy/gateway/` 全部 10 个 .py（含 codex.py 961 行，不改一行）+ `gateway_main.py` + ���建 `Dockerfile`（照 cc-proxy 模板，注释改为 codex-role）+ `.gitignore`。

### A2. 新建 `configs/proxy/passthrough-proxy/`
同样复制全部 .py + gateway_main.py + Dockerfile + .gitignore。

### A3. 改 `configs/docker-compose.yml`
- `auth_to_api_40002` 的 `build.context: ./proxy` → `./proxy/codex-proxy`
- `auth_to_api_40003` 的 `build.context: ./proxy` → `./proxy/passthrough-proxy`
- 加 R31.6 注释说明物理隔离。
- `auth_to_api_40001` **暂不动**（40001 是 sonnet fallback，仍用共享 `./proxy`；下轮再拆）。→ 这样 40002/40003 彻底与 40001 解耦。

### A4. 同步 + 重建
```bash
# sync 到 /opt/cc-infra
cp -r configs/proxy/codex-proxy configs/proxy/passthrough-proxy /opt/cc-infra/proxy/
cp configs/docker-compose.yml /opt/cc-infra/
cd /opt/cc-infra && docker compose build auth_to_api_40002 auth_to_api_40003
docker compose up -d --force-recreate auth_to_api_40002 auth_to_api_40003
```

### A5. 验证（不碰 40001/40005）
- 40002 `/v1/responses` → 200（codex 实测）
- 40003 `/v1/chat/completions` → 200（OpenAI 实测）
- 两个容器 health OK
- 40001/40005 未动（不重建）

### A6. 不删共享 `./proxy/gateway/`
40001 仍在用，保留。后续 40001 也拆完后共享树再清理。

---

## Part B：CC 切到 40005 的正式重启脚本

**核心难点：脚本会杀掉 PID 798 = 杀掉 webui = 杀掉我自己。** 必须用脱离会话的 watchdog 完成杀→等→重启→验证，脚本自身不能在被杀后做事。

### B1. 新建 `scripts/switch_cc_proxy.sh`
参数：`40005`（默认）或 `40001`（手动切回）。

脚本做两件事：
1. **改 webui service env**（同进程内完成，不依赖被杀后的状态）：
   - `systemctl --user stop cloudcli-webui.service`（先停 systemd 侧，停止 crash-loop）
   - 用 sed 改 `~/.config/systemd/user/cloudcli-webui.service` 的 `ANTHROPIC_BASE_URL=` 行为目标值
   - `systemctl --user daemon-reload`

2. **派发一个完全脱离当前会话的 watchdog**（`setsid ... &`，重定向到日志文件，不依赖任何终端）：
   ```bash
   setsid bash -c '
     sleep 2                      # 等主脚本退出、cc进程随webui死亡
     pkill -9 -f "claude"         # 深度检索：杀全部 claude 子进程(含残留)
     pkill -9 -f "cc_webui/dist-server"  # 杀孤儿 webui(798)
     sleep 3                      # 等 3001 释放
     systemctl --user reset-failed cloudcli-webui.service
     systemctl --user start cloudcli-webui.service   # systemd 拉新 webui(读新env)
     sleep 8                      # 等 webui + DB schema 起来
     # 验证:curl webui 3001 + curl 目标proxy /health
     curl -sf http://127.0.0.1:3001/ >/dev/null && echo OK_WEBUI || echo FAIL_WEBUI
     curl -sf http://127.0.0.1:40005/health && echo OK_PROXY || echo FAIL_PROXY
   ' </dev/null >/tmp/switch_cc_watchdog.log 2>&1 &
   ```
   watchdog 派发后，主脚本立即 `exit 0`（在被杀之前干净退出）。
   系统随后：webui 死 → claude 死（我下线）→ systemd 因 `Restart=always` 重启 webui → 新 webui 读 env=40005 → 新 claude 走 40005。

3. **多 CC 进程处理**：`pkill -9 -f "claude"` 会匹配全部 `/home/opc2_uname/.npm-global/bin/claude` 进程（当前 3 个 + 任何后续），无需枚举 PID。这是用户明确要求的"深度检索全部杀掉"。

### B2. 为什么不自动回退
用户选"不回退，手动处理"。脚本只验证+记录日志到 `/tmp/switch_cc_watchdog.log`，失败时由你人工查日志决定改回 40001。脚本可重复执行：`switch_cc_proxy.sh 40001` 即切回 fallback。

### B3. 执行顺序（本轮）
1. 先完成 Part A（拆分），验证 40002/40003 正常。
2. 写好 `switch_cc_proxy.sh`，**dry-run 自检**（不改 env、不杀进程，只打印将要做的事 + 确认目标 proxy 健康）。
3. commit + push Part A + Part B（脚本）到 git。
4. **实际执行切换 = 跑 `switch_cc_proxy.sh 40005`** —— 这一步会杀掉我（webui 798 + 所有 claude）。我会在执行前给你最后一次确认。
   - 执行后：浏览器会断开几秒 → 新 webui 起来 → 你刷新浏览器/重开会话 → 新 CC 走 40005。

---

## 风险与保底

| 风险 | 缓解 |
|------|------|
| watchdog 没起来 → 3001 不释放 → webui 起不来 → 你失去我 | watchdog 日志写 `/tmp/`；systemd `Restart=always` 本身会重试；最坏情况你 SSH 进机器手动 `systemctl --user start cloudcli-webui.service` |
| sed 改 unit 失败 | 改前先 stop service + 备份 unit 文件 |
| claude 残留进程 | `pkill -9 -f claude` 兜底全杀 |
| 拆分引入 bug | 复制不删码，行为逐行不变；且 40002/40003 与 CC 无关，CC 走 40005 |

## 本轮提交
- `configs/proxy/codex-proxy/`（新）
- `configs/proxy/passthrough-proxy/`（新）
- `configs/docker-compose.yml`（改）
- `scripts/switch_cc_proxy.sh`（新）
- 更新 DEPLOY_STATUS.md + memory
