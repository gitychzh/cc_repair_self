# R30: Variant 跨容器全局轮询（PostgreSQL 序列）

## 问题
当前 `_vk_rr_counter` 是每个 proxy 容器进程内的内存 dict（config.py:259），从 0 起步。
- 3 个 proxy 容器各自独立计数 → 40001(cc→glm5.2) 和 40002(codex→glm5.2) 共享同一组 70 个 glm5.2 dep，但 counter 独立 → 必然碰撞同一 variant
- 容器重启 counter 归零 → 所有新请求重新从 v1k1 开始
- 实测远程：v0 被打 22 次（其他 variant 5-7 次），v0 的 429 率 27.3%（本机均匀分布时仅 9.5%）

## 目标
无论哪个 session / agent 进程 / 容器请求，variant×key 都应从**全局共享的当前位置**绝对轮询。
上一个请求打了第 36 号（v6k1），下一个新 session 的请求必须打第 37 号（v6k2），而不是从头 v1k1。

## 方案：PostgreSQL SEQUENCE

用 cc_postgres 里现成的 `litellm_glm51` 库，为 glm5.2 和 dsv4p 各建一个 SEQUENCE。
3 个 proxy 容器都连同一个 DB，用原子操作 `nextval()` 拿到全局递增的 N。

### 为什么选 SEQUENCE 而非 UPDATE+RETURNING
- SEQUENCE 是 PG 原生原子计数器，专为高并发递增设计
- `nextval()` 即使在事务回滚时也**不回退**（这正是我们要的：失败了 key cycling 消耗的也不重用）
- 比 row+lock 更轻量、无锁竞争

### N → (variant_idx, key_idx) 映射不变
保留现有 2D 公式：
```
variant_idx = (N // NUM_KEYS) % NUM_VARIANTS
key_idx     = N % NUM_KEYS
```
NUM_KEYS=7, NUM_VARIANTS=10 → N 从 0..69 覆盖全部 70 个组合，N=70 回到 v1k1。

## 改动文件

### 1. `configs/postgres/init-db.sh`
不动。SEQUENCE 在 proxy 启动时 lazy 创建（`CREATE SEQUENCE IF NOT EXISTS`），避免改 postgres 初始化 + 重建容器。

### 2. `configs/proxy/Dockerfile`
加一行装 psycopg2-binary：
```dockerfile
RUN pip install --no-cache-dir psycopg2-binary==2.9.10
```
（LiteLLM 精简镜像里没有 psycopg2，实测确认）

### 3. `configs/proxy/gateway/rr_store.py`（新文件）
封装 PG 连接 + nextval：

```python
import os, threading
import psycopg2
from .logger import _log

# 全局连接池（每进程一个连接，ThreadedHTTPServer 多线程共享）
_conn = None
_conn_lock = threading.Lock()

DB_URL = os.environ.get("DATABASE_URL") or \
    "postgresql://litellm:{}@cc_postgres:5432/litellm_glm51".format(
        os.environ.get("POSTGRES_PASSWORD", ""))

def _get_conn():
    global _conn
    if _conn is not None and _conn.closed == 0:
        return _conn
    with _conn_lock:
        if _conn is None or _conn.closed != 0:
            _conn = psycopg2.connect(DB_URL, connect_timeout=5)
            _conn.autocommit = True  # nextval 不需要事务包裹
            _ensure_sequences(_conn)
            _log("RR-DB", f"connected to PG, sequences ready")
    return _conn

def _ensure_sequences(conn):
    cur = conn.cursor()
    for model in ("glm5.2", "dsv4p"):
        seq = f"rr_{model.replace('.', '_')}_seq"  # rr_glm5_2_seq
        cur.execute(f"""
            CREATE SEQUENCE IF NOT EXISTS {seq}
            INCREMENT 1 MINVALUE 0 MAXVALUE 69 START 0 CACHE 1 CYCLE
        """)
    conn.commit()

def next_counter(model: str) -> int:
    """原子递增，返回当前 N (0..69)。失败回退到进程内 counter。"""
    seq = f"rr_{model.replace('.', '_')}_seq"
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT nextval('{seq}')")
        n = int(cur.fetchone()[0])
        return n
    except Exception as e:
        _log("RR-DB-ERR", f"nextval failed, fallback to local: {e}")
        return None  # 调用方处理 fallback
```

### 4. `configs/proxy/gateway/config.py`
修改 `_next_variant_key_pair`：优先 PG nextval，失败回退到进程内 counter（容错）。

```python
from .rr_store import next_counter

_vk_rr_counter = {}  # 仅作 PG 不可用时的 fallback
_vk_rr_lock = threading.Lock()

def _next_variant_key_pair(model: str) -> tuple:
    num_variants = NUM_VARIANTS.get(model, 10)
    n = next_counter(model)
    if n is None:
        # PG 不可用 → 进程内 fallback（保持可用性）
        with _vk_rr_lock:
            n = _vk_rr_counter.get(model, 0)
            _vk_rr_counter[model] = n + 1
    variant_idx = (n // NUM_KEYS) % num_variants
    key_idx = n % NUM_KEYS
    return (variant_idx, key_idx)
```

### 5. `configs/docker-compose.yml`
给 3 个 proxy 容器加 `DATABASE_URL` 和 `POSTGRES_PASSWORD`：
```yaml
DATABASE_URL: postgresql://litellm:${POSTGRES_PASSWORD}@cc_postgres:5432/litellm_glm51
POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
```
（depends_on cc_postgres 已有，无需改）

## 验证方法
1. 部署后，3 个 proxy 容器启动时各自 `CREATE SEQUENCE IF NOT EXISTS`（幂等，只第一个实际创建）
2. 跨容器测试：在 40001 和 40002 交替发请求，看 litellm_model 是否严格递增（不重复、不归零）
3. metrics 里 variant_idx 分布应均匀（每个 variant 5-7 次，而非 v0 独占 22 次）

## 风险与回退
- **风险**: PG 连接失败 → fallback 到进程内 counter（行为同 R29，不会崩）
- **CYCLE**: SEQUENCE 设 CYCLE，到 69 后回 0（正常轮询行为）
- **CACHE=1**: 牺牲一点性能换严格顺序（多容器不预取导致乱序）；实测请求量低（~100/天），无性能问题
- **回退**: 注释掉 Dockerfile 的 pip install + config.py 改回内存 counter 即可

## 不改动
- error cycling / variant fallback / key cycling 逻辑全部不动
- N → (v, k) 映射公式不动
- 429 cycling 仍然在 key 层（同 variant 换 key），只是**起始点**现在全局协调
