#!/usr/bin/env python3
"""proxy_health_score.py — Calculate and persist health scores for both CC proxy containers.

R35: Self-optimization framework component. Reads metrics from both 40001 and 40005,
computes a composite health score, and writes to configs/PROXY_HEALTH_SCORES.md for
monitoring and auto_tune.sh consumption.

Usage:
    python3 scripts/proxy_health_score.py           # Today's metrics
    python3 scripts/proxy_health_score.py 2026-06-20  # Specific date
"""

import json
import sys
import os
import datetime
from pathlib import Path

REPO = Path(os.environ.get("REPO_DIR", "/home/opc_uname/cc_ps/cc_repair_self"))
DEPLOY = Path("/opt/cc-infra")
SCORES_FILE = REPO / "configs" / "PROXY_HEALTH_SCORES.md"

# Score weights
W_SUCCESS = 40   # success rate weight
W_429 = 30       # 429 rate penalty weight
W_502 = 15       # 502 rate penalty weight
W_TTFB = 10      # TTFB weight
W_NV = 5         # NV interleaving bonus weight


def load_metrics(filepath: Path) -> list:
    """Load JSONL metrics file, return list of dicts."""
    entries = []
    if not filepath.exists():
        return entries
    try:
        with open(filepath) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception as e:
        sys.stderr.write(f"[HEALTH] Error reading {filepath}: {e}\n")
    return entries


def compute_stats(entries: list) -> dict:
    """Compute aggregate statistics from metrics entries."""
    if not entries:
        return {
            "total": 0, "success": 0, "success_rate": 0,
            "429_count": 0, "429_rate": 0, "502_count": 0, "502_rate": 0,
            "400_count": 0, "abort_no_fallback": 0,
            "ttfb_avg": 0, "ttfb_p50": 0, "ttfb_p95": 0,
            "duration_avg": 0, "duration_p50": 0,
            "ms_slots": 0, "nv_slots": 0, "nv_ratio": 0,
            "key_cycles": 0,
        }

    total = len(entries)
    success = 0
    s429 = 0; s502 = 0; s400 = 0; other_err = 0; abort = 0
    ttfb_list = []; dur_list = []
    ms_slots = 0; nv_slots = 0; key_cycles = 0

    for e in entries:
        st = e.get("status", 0)
        if st == 200:
            success += 1
            t = e.get("ttfb_ms")
            if t and t > 0:
                ttfb_list.append(t)
            d = e.get("duration_ms")
            if d and d > 0:
                dur_list.append(d)
            ut = e.get("upstream_type", "ms")
            if ut == "ms":
                ms_slots += 1
            elif ut == "nv":
                nv_slots += 1
            kc = e.get("key_cycle_429s_before_success", 0)
            if kc and kc > 0:
                key_cycles += 1
        elif st == 429:
            s429 += 1
            if e.get("error_type") in ("429_all_transient", "429_all_keys_exhausted") or \
               "ABORT" in str(e.get("error_message", "")):
                abort += 1
        elif st == 502:
            s502 += 1
        elif st == 400:
            s400 += 1
        elif st > 0:
            other_err += 1

    def avg(lst): return sum(lst)/len(lst) if lst else 0
    def p50(lst):
        if not lst: return 0
        s = sorted(lst)
        return s[len(s)//2]
    def p95(lst):
        if not lst: return 0
        s = sorted(lst)
        idx = int(len(s) * 0.95)
        return s[min(idx, len(s)-1)]

    return {
        "total": total,
        "success": success,
        "success_rate": success/total*100 if total else 0,
        "429_count": s429,
        "429_rate": s429/total*100 if total else 0,
        "502_count": s502,
        "502_rate": s502/total*100 if total else 0,
        "400_count": s400,
        "abort_no_fallback": abort,
        "ttfb_avg": avg(ttfb_list),
        "ttfb_p50": p50(ttfb_list),
        "ttfb_p95": p95(ttfb_list),
        "duration_avg": avg(dur_list),
        "duration_p50": p50(dur_list),
        "ms_slots": ms_slots,
        "nv_slots": nv_slots,
        "nv_ratio": nv_slots/(ms_slots+nv_slots)*100 if (ms_slots+nv_slots) else 0,
        "key_cycles": key_cycles,
    }


def compute_health_score(stats: dict) -> float:
    """Compute composite health score (0-100, higher = better).

    Formula:
      score = W_SUCCESS * success_rate/100
            - W_429 * 429_rate/100 * 3  (429 heavily penalized)
            - W_502 * 502_rate/100 * 2
            - W_TTFB * ttfb_avg/10000  (normalize: 10s = 10 points)
            + W_NV * nv_ratio/100  (NV interleaving bonus)
    """
    base = W_SUCCESS * (stats["success_rate"] / 100)
    penalty_429 = W_429 * (stats["429_rate"] / 100) * 3
    penalty_502 = W_502 * (stats["502_rate"] / 100) * 2
    penalty_ttfb = W_TTFB * (stats["ttfb_avg"] / 10000)
    bonus_nv = W_NV * (stats["nv_ratio"] / 100)

    score = base - penalty_429 - penalty_502 - penalty_ttfb + bonus_nv
    return max(0, min(100, score))


def write_scores_md(s01: dict, s05: dict, score01: float, score05: float, date: str):
    """Write health scores to PROXY_HEALTH_SCORES.md."""
    now = datetime.datetime.now().isoformat()

    # Determine verdict
    if s05["total"] == 0:
        verdict = "NO_DATA_40005"
    elif s05["total"] < 5:
        verdict = "INSUFFICIENT_DATA"
    elif score05 >= score01 + 5:
        verdict = "PROMOTE_40005"
    elif score05 < score01 - 10:
        verdict = "ROLLBACK_40005"
    else:
        verdict = "STABLE"

    content = f"""# Proxy Health Scores (R35 auto-generated)

> Updated: {now} | Date: {date}

## 40001 (Fallback/Stable) — Score: {score01:.1f}/100

| Metric | Value |
|--------|-------|
| Total requests | {s01['total']} |
| Success rate | {s01['success_rate']:.1f}% |
| 429 rate | {s01['429_rate']:.1f}% ({s01['429_count']} events) |
| 502 rate | {s01['502_rate']:.1f}% ({s01['502_count']} events) |
| 400 rate | {s01['400_count']} events |
| ABORT-NO-FALLBACK | {s01['abort_no_fallback']} |
| TTFB avg/p50/p95 | {s01['ttfb_avg']:.0f}/{s01['ttfb_p50']:.0f}/{s01['ttfb_p95']:.0f} ms |
| Duration avg/p50 | {s01['duration_avg']:.0f}/{s01['duration_p50']:.0f} ms |
| NV ratio | {s01['nv_ratio']:.1f}% (pure MS) |

## 40005 (Primary/Experiment) — Score: {score05:.1f}/100

| Metric | Value |
|--------|-------|
| Total requests | {s05['total']} |
| Success rate | {s05['success_rate']:.1f}% |
| 429 rate | {s05['429_rate']:.1f}% ({s05['429_count']} events) |
| 502 rate | {s05['502_rate']:.1f}% ({s05['502_count']} events) |
| 400 rate | {s05['400_count']} events |
| ABORT-NO-FALLBACK | {s05['abort_no_fallback']} |
| TTFB avg/p50/p95 | {s05['ttfb_avg']:.0f}/{s05['ttfb_p50']:.0f}/{s05['ttfb_p95']:.0f} ms |
| Duration avg/p50 | {s05['duration_avg']:.0f}/{s05['duration_p50']:.0f} ms |
| NV ratio | {s05['nv_ratio']:.1f}% ({s05['nv_slots']} NV / {s05['ms_slots']} MS) |

## Verdict: {verdict}

- PROMOTE_40005: 40005 outperforms → eligible for version promotion to 40001
- ROLLBACK_40005: 40005 underperforms → consider rollback
- STABLE: Similar performance → continue observing
- INSUFFICIENT_DATA: Not enough traffic to evaluate
- NO_DATA_40005: No metrics for 40005

## Score Formula
```
score = 40*success_rate/100 - 30*429_rate*3/100 - 15*502_rate*2/100 - 10*ttfb_avg/10000 + 5*nv_ratio/100
```
"""

    SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SCORES_FILE, "w") as f:
        f.write(content)
    print(f"Health scores written to {SCORES_FILE}")


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()

    m40001_path = DEPLOY / "logs" / "proxy40001" / f"metrics.{date}.jsonl"
    m40005_path = DEPLOY / "logs" / "proxy40005" / f"metrics.{date}.jsonl"

    # Also check old path for 40001 (transition period)
    m40001_old = DEPLOY / "logs" / "proxy" / f"metrics.{date}.jsonl"

    entries01 = load_metrics(m40001_path)
    if not entries01:
        entries01 = load_metrics(m40001_old)

    entries05 = load_metrics(m40005_path)

    s01 = compute_stats(entries01)
    s05 = compute_stats(entries05)
    score01 = compute_health_score(s01)
    score05 = compute_health_score(s05)

    print(f"=== Proxy Health Scores ({date}) ===")
    print(f"40001: {score01:.1f}/100 | success={s01['success_rate']:.1f}% | 429={s01['429_rate']:.1f}% | TTFB={s01['ttfb_avg']:.0f}ms")
    print(f"40005: {score05:.1f}/100 | success={s05['success_rate']:.1f}% | 429={s05['429_rate']:.1f}% | TTFB={s05['ttfb_avg']:.0f}ms")

    write_scores_md(s01, s05, score01, score05, date)


if __name__ == "__main__":
    main()
