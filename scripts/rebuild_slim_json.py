"""
一次性/复用脚本：从 reports/raw/YYYY-MM-DD.json (全量) 重建对应的精简主 JSON
用法：  python3 scripts/rebuild_slim_json.py [YYYY-MM-DD]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


STRATEGY_POOLS = [
    ("breakout", "🚀 突破池", "放量创新高突破",          "胜率 60-70% · 强势股动量"),
    ("trend",    "🎯 趋势池", "Minervini 强势趋势模板",   "胜率 55-65% · 持有期较长"),
    ("pullback", "🔄 回踩池", "强势股回踩 MA20/MA50 不破", "胜率 55-65% · 强势股最佳介入"),
    ("reversal", "🔥 反转池", "底部放量确认反转",          "胜率 40-50% · 赔率高但胜率低"),
]
POOL_TOP = 3


def rebuild(date: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    raw_path = root / "reports" / "raw" / f"{date}.json"
    out_path = root / "reports" / f"{date}.json"
    if not raw_path.exists():
        raise FileNotFoundError(raw_path)

    raw = json.loads(raw_path.read_text())
    watchlist = raw.get("watchlist", [])
    holdings = raw.get("holdings", [])

    def pool_top(name: str, n: int):
        f = [
            c for c in watchlist
            if c.get("best_strategy") == name
            and (c.get("strategy_scores") or {}).get(name, {}).get("passed_gates")
        ]
        f.sort(
            key=lambda c: (c.get("strategy_scores") or {}).get(name, {}).get("score", 0),
            reverse=True,
        )
        return f[:n]

    pool_stats = {}
    for name, label, desc, winrate in STRATEGY_POOLS:
        q = [
            c for c in watchlist
            if c.get("best_strategy") == name
            and (c.get("strategy_scores") or {}).get(name, {}).get("passed_gates")
        ]
        a = [c for c in q if (c.get("strategy_scores") or {}).get(name, {}).get("grade") == "A"]
        pool_stats[name] = {
            "label": label, "desc": desc, "winrate": winrate,
            "qualified": len(q), "a_grade": len(a),
        }

    pools = {name: pool_top(name, POOL_TOP) for name, *_ in STRATEGY_POOLS}

    payload = {
        "date": raw.get("date", date),
        "generated_at": raw.get("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M")),
        "config": {
            "pool_top": POOL_TOP,
            "watchlist_total": len(watchlist),
        },
        "macro": raw.get("macro", {}),
        "holdings": holdings,
        "pool_stats": pool_stats,
        "pools": pools,
    }
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"[OK] {out_path} ({out_path.stat().st_size/1024:.1f} KB, "
          f"{len(out_path.read_text().splitlines())} lines)")
    print(f"     watchlist 全量 {len(watchlist)} 只 -> "
          f"pool_stats {[(n, pool_stats[n]['qualified']) for n,*_ in STRATEGY_POOLS]}")
    print(f"     holdings={len(holdings)}")
    return out_path


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    rebuild(date)
