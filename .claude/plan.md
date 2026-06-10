# R15 Plan: GLM IQ-safe autoCompactWindow 180K→140K

## 背景
社区研究（Stanford "Lost in the Middle" + Reddit/V2EX 反馈）证实：
- 所有LLM在高上下文(>100K tokens)都会出现智商下降（推理减弱、指令遵循下降、中间信息丢失）
- GLM系列同样受影响，实际有效智商窗口远低于宣称的128K/202K
- 用户要求保守：150K real tokens 就触发 auto-compact

## 实测数据支撑（06-10 post-R7, chars/token=3.0）
| Context (real) | Old conv ratio | New conv ratio |
|---------------|----------------|----------------|
| 0-50K | 1.078 | 1.047 |
| 50-100K | 0.995 | 0.976 |
| 100-150K | 1.033 | 0.896 |
| 150-180K | **1.058** | **0.865** |

旧会话ratio=1.058（低估5.8%）→ worst case; 新会话ratio=0.865（过估13.5%）→ better case

## 参数计算
autoCompactWindow=140000 (est tokens):
- 旧会话(worst): 140K est → ~148K real → just below 150K ✓
- 新会话(better): 140K est → ~117-121K real → below 150K ✓
- **保证所有会话在150K real以内compact**

contextWindow=170000 (降低, 与safety对齐):
- 旧会话: 170K est → ~179K real → below 202.7K ✓
- 新会话: 170K est → ~147K real → below ModelScope ✓
- 30K buffer: compact(140K) ↔ hard limit(170K) → CC auto-compact正常

MODEL_INPUT_TOKEN_SAFETY: 190000→170000 (与contextWindow对齐)
CLAUDE_CODE_AUTO_COMPACT_WINDOW env: 180000→140000

## 变更文件
1. `configs/claude/settings-opc_uname.json` — contextWindow 190→170, autoCompactWindow 180→140, env 180→140
2. `configs/claude/settings-opc2_uname.json` — autoCompactWindow 150→140, env 150→140 (contextWindow already 170)
3. `configs/docker-compose.yml` — MODEL_INPUT_TOKEN_SAFETY 190000→170000 (两处proxy)
4. `configs/DEPLOY_STATUS.md` — 添加R15记录
5. Copy 06-10 metrics/error_detail → configs/logs/

## 本机部署
1. backup: bash scripts/backup_config.sh
2. sync settings.json → ~/.claude/settings.json
3. rebuild proxy: docker compose up -d --build --force-recreate auth_to_api_40001
4. restart Claude: bash ~/cc_ps/cc_recover/restart_claude.sh
5. curl test glm5.1 + dsv4p
6. verify docker inspect env vars

## 对齐逻辑
contextWindow 和 MODEL_INPUT_TOKEN_SAFETY 必须对齐：
- contextWindow=170K → CC认为模型最大170K est tokens
- MODEL_INPUT_TOKEN_SAFETY=170K → proxy /v1/models 报告170K context_window
- 如果safety=190而contextWindow=170 → CC在170K就认为超限但proxy说还有190K → 矛盾
- 所以两者必须一致：170K