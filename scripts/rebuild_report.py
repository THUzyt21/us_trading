"""
从 reports/raw/YYYY-MM-DD.json 反序列化 ScoreCard 并重新调用 reporter 生成最新版 MD + JSON。
用法：  python3 scripts/rebuild_report.py [YYYY-MM-DD]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# 让 src/ 可被导入
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.signals import ScoreCard
from src.reporter import generate_report


def _to_card(d: dict) -> ScoreCard:
    fields = {
        f: d.get(f) for f in ScoreCard.__dataclass_fields__
        if f in d
    }
    return ScoreCard(**fields)


def rebuild(date: str) -> Path:
    raw_path = ROOT / "reports" / "raw" / f"{date}.json"
    if not raw_path.exists():
        raise FileNotFoundError(raw_path)
    raw = json.loads(raw_path.read_text())

    macro = raw.get("macro", {})
    holdings = [_to_card(d) for d in raw.get("holdings", [])]
    watchlist = [_to_card(d) for d in raw.get("watchlist", [])]

    md_path = generate_report(macro, holdings, watchlist, report_date=date)
    print(f"[OK] {md_path}")
    print(f"     {md_path.with_suffix('.json')}")
    return md_path


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    rebuild(date)
