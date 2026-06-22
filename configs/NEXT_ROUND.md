# Round R35.14 (第7轮) — 2026-06-22 19:15 CST

## R35.14 验证数据（opc2_uname, 24h 运行数据）

### 40005 (cc-proxy, EXPERIMENT) — 1 request in 24h (CC流量极少)

| 指标 | R35.14 | R35.13 (opc_uname全天) |
|------|--------|----------------------|
| 总请求 | 1 | 1272 |
| 200率 | 100% | 99.1% |
| ABORT率 | 0% | 0% |
| NV traffic | 0 (NV disabled) | 0 |

极低流量说明 CC 本轮几乎没有使用。单个请求成功完成。

### 40001 (MIRROR): 0 request (idle)
### 40002 (codex): 未采集 (极低流量)

### 40003 (passthrough, OpenClaw/OpenHermes) — 49 requests in 24h

| 指标 | R35.14 (24h) | 备注 |
|------|-------------|------|
| 总请求 | 49 | OpenClaw日常流量 |
| 429 cycling events | 77 | 正常 burst throttle |
| 500 cycling events | 56 | **DNS outage 期间** |
| ABORT | 5 | 16:26-16:28 OpenClaw burst |
| ALL-KEYS-500/502 | 8 | 15:05-15:40 DNS outage |
| KEY-CYCLE-SUCCESS | 17 | 成功的 cycling |
| 成功率 | 73.5% (36/49) | 含DNS outage |
| 成功率(排除DNS) | ~87.5% | DNS后恢复 |

### 新发现1: ⚡ NV API 恢复可用！

- 经7893 US代理测试4/4全部200
- 延迟: 1.1s-7.4s（第一次稍慢，后续1.1-1.2s）
- thinking_budget: 仍400 Unsupported（proxy需strip）
- 内容完整，模型 z-ai/glm-5.1 正常工作
- **但 opc2_uname mihomo 没有7894端口** → NV_PROXY_URL=host.docker.internal:7894 无法连接
- 需要给opc2_uname mihomo 配7894+US-NV proxy-group才能重启用

### 新发现2: ModelScope DNS outage (15:05-15:40)

- LiteLLM日志: `socket.gaierror: Temporary failure in name resolution`
- 所有 variant/key 同时DNS失败 → 8次 ALL-KEYS-500/502 全失败
- proxy cycling正确工作但无法防护基础设施级DNS故障
- 35分钟后自动恢复，无需人工干预

### 新发现3: OpenClaw burst ABORT (16:26-16:28)

- 5次ABORT-NO-FALLBACK (v4/v5/v6 all-429)
- ~2分钟后恢复，后续请求正常
- burst期间多个OpenClaw/OpenHermes agent同时请求导致 RPM burst throttle

---

## Action: 本轮无需修改 ✅

**不做的事**（稳定优先，NV恢复但暂不启用）:
- ❌ 不重新启用 NV: API恢复是好消息，但需观察稳定性（之前连续3轮不可用R35.11/12/13）
- ❌ 不修改 opc2_uname mihomo: 配7894+US-NV是较大改动，需在确认NV稳定后再做
- ❌ 不修改任何参数: 系统运行稳定
- ❌ 不降低 throttle: burst ABORT说明1.5s间隔仍然必要
- ❌ 不添加DNS fallback: DNS outage是35min基础设施问题，proxy无法防护

**NV re-enable 前置条件**（下轮如NV仍可用则考虑）:
1. opc2_uname mihomo添加7894 listener + ♻️US-NV url-test proxy-group
2. NV连续2轮可用确认（R35.14✅ + R35.15待验证）
3. docker-compose.yml NV_NUM_KEYS=2 (先用2 key测试)
4. rebuild proxy容器并测试

---

## 参数现状 (R35.14)
PROXY_TIMEOUT=300 | UPSTREAM_TIMEOUT=60 | CPT=3.0 | SAFETY=170000 | THROTTLE=1.5s (ALL ports) | NV_NUM_KEYS=0 | NV_TIMEOUT=20 | MS_NV_TOTAL_SLOTS=N/A(pure MS) | LOG_RETENTION_DAYS=7 | is_quota_exhaustion=always-False | PROXY_TIMEOUT import in stream.py | dispatcher close_connection | passthrough SSE buffer-based parsing (R35.9 STABLE) | cc-proxy buffer-based parsing (100% FR) | MSG-FIX (R35.10 VERIFIED) | NV glm-5.1 API ✅ RECOVERED (4/4 200, 1.1-7.4s, not re-enabled pending mihomo config + stability monitoring)

## 连续无需修改计数: 4/5（R35.11/12/13/14, 还差1轮项目完工）

## 下轮待办 (R35.15)
- **NV API 稳定性确认**: 再次测试NV可用性，如果连续2轮可用→考虑逐步重启用
- **opc2_uname mihomo 7894 配置**: 如NV稳定→添加7894+US-NV proxy-group（需同步nv-us-provider）
- **NV_NUM_KEYS=2 测试**: 先用2 key小范围测试，不直接5 key
- **ModelScope DNS 监控**: DNS outage是否反复出现（单次是瞬态，反复出现需关注）
- **OpenClaw burst 频率**: 5 ABORT in 2min→恢复正常，是否需要更aggressive throttle？
