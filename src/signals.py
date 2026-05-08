"""
signals.py - USv1.0 信号计算与打分
纯 pandas/numpy 实现，不依赖 TA-Lib，避免编译麻烦。

打分维度（总分100）：
    ma50_trend      20   50日线之上且方向向上
    ma200_trend     10   200日线向上
    rs_vs_spy_6m    20   6个月相对强度 vs SPY
    pos_52w         10   距52周高/低位置
    kdj_j           10   KDJ_J<30满分，>80零分
    vol_price       10   量价配合（放量上涨）
    fwd_pe          10   前瞻PE合理性
    growth_yoy      10   营收/EPS增速
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd


# ------------------------ 技术指标 ------------------------

def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def kdj(df: pd.DataFrame, n: int = 9, k_period: int = 3, d_period: int = 3) -> pd.DataFrame:
    low_min = df["Low"].rolling(n, min_periods=n).min()
    high_max = df["High"].rolling(n, min_periods=n).max()
    rsv = (df["Close"] - low_min) / (high_max - low_min).replace(0, np.nan) * 100
    k = rsv.ewm(com=k_period - 1, adjust=False).mean()
    d = k.ewm(com=d_period - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return pd.DataFrame({"K": k, "D": d, "J": j}, index=df.index)


def relative_strength(target: pd.Series, benchmark: pd.Series, lookback: int = 126) -> float:
    """
    6个月相对强度（lookback 默认126交易日 ≈ 6个月）
    返回 (target 涨幅 - benchmark 涨幅)，正数=跑赢
    """
    if len(target) < lookback + 1 or len(benchmark) < lookback + 1:
        return 0.0
    tgt_ret = target.iloc[-1] / target.iloc[-lookback - 1] - 1
    bm_ret = benchmark.iloc[-1] / benchmark.iloc[-lookback - 1] - 1
    return float(tgt_ret - bm_ret)


# ------------------------ 打分数据结构 ------------------------

@dataclass
class ScoreCard:
    symbol: str
    price: float = 0.0
    change_pct: float = 0.0  # 最近一日涨跌
    ma20: float = 0.0
    ma50: float = 0.0
    ma200: float = 0.0
    kdj_j: float = 0.0
    rs_6m: float = 0.0
    dist_52w_high_pct: float = 0.0
    dist_52w_low_pct: float = 0.0
    volume_ratio: float = 0.0  # 当日成交量 / 20日均量
    fwd_pe: Optional[float] = None
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    market_cap: Optional[float] = None
    sector: Optional[str] = None
    earnings_soon: bool = False  # 未来2日内有财报

    # 分项得分
    s_ma50: float = 0.0
    s_ma200: float = 0.0
    s_rs: float = 0.0
    s_pos52w: float = 0.0
    s_kdj: float = 0.0
    s_volprice: float = 0.0
    s_fwdpe: float = 0.0
    s_growth: float = 0.0

    total_score: float = 0.0
    grade: str = ""       # A/B/C/D
    verdict: str = ""     # 狙击 / 观察 / 排除
    notes: list = field(default_factory=list)

    # ========== 多策略池评分（v2新增） ==========
    # 每个策略独立打分 0-100。strategy_scores 格式：
    #   {"breakout": {"score": 85, "grade": "A", "reasons": [...], ...}, ...}
    strategy_scores: dict = field(default_factory=dict)
    # 最高分策略池 name（用于榜单分类与展示）
    best_strategy: Optional[str] = None
    best_strategy_label: Optional[str] = None
    best_strategy_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------ 打分核心 ------------------------

def _score_ma50(price: float, ma50: float, ma50_prev: float) -> float:
    if pd.isna(ma50) or pd.isna(ma50_prev):
        return 0.0
    above = price > ma50
    rising = ma50 > ma50_prev
    if above and rising:
        return 20.0
    if above and not rising:
        return 10.0
    if not above and rising:
        return 5.0
    return 0.0


def _score_ma200(ma200: float, ma200_prev: float) -> float:
    if pd.isna(ma200) or pd.isna(ma200_prev):
        return 0.0
    if ma200 > ma200_prev:
        return 10.0
    return 0.0


def _score_rs(rs_6m: float) -> float:
    # 跑赢SPY 30%+ 满分，跑输 15% 零分
    if rs_6m >= 0.30:
        return 20.0
    if rs_6m <= -0.15:
        return 0.0
    # 线性插值
    return round((rs_6m + 0.15) / 0.45 * 20.0, 1)


def _score_pos_52w(dist_low_pct: float, dist_high_pct: float) -> float:
    """
    距低点 >= 25% 且 距高点 <= 25%（Minervini 趋势模板）
    """
    score = 0.0
    if dist_low_pct >= 25:
        score += 5.0
    elif dist_low_pct >= 10:
        score += 2.5
    if dist_high_pct <= 25:
        score += 5.0
    elif dist_high_pct <= 40:
        score += 2.5
    return score


def _score_kdj(j: float) -> float:
    if pd.isna(j):
        return 5.0
    if j < 30:
        return 10.0
    if j < 50:
        return 7.0
    if j < 80:
        return 4.0
    return 0.0


def _score_volprice(df: pd.DataFrame) -> float:
    """
    最近5日：看量价是否配合。放量上涨 +，放量下跌 -。
    """
    if len(df) < 25:
        return 5.0
    recent = df.tail(5)
    avg20_vol = df["Volume"].tail(20).mean()
    score = 5.0  # baseline
    for _, row in recent.iterrows():
        change = (row["Close"] - row["Open"]) / row["Open"] if row["Open"] else 0
        vol_ratio = row["Volume"] / avg20_vol if avg20_vol else 1
        if change > 0.01 and vol_ratio > 1.2:
            score += 1.0
        elif change < -0.01 and vol_ratio > 1.5:
            score -= 1.5
    return max(0.0, min(10.0, score))


def _score_fwdpe(fwd_pe: Optional[float]) -> float:
    if fwd_pe is None or fwd_pe <= 0:
        return 5.0  # 拿不到数据给中位
    if fwd_pe < 15:
        return 10.0
    if fwd_pe < 25:
        return 8.0
    if fwd_pe < 40:
        return 5.0
    if fwd_pe < 60:
        return 2.0
    return 0.0


def _score_growth(rev_g: Optional[float], eps_g: Optional[float]) -> float:
    vals = [v for v in [rev_g, eps_g] if v is not None]
    if not vals:
        return 5.0
    avg = sum(vals) / len(vals)
    if avg >= 0.25:
        return 10.0
    if avg >= 0.10:
        return 7.0
    if avg >= 0.0:
        return 4.0
    if avg >= -0.10:
        return 1.0
    return 0.0


def compute_scorecard(
    symbol: str,
    history: pd.DataFrame,
    info: dict,
    spy_history: pd.DataFrame,
    earnings_date=None,
) -> ScoreCard:
    # 本地 import 避免循环依赖
    from .strategies import score_all_strategies, best_strategy, STRATEGY_LABELS

    sc = ScoreCard(symbol=symbol)

    if history is None or len(history) < 50:
        sc.notes.append("历史数据不足50根，跳过打分")
        sc.verdict = "数据不足"
        return sc

    df = history.copy()
    df["MA20"] = sma(df["Close"], 20)
    df["MA50"] = sma(df["Close"], 50)
    df["MA200"] = sma(df["Close"], 200)

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last

    sc.price = float(last["Close"])
    sc.change_pct = float((last["Close"] - prev["Close"]) / prev["Close"] * 100) if prev["Close"] else 0.0
    sc.ma20 = float(last["MA20"]) if pd.notna(last["MA20"]) else 0.0
    sc.ma50 = float(last["MA50"]) if pd.notna(last["MA50"]) else 0.0
    sc.ma200 = float(last["MA200"]) if pd.notna(last["MA200"]) else 0.0

    # KDJ
    kdj_df = kdj(df)
    sc.kdj_j = float(kdj_df["J"].iloc[-1]) if pd.notna(kdj_df["J"].iloc[-1]) else 50.0

    # 相对强度
    if spy_history is not None and len(spy_history) > 130:
        sc.rs_6m = relative_strength(df["Close"], spy_history["Close"])

    # 52周位置
    window = df.tail(252) if len(df) >= 252 else df
    high_52w = float(window["High"].max())
    low_52w = float(window["Low"].min())
    sc.dist_52w_high_pct = (sc.price - high_52w) / high_52w * 100 if high_52w else 0
    sc.dist_52w_low_pct = (sc.price - low_52w) / low_52w * 100 if low_52w else 0

    # 量比
    avg20_vol = df["Volume"].tail(20).mean()
    sc.volume_ratio = float(last["Volume"] / avg20_vol) if avg20_vol else 1.0

    # 基本面
    sc.fwd_pe = info.get("forwardPE")
    sc.revenue_growth = info.get("revenueGrowth")
    sc.earnings_growth = info.get("earningsGrowth")
    sc.market_cap = info.get("marketCap")
    sc.sector = info.get("sector")

    # 财报黑名单
    if earnings_date is not None:
        try:
            days_to_earn = (pd.Timestamp(earnings_date).normalize() - pd.Timestamp.now().normalize()).days
            if 0 <= days_to_earn <= 2:
                sc.earnings_soon = True
                sc.notes.append(f"⚠ {days_to_earn}日内有财报，禁入")
        except Exception:
            pass

    # 分项打分
    sc.s_ma50 = _score_ma50(sc.price, last["MA50"], prev["MA50"])
    sc.s_ma200 = _score_ma200(last["MA200"], prev["MA200"])
    sc.s_rs = _score_rs(sc.rs_6m)
    sc.s_pos52w = _score_pos_52w(sc.dist_52w_low_pct, abs(sc.dist_52w_high_pct))
    sc.s_kdj = _score_kdj(sc.kdj_j)
    sc.s_volprice = _score_volprice(df)
    sc.s_fwdpe = _score_fwdpe(sc.fwd_pe)
    sc.s_growth = _score_growth(sc.revenue_growth, sc.earnings_growth)

    sc.total_score = round(
        sc.s_ma50 + sc.s_ma200 + sc.s_rs + sc.s_pos52w
        + sc.s_kdj + sc.s_volprice + sc.s_fwdpe + sc.s_growth, 1
    )

    # 财报黑名单强制降级
    if sc.earnings_soon:
        sc.total_score = min(sc.total_score, 55.0)

    # 分级
    if sc.total_score >= 80:
        sc.grade, sc.verdict = "A", "狙击（系统全绿）"
    elif sc.total_score >= 65:
        sc.grade, sc.verdict = "B", "观察（待回踩/待确认）"
    elif sc.total_score >= 50:
        sc.grade, sc.verdict = "C", "边缘（暂不参与）"
    else:
        sc.grade, sc.verdict = "D", "排除"

    # ========== 多策略池评分 ==========
    try:
        strat_map = score_all_strategies(df, info or {}, sc.rs_6m)
        sc.strategy_scores = {name: s.to_dict() for name, s in strat_map.items()}
        best = best_strategy(strat_map)
        if best is not None:
            sc.best_strategy = best.name
            sc.best_strategy_label = best.label
            sc.best_strategy_score = best.score
            # 财报黑名单 → 策略分也打折
            if sc.earnings_soon:
                sc.best_strategy_score = min(sc.best_strategy_score, 55.0)
    except Exception as e:
        sc.notes.append(f"策略池评分失败: {e}")

    return sc


def macro_snapshot(macro_data: dict) -> dict:
    """
    生成大盘风向标快照：SPY/QQQ/VIX/DXY 的最近变化。
    """
    out = {}
    for sym, bundle in macro_data.items():
        df = bundle.get("history")
        if df is None or len(df) < 52:
            continue
        last = df.iloc[-1]
        prev = df.iloc[-2]
        close = float(last["Close"])
        chg_1d = (close - float(prev["Close"])) / float(prev["Close"]) * 100
        ma50 = df["Close"].rolling(50).mean().iloc[-1]
        ma200 = df["Close"].rolling(200).mean().iloc[-1] if len(df) >= 200 else None

        status = []
        if pd.notna(ma50):
            status.append("价>50MA" if close > ma50 else "价<50MA")
        if ma200 is not None and pd.notna(ma200):
            status.append("价>200MA" if close > ma200 else "价<200MA")

        out[sym] = {
            "close": round(close, 2),
            "chg_1d": round(chg_1d, 2),
            "ma50": round(float(ma50), 2) if pd.notna(ma50) else None,
            "ma200": round(float(ma200), 2) if ma200 is not None and pd.notna(ma200) else None,
            "status": " / ".join(status),
        }
    return out
