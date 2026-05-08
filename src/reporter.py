"""
reporter.py - 美股每日报告生成器
输出：
  1) reports/YYYY-MM-DD.md       人类可读的Markdown报告（喂给IMA/龙虾解读）
  2) reports/YYYY-MM-DD.json     与 MD 对齐的精简 JSON（四大策略池 Top N + 持仓 + 大盘）
  3) reports/raw/YYYY-MM-DD.json 全市场全量 ScoreCard 归档（供回测/历史对比）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict

from .signals import ScoreCard


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 策略池定义（顺序即报告展示顺序）
STRATEGY_POOLS = [
    ("breakout", "🚀 突破池", "放量创新高突破",        "胜率 60-70% · 强势股动量"),
    ("trend",    "🎯 趋势池", "Minervini 强势趋势模板",  "胜率 55-65% · 持有期较长"),
    ("pullback", "🔄 回踩池", "强势股回踩 MA20/MA50 不破", "胜率 55-65% · 强势股最佳介入"),
    ("reversal", "🔥 反转池", "底部放量确认反转",        "胜率 40-50% · 赔率高但胜率低"),
]


def _fmt_num(v, digits=2, pct=False, default="—"):
    if v is None:
        return default
    try:
        if pct:
            return f"{v * 100:.{digits}f}%" if abs(v) < 10 else f"{v:.{digits}f}%"
        return f"{v:.{digits}f}"
    except Exception:
        return default


def _fmt_mc(v):
    if v is None:
        return "—"
    if v >= 1e12:
        return f"{v/1e12:.2f}T"
    if v >= 1e9:
        return f"{v/1e9:.1f}B"
    if v >= 1e6:
        return f"{v/1e6:.0f}M"
    return f"{v:.0f}"


def build_macro_table(macro: dict) -> str:
    if not macro:
        return "_大盘数据缺失_\n"
    lines = ["| 代码 | 收盘 | 涨跌% | 50MA | 200MA | 状态 |",
             "|---|---|---|---|---|---|"]
    for sym, d in macro.items():
        lines.append(
            f"| **{sym}** | {d['close']} | {d['chg_1d']:+.2f}% | "
            f"{d.get('ma50') or '—'} | {d.get('ma200') or '—'} | {d.get('status','')} |"
        )
    return "\n".join(lines) + "\n"


def _score_row(sc: ScoreCard) -> str:
    earn_flag = " 🔴财报" if sc.earnings_soon else ""
    return (
        f"| **{sc.symbol}**{earn_flag} | **{sc.total_score:.1f}** ({sc.grade}) "
        f"| {sc.price:.2f} | {sc.change_pct:+.2f}% "
        f"| {sc.ma50:.2f} | {sc.ma200:.2f} "
        f"| {sc.kdj_j:.1f} | {sc.rs_6m*100:+.1f}% "
        f"| {sc.dist_52w_high_pct:+.1f}% / {sc.dist_52w_low_pct:+.1f}% "
        f"| {sc.volume_ratio:.2f}x "
        f"| {_fmt_num(sc.fwd_pe,1)} "
        f"| {_fmt_num(sc.revenue_growth, 1, pct=True)} / {_fmt_num(sc.earnings_growth, 1, pct=True)} "
        f"| {sc.verdict} |"
    )


def build_score_table(title: str, cards: List[ScoreCard]) -> str:
    if not cards:
        return f"### {title}\n\n_无数据_\n"
    header = (
        "| 代码 | 总分(等级) | 现价 | 日涨跌 | 50MA | 200MA | KDJ_J | RS(6M) "
        "| 距高/低(52W) | 量比 | FwdPE | 营收/EPS增速 | 判定 |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = "\n".join(_score_row(c) for c in cards)
    return f"### {title}\n\n{header}{rows}\n"


def _strat_row(sc: ScoreCard, strat_name: str) -> str:
    """渲染单只票在指定策略池中的一行"""
    strat = (sc.strategy_scores or {}).get(strat_name, {})
    score = strat.get("score", 0) or 0
    grade = strat.get("grade", "—")
    reasons = strat.get("reasons") or []
    warnings = strat.get("warnings") or []
    earn_flag = " 🔴财报" if sc.earnings_soon else ""
    reason_txt = "; ".join(reasons[:3]) if reasons else "—"
    warn_txt = "; ".join(warnings[:2]) if warnings else ""
    if warn_txt:
        reason_txt = f"{reason_txt}  ⚠️ {warn_txt}" if reasons else f"⚠️ {warn_txt}"
    return (
        f"| **{sc.symbol}**{earn_flag} | **{score:.1f}** ({grade}) "
        f"| {sc.price:.2f} | {sc.change_pct:+.2f}% "
        f"| {sc.rs_6m*100:+.1f}% "
        f"| {sc.dist_52w_high_pct:+.1f}% / {sc.dist_52w_low_pct:+.1f}% "
        f"| {sc.volume_ratio:.2f}x "
        f"| {_fmt_num(sc.fwd_pe,1)} "
        f"| {sc.sector or '—'} "
        f"| {reason_txt} |"
    )


def build_pool_table(cards: List[ScoreCard], strat_name: str, top_n: int = 3) -> str:
    """
    从所有 cards 中挑出 "best_strategy == strat_name" 且通过门槛的，按该池得分降序，取 Top N
    """
    filtered = [
        c for c in cards
        if c.best_strategy == strat_name
        and (c.strategy_scores or {}).get(strat_name, {}).get("passed_gates", False)
    ]
    filtered.sort(
        key=lambda c: (c.strategy_scores or {}).get(strat_name, {}).get("score", 0),
        reverse=True,
    )
    top = filtered[:top_n]
    if not top:
        return f"_本池今日无合格候选_\n"

    header = (
        "| 代码 | 策略分(等级) | 现价 | 日涨跌 | RS(6M) | 距高/低(52W) "
        "| 量比 | FwdPE | 板块 | 加分理由 |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = "\n".join(_strat_row(c, strat_name) for c in top)
    total_in_pool = len(filtered)
    caption = f"_本池合格候选共 {total_in_pool} 只，展示 Top {len(top)}_\n\n" if total_in_pool > top_n else ""
    return caption + header + rows + "\n"


def build_strategy_summary(cards: List[ScoreCard]) -> str:
    """策略池候选数量统计速览"""
    counts = {name: 0 for name, *_ in STRATEGY_POOLS}
    a_counts = {name: 0 for name, *_ in STRATEGY_POOLS}
    for c in cards:
        if c.best_strategy and c.best_strategy in counts:
            strat = (c.strategy_scores or {}).get(c.best_strategy, {})
            if strat.get("passed_gates"):
                counts[c.best_strategy] += 1
                if strat.get("grade") == "A":
                    a_counts[c.best_strategy] += 1

    lines = ["| 策略池 | 定位 | 合格数 | A级(≥80) |",
             "|---|---|---|---|"]
    for name, label, desc, winrate in STRATEGY_POOLS:
        lines.append(
            f"| {label} | {desc} | **{counts[name]}** | {a_counts[name]} |"
        )
    return "\n".join(lines) + "\n"


def generate_report(
    macro: dict,
    holdings_cards: List[ScoreCard],
    watchlist_cards: List[ScoreCard],
    report_date: str = None,
    pool_top: int = 3,
) -> Path:
    """
    生成每日报告。

    Args:
        pool_top: 每个策略池展示 Top N（默认3，核心战术清单）
                  主 JSON 与 MD 完全对齐；全量数据归档到 reports/raw/
    """
    report_date = report_date or datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ========== 构建 Markdown ==========
    md = []
    md.append(f"# 美股鹰眼系统 · 每日速报 v2（多策略池）")
    md.append(f"**报告日期**：{report_date} （生成时间 {now_str}）")
    md.append(f"_战术清单：4 个策略池各展示 Top {pool_top}；完整数据见 `{report_date}.json`_\n")
    md.append("---\n")

    # 1. 大盘风向标
    md.append("## 一、大盘风向标")
    md.append("")
    md.append(build_macro_table(macro))
    md.append("")

    # 2. 持仓诊断
    md.append("## 二、持仓诊断")
    md.append("")
    md.append(build_score_table("老哥当前持仓", holdings_cards))
    md.append("")

    # 3. 策略池速览
    md.append("## 三、策略池速览")
    md.append("")
    md.append(build_strategy_summary(watchlist_cards))
    md.append("")

    # 4. 四大策略池 Top N —— 核心战术清单
    md.append(f"## 四、四大策略池 · Top {pool_top}（核心战术清单）")
    md.append("")
    md.append("> 每只票在 4 个池中独立评分，取**最高分池**作为主标签，只出现在一个池里（避免跨池重复）。")
    md.append("")
    for i, (name, label, desc, winrate) in enumerate(STRATEGY_POOLS, start=1):
        md.append(f"### 4.{i} {label}  ·  {desc}")
        md.append(f"_{winrate}_")
        md.append("")
        md.append(build_pool_table(watchlist_cards, name, top_n=pool_top))
        md.append("")

    # 5. 系统说明
    md.append("## 五、系统说明（供 AI 解读参考）")
    md.append("""
### 四大策略池定义

| 池 | 硬门槛 | 核心评分 | 胜率 | 操作要点 |
|---|---|---|---|---|
| 🚀 **突破** | 距52W高≤5%、放量≥1.3x、价>MA50、MA50>MA200 | 突破新高+箱体爆发+量能+RS | 60-70% | 追入当日，止损设在突破前箱体下沿 |
| 🎯 **趋势** | 价>MA50>MA150>MA200、MA200向上、距低≥30%、距高≤25% | Minervini模板+RS+均线发散+增速 | 55-65% | 持有时间长，MA50 下方止损 |
| 🔄 **回踩** | 50日内创60日新高、价>MA50*0.97、RSI 35-60、缩量 | 回踩深度+RS+缩量+见底K线 | 55-65% | 最佳介入强势股时机，止损近期低点 |
| 🔥 **反转** | 距高≤-25%、放量≥1.5x、价>MA20 | 反转结构+放量+MA50上翘+RSI回升 | 40-50% | 赔率高但仓位轻，分批建仓 |

### 风控铁律（所有策略通用）
- 硬止损 **-12%**，首次止盈 **+25% 卖 1/3**
- 剩余 2/3 用 **MA20 追踪止损**
- **财报前 2 日禁入**（系统自动识别 🔴）
- 单只最大仓位 **20%**，同时最多持有 **3 只 A 级策略信号**

### 阅读顺序建议
1. 先看"第一章 大盘"：SPY/QQQ 在 50MA 之上 & VIX < 20 → 可进攻；反之轻仓
2. 再看"第二章 持仓"：跌破 MA50 / 策略信号转差的票 → 立即警戒 / 止损
3. 核心战术在"第四章 四大策略池"：
   - 激进老哥优先看 🚀 突破 + 🎯 趋势（胜率最高）
   - 稳健老哥优先看 🔄 回踩（买的便宜，止损近）
   - 🔥 反转池只用小仓位试错（赔率高但胜率低）
""")

    md_text = "\n".join(md)

    # 写 Markdown
    md_path = REPORTS_DIR / f"{report_date}.md"
    md_path.write_text(md_text, encoding="utf-8")

    # ========== 写 JSON（与 MD 对齐的精简版）==========
    # 每个池挑合格 & best_strategy 指向本池的票，按池内得分降序取 Top N
    def _pool_top(strat_name: str, top_n: int) -> list:
        filtered = [
            c for c in watchlist_cards
            if c.best_strategy == strat_name
            and (c.strategy_scores or {}).get(strat_name, {}).get("passed_gates", False)
        ]
        filtered.sort(
            key=lambda c: (c.strategy_scores or {}).get(strat_name, {}).get("score", 0),
            reverse=True,
        )
        return [c.to_dict() for c in filtered[:top_n]]

    # 每池合格总数（对齐 MD 第三章速览）
    pool_stats = {}
    for name, label, desc, winrate in STRATEGY_POOLS:
        qualified = [
            c for c in watchlist_cards
            if c.best_strategy == name
            and (c.strategy_scores or {}).get(name, {}).get("passed_gates", False)
        ]
        a_grade = [
            c for c in qualified
            if (c.strategy_scores or {}).get(name, {}).get("grade") == "A"
        ]
        pool_stats[name] = {
            "label": label,
            "desc": desc,
            "winrate": winrate,
            "qualified": len(qualified),
            "a_grade": len(a_grade),
        }

    pools_payload = {
        name: _pool_top(name, pool_top)
        for name, *_ in STRATEGY_POOLS
    }

    payload = {
        "date": report_date,
        "generated_at": now_str,
        "config": {
            "pool_top": pool_top,
            "watchlist_total": len(watchlist_cards),
        },
        "macro": macro,
        "holdings": [c.to_dict() for c in holdings_cards],
        "pool_stats": pool_stats,
        "pools": pools_payload,
    }
    json_path = REPORTS_DIR / f"{report_date}.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    # ========== 全量数据归档到 raw/ 子目录（供回测/历史对比，不污染主目录）==========
    raw_dir = REPORTS_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_payload = {
        "date": report_date,
        "generated_at": now_str,
        "macro": macro,
        "holdings": [c.to_dict() for c in holdings_cards],
        "watchlist": [c.to_dict() for c in watchlist_cards],
    }
    raw_path = raw_dir / f"{report_date}.json"
    raw_path.write_text(json.dumps(raw_payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    return md_path
