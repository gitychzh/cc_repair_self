# NV API (NVIDIA integrate.api.nvidia.com) 详细画像

## 测试时间：2026-06-22 18:30-19:00 CST (10:30-11:00 UTC, 周一)

## 1. NV API 服务器位置

| 域名 | IP | 位置 | 云厂商 |
|------|-----|------|--------|
| integrate.api.nvidia.com | 99.83.136.103 | **Seattle, WA, US** | Amazon AWS (Global Accelerator) |
| integrate.api.nvidia.com | 75.2.113.119 | **Seattle, WA, US** | Amazon AWS (Global Accelerator) |

**结论**: NV API 服务器在美国西海岸（Seattle）。**必须经美国代理访问**，直连延迟极高。

## 2. 5个 NV API Key 汇总

| Key# | 后缀 | 来源 | 状态(测试时) | 用途 |
|------|------|------|-------------|------|
| Key1 | ...OG_xYAlO | docker-compose.yml + .env | ❌ 429 (测试导致burst限速) | 主力key（proxy容器使用） |
| Key2 | ...AJT6yu6O | docker-compose.yml + .env | ✅ OK | 备用key（proxy容器使用） |
| Key3 | ...MXv_HZry | .env.template only | ✅ OK | 额外key（未被proxy使用，需启用） |
| Key4 | ...jGphx29O | .env.template only | ✅ OK | 额外key（未被proxy使用，需启用） |
| Key5 | ...ZnkN3swt | .env.template only | ✅ OK | 额外key（未被proxy使用，需启用） |

**重要发现**: Key1/Key2 在 docker-compose.yml 中配置（NV_NUM_KEYS=2 时只用到这2个），但 .env.template 中有 **5个key**。Key3-5 目前未被 proxy 使用。如启用 NV_NUM_KEYS=5，可利用全部5个key的独立 rate limit quota。

### Rate Limit 特性

| 特性 | 值 | 说明 |
|------|-----|------|
| 限速粒度 | **per-key** | Key1 429 时 Key2-5 正常，互不影响 |
| 稳定窗口 | ~10 req/短burst 可通过 | 15连续请求测试通过，0次429 |
| 限速恢复 | **10+ 分钟** | Key1 在burst后>15分钟仍429（恢复时间待精确测定） |
| 429 响体 | `{"status":429,"title":"Too Many Requests"}` | 无 X-RateLimit-* headers，无 retry-after |
| 文档声称 | ~40-60 RPM/key | NVIDIA 文档标注（实测未达到此上限） |

### Key 独立测试延迟（US-NV 7894 代理）

| Key# | 延迟(ms) | finish_reason |
|------|----------|---------------|
| Key1 | 4396 | length |
| Key2 | 5764 | length |
| Key3 | 8842 | length |
| Key4 | 12243 | length |
| Key5 | 7366 | length |

**延迟差异**: Key4 延迟明显高于其他（12s vs 4-7s），可能是 NV 内部 routing 或 burst queue 效果，非 key 本身差异。

## 3. 代理路由延迟对比

### 完整对比（3轮×5路由，2026-06-22 18:30 CST）

| 路由 | 代理端口 | 出口IP | 出口位置 | avg延迟(ms) | min(ms) | max(ms) | median(ms) | 可用性 |
|------|---------|--------|---------|------------|---------|---------|-----------|--------|
| **US-NV** | 7894 | 203.10.96.139 | **Los Angeles, CA, US** | **4247** | **566** | **16082** | **3304** | ✅ 最佳 |
| Japan | 7892 | 103.62.49.138 | Tokyo, JP | 9524 | 3229 | 18261 | 9573 | ⚠️ 慢 |
| Singapore | 7891 | **103.62.49.138** | **Tokyo, JP** (同日本！) | 13841 | 2947 | 26460 | 20887 | ⚠️ 很慢 |
| US-auto | 7893 | 103.62.49.170 | Tokyo, JP | 12252 | 4075 | 32681 | 8172 | ⚠️ 慢 |
| Mixed | 7880 | 103.62.49.138 | Tokyo, JP | ~17000 | 6555 | ~34000 | ~17000 | ⚠️ 很慢 |
| **Direct** | 无 | 218.93.250.242 | Nanjing, CN | **18947** | 8361 | 29821 | 22176 | ❌ 极慢 |

### 关键结论

1. **US-NV (7894) 是唯一可行的路由**：avg ~4s，其余路由 avg 10-25s
2. **日本和新加坡出口IP相同**（103.62.49.138）— mihomo 的 JP/SG 代理组选择了同一个 Tokyo 出口节点
3. **直连延迟 ~20s**：中国→美国 NV API 无代理时极慢
4. **US-NV 出口在洛杉矶**：距 Seattle 约 1500km，网络延迟很低
5. **延迟不稳定**：US-NV min=566ms, max=16082ms — NV API burst queue 效果

### 代理节点运营商

所有出口IP都属于 **GSL Networks Pty LTD** (AS137409)，一家全球 CDN/网络服务商。

### mihomo 配置

| 端口 | 代理组 | 选择策略 | 过滤规则 |
|------|--------|---------|---------|
| 7880 | mixed (🚀节点选择) | select | 全部节点 |
| 7891 | 🇸🇬狮城节点 | select | pq-provider (全部节点) |
| 7892 | 🇯🇵日本节点 | select | pq-provider (全部节点) |
| 7893 | ♻️US自动 | url-test (tolerance=50) | pq-provider |
| 7894 | ♻️US-NV | url-test (tolerance=100) | **nv-us-provider** (过滤: 美国|圣何塞|阿什本|洛杉矶) |

**US-NV (7894) 专用**: 从 89 个 pq-provider 节点中过滤出 32 个美国节点，url-test 选最佳5个。

## 4. NV API 模型可用性

121 个模型可用，关键模型：
- `z-ai/glm-5.1` — 当前目标模型 ✅
- `deepseek-ai/deepseek-v4-flash` / `deepseek-v4-pro` — 也可用
- `meta/llama-4-maverick-17b-128e-instruct` 等 — 未来备选

### glm-5.1 在 NV 上的特性

| 特性 | NV glm-5.1 | MS glm-5.1 |
|------|-----------|------------|
| model ID | z-ai/glm-5.1 | ZHIPUAI/GLM-5.1 (10 variants) |
| thinking_budget | ❌ 400 Unsupported | ✅ 支持 |
| reasoning_content | ✅ 返回 (delta.reasoning_content) | ✅ 返回 |
| streaming | ✅ SSE format | ✅ SSE format |
| finish_reason | ✅ stop/tool_calls/length | ✅ stop/tool_calls/length |
| max_tokens | 5-2000+ tested | 5-2000+ tested |
| 429 rate limit | per-key, ~10req/burst | per-deployment, 1RPM |
| rate limit recovery | >10min (per-key) | ~15min (per-deployment) |

## 5. 时间段推测

### 测试时间点数据

| 时间(CST) | UTC | 星期 | US-NV avg(ms) | 备注 |
|-----------|-----|------|--------------|------|
| 18:00 | 10:00 | Mon | 2200 (5次seq) | 最早测试，NV刚发现 |
| 18:34 | 10:34 | Mon | 3800 (3轮) | 系统对比测试 |
| 18:44 | 10:44 | Mon | 3300-16000 (3轮，variance大) | 第二轮对比 |

### 延迟波动分析

- **低延迟窗口**: 1000-3000ms (NV API 低负载时)
- **高延迟窗口**: 8000-16000ms (NV API burst queue 或高负载)
- **不稳定**: 同一分钟内延迟可以从 566ms 到 16082ms
- **burst 效果**: 连续请求会逐渐变慢（第1req 1.5s → 第3req 7s → 第5req 10s）

### 高峰/空闲推测（基于NV API在Seattle, US West Coast）

| 时间段(CST) | UTC | 推测负载 | 说明 |
|------------|-----|---------|------|
| 02:00-08:00 | 18:00-00:00 | **空闲** (US evening off-peak) | 美国下班后，NV API 低负载 |
| 08:00-14:00 | 00:00-06:00 | **中低** (US night) | 美国深夜/凌晨，最低负载 |
| 14:00-20:00 | 06:00-12:00 | **高峰** (US morning/noon) | 美国工作日早高峰 |
| 20:00-02:00 | 12:00-18:00 | **中高** (US afternoon) | 美国下午 |

**测试在 10:00-11:00 UTC (18:00-19:00 CST)** — 属于 US morning 高峰时段！
空闲时段测试需在 **CST 02:00-08:00** 进行。

## 6. NV 重启用建议

### 前置条件

- ✅ NV glm-5.1 API 恢复工作（5/5 key 成功）
- ✅ US-NV (7894) 代理延迟可接受（avg ~4s, 最佳 ~1s）
- ✅ 5个独立key，per-key rate limit
- ✅ streaming + finish_reason 正常
- ❌ rate limit 恢复时间未知（>10min，需进一步测定）
- ❌ 稳定性不足 24h 验证
- ❌ Key3-5 未在 proxy 配置中（需添加）

### 推荐方案

1. **先在 40005 EXPERIMENT 启用 NV_NUM_KEYS=2**（Key1+Key2，已有配置）
2. **观察 1-2 天**：NV interleaving 效果、429 率、延迟
3. **如稳定 → 升级到 NV_NUM_KEYS=5**（启用 Key3-5，增加独立 quota）
4. **如不稳定 → 回滚 NV_NUM_KEYS=0**

### 关键风险

- NV thinking_budget 400 → proxy 必须继续 strip（已有代码）
- NV 延迟不稳定（1s-16s）→ MS interleaving 时 TTFB 可能波动
- NV rate limit >10min恢复 → 远比 MS 的 ~15min恢复长
- NV 可能随时恢复不可用 → 需保留 NV_NUM_KEYS=0 回滚能力
