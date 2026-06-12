# 项目整理计划：归档、提炼、去重、保留核心功能

## 目标
让下一个人能直接接手这个项目，只保留核心功能代码和必要的历史教训，清除所有临时/冗余/过时文件。

## 1. 删除临时/无用文件

| 文件/目录 | 原因 | 操作 |
|-----------|------|------|
| `claude_output.log` | CC 启动终端输出的乱码日志，无价值 | 删除 |
| `plan.md` | R24 Codex 的旧计划文件，已实现，.gitignore 已排除 | 删除 |
| `backups/` | 单次备份快照(2026-06-12)，包含已删除的 proxy.py 和旧 dsv4p config，无恢复价值 | 删除整个目录 |
| `configs/logs/proxy/` | 6个旧 proxy 日志(metrics/error_detail 0605/0609/0610)，总量 1.1MB，历史日志不应在 git 中 | 删除 |
| `configs/logs/round_5_analysis.json` | 重复的(根目录 logs/ 也有) | 删除 |
| `configs/proxy/__pycache__/` | Python 编译缓存，不应在 git 中 | 删除 |
| `configs/proxy/gateway/__pycache__/` | Python 编译缓存 | 删除 |
| `.claude/plans/r8-529-fix-plan.md` | R8 旧计划，已完全实现并超述 | 删除 |
| `.claude/plans/r23-cleanup-plan.md` | R23 旧清理计划，已完全执行 | 删除 |
| `.claude/plans/r23-variant-fallback-plan.md` | R23 fallback 旧计划，已完全实现 | 删除 |
| `.claude/scheduled_tasks.json` | 空 `{"tasks":[]}`，无意义 | 删除 |
| `logs/round_1~7,16_analysis.json` | 7个历史分析 JSON(~23KB)，这些是运行快照而非持续参考 | 删除 |

## 2. 清理 cross_optimize/ 目录

| 文件 | 状态 | 操作 |
|------|------|------|
| `cross_optimize/opc2_uname_request.md` | R9 时期(2026-06-03)的旧配置，参数已完全过时(contextWindow=130K, num_retries=5, 11 variants) | 删除 |
| `cross_optimize/opc_uname_request.md` | 同上，R9 时期模板，引用已删除的 42001/41003 | 删除整个目录 |

## 3. auto-loop/ 目录 — 保留但标记

auto-loop 是从本项目提炼出的通用 skill 框架。它与 cc_repair_self 的核心功能（proxy gateway）无关，但作为衍生项目有独立价值。

**操作**: 保留，不做改动。它已经是一个完整的独立子项目。

## 4. 更新 .gitignore

新增排除项：
```
# Python cache
__pycache__/
*.pyc

# Proxy runtime logs (not for git)
configs/logs/proxy/

# Session analysis logs (temporary)
logs/

# Claude session output
claude_output.log
```

确认已有排除项保留：backups/, configs/.env, plan.md, PLAN.md 等

## 5. 精简 Memory 文件（21→约8个）

当前 21 个 memory 文件，很多内容已归入 CLAUDE.md 的"关键原则"和"不可变更约束"部分，属于重复。

**合并/删除策略**：

| Memory 文件 | 处理 | 原因 |
|-------------|------|------|
| `529-overloaded-compact-fail.md` | **删除** | 内容已归入 CLAUDE.md "关键原则" 529→CC崩溃 部分 |
| `remove-proxy-auto-compact.md` | **删除** | 已归入 CLAUDE.md "proxy绝不做截断/压缩" |
| `input-length-400-to-529-fix.md` | **删除** | 已归入 CLAUDE.md "429→529 转换会导致CC崩溃" |
| `inappropriate-content-fix.md` | **删除** | 已归入 CLAUDE.md（400 inappropriate content handling） |
| `thinking-budget-preflight-fix.md` | **删除** | 已归入 CLAUDE.md（不cycling的错误类型列表） |
| `auto-compact-window-fix.md` | **删除** | 已归入 CLAUDE.md 参数表 + 关键原则 |
| `never-call-health-endpoint.md` | **删除** | 已归入 CLAUDE.md "/health endpoint会触发fd耗尽" |
| `cc-v2170-startup-check.md` | **删除** | 已归入 CLAUDE.md "CC v2.1.170+ startup check用shell env vars" |
| `modelscope-force-stream-fix.md` | **删除** | 旧 R19 修复，已体现在 gateway stream.py 代码中 |
| `r19-proxy-rebuild.md` | **删除** | R19 教训"proxy变更必须rebuild"已归入 CLAUDE.md 重启命令 |
| `r19-1-timeout-logging.md` | **删除** | timeout 日志已体现在 upstream.py 代码中 |
| `r21-unified-container.md` | **删除** | R21 架构已归入 CLAUDE.md 和 DEPLOY_STATUS.md |
| `r23-variant-fallback.md` | **删除** | R23 fallback 机制已详述在 CLAUDE.md 中 |
| `metrics-24h-0610.md` | **删除** | 2026-06-10 的旧 metrics 快照，已过时 |
| `opc-uname-ssh-and-deploy.md` | **保留** | SSH/deploy 命令是持续需要的操作参考 |
| `tailscale-network-fix.md` | **保留** | 网络修复经验仍可能复现 |
| `openai-agent-direct-litellm-400.md` | **保留** | 关键约束：OpenAI agent 必须通过 proxy |
| `early-round-fixes-history.md` | **保留但精简** | 合并了 R1→R10 的历史，有参考价值但太长 |
| `user-language-preference.md` | **保留** | 用户偏好 |

**新增 1 个合并 Memory**：

- `core-lessons.md` — 将已删除的 9 个 memory 的**一句话核心教训**合并到一个文件，不重复 CLAUDE.md 已有的详细描述，只保留"一句话记忆钩子 + 为什么重要"：
  - 529→CC崩溃：绝不转换429→529
  - proxy绝不做截断/压缩
  - 删除资源前必须验证独立价值
  - proxy-level retry增加37%延迟
  - CC v2.1.170 startup check用shell env vars
  - /health endpoint会触发fd耗尽
  - CC tokenizer overestimates tokens ~1.7x
  - ModelScope双 quota 系统
  - 多CC进程加速token quota耗尽

## 6. 精简 DEPLOY_STATUS.md

当前 DEPLOY_STATUS.md 有 387 行，包含 R8→R24 全部变更历史。大量历史已不需要新人阅读。

**保留部分**（~150行）：
- 架构图（R24.2 当前架构）
- 容器表（3容器）
- 当前参数表
- opc2_uname 验证状态
- Key Issues & Notes（auto-compact, variant fallback, ModelScope quota, /health）
- R24.4 最终状态

**删除部分**：
- R23 Changes 详细（已归入 git history）
- R22 Changes 详细
- R21 Changes 详细
- R23.1 refactoring 详细
- R24.2 Codex 详细（改为简要说明）
- Parameter Change History 表（R1→R24，太长）

**改为简要历史表**（每轮一行）保留在 README.md 而非 DEPLOY_STATUS。

## 7. 精简 README.md

当前 README.md 有过时架构（4容器/40002/42001/41003）。

**重写为简洁版**：
- 一句话项目描述
- 当前架构图（R24.2，5 agent types → 40001 → 41001）
- 3容器表
- 不可变更约束
- 优化轮次历史（每轮一行，改为链接到 git history）
- 链接到 CLAUDE.md 完整文档

## 8. 更新 CLAUDE.md

CLAUDE.md 已经很完善（~200行核心内容），但需要小调整：
- 更新架构图删除已删除的 40002 行（如还有）
- 确保所有"关键原则"与 memory 合并后的 core-lessons.md 不冲突（CLAUDE.md 是主文档，memory 是补充钩子）
- 项目文件结构列表中删除 `configs/proxy/proxy.py`（已删除为 gateway/ package）

## 执行顺序

1. 删除临时/无用文件（git rm + rm）
2. 更新 .gitignore
3. 删除 cross_optimize/ 目录
4. 清理 memory 文件（删除→新建 core-lessons→更新 MEMORY.md）
5. 精简 DEPLOY_STATUS.md
6. 重写 README.md
7. 小更新 CLAUDE.md
8. git add + commit + push
