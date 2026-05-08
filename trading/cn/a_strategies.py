# -*- coding: utf-8 -*-
"""
a_strategies.py - A-share Multi-Strategy Pool Scoring Module
=============================================================

Adds 3 independent strategy pools to complement the existing chip_screener_v3
(which becomes the 4th "Chip" pool):

  🚀 Breakout   放量创新高突破       胜率 60-70%  (动量右侧)
  🎯 Trend      Minervini 强势趋势   胜率 55-65%  (强者恒强)
  🔄 Pullback   强势股回踩不破       胜率 55-65%  (最佳介入时机)

Each strategy scores 0-100 independently, grades A/B/C/D.
A stock can belong to multiple pools; the highest-scoring pool is the primary label.

Data format: Tushare daily (columns: trade_date, open, high, low, close, vol, amount, pct_chg)
RS benchmark: CSI 300 (沪深300) instead of SPY.

Author: 龙虾 x 老哥
Date: 2026-05-08
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ======================== Data Structures ========================

@dataclass
class StrategyScore:
    """Score result for a single strategy pool."""
    name: str                           # breakout / trend / pullback
    label: str                          # Chinese display name
    score: float = 0.0                  # 0-100
    grade: str = ""                     # A(>=80) / B(>=65) / C(>=50) / D
    passed_gates: bool = False          # Hard gates passed? (False = score zeroed)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


STRATEGY_LABELS = {
    "breakout": "🚀 突破",
    "trend":    "🎯 趋势",
    "pullback": "🔄 回踩",
}


def _grade(score: float) -> str:
    if score >= 80: return "A"
    if score >= 65: return "B"
    if score >= 50: return "C"
    return "D"


# ======================== Utilities ========================

def _sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average."""
    return s.rolling(n, min_periods=n).mean()


def _rsi(close: pd.Series, n: int = 14) -> float:
    """Last RSI value."""
    if len(close) < n + 1:
        return 50.0
    delta = close.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    rs = up / down.replace(0, np.nan)
    rsi_series = 100 - 100 / (1 + rs)
    v = rsi_series.iloc[-1]
    return float(v) if pd.notna(v) else 50.0


def _pct(a: float, b: float) -> float:
    """Percentage change from b to a."""
    return (a - b) / b * 100 if b else 0.0


def compute_rs_vs_index(stock_close: pd.Series, index_close: pd.Series,
                        lookback: int = 120) -> float:
    """
    Compute relative strength vs index (e.g. CSI300) over lookback days.
    Returns: float, e.g. 0.15 means stock outperformed index by 15%.
    """
    if len(stock_close) < lookback or len(index_close) < lookback:
        return 0.0
    stock_ret = (float(stock_close.iloc[-1]) / float(stock_close.iloc[-lookback]) - 1)
    index_ret = (float(index_close.iloc[-1]) / float(index_close.iloc[-lookback]) - 1)
    return stock_ret - index_ret


# ======================== Configuration ========================

A_STRATEGY_CONFIG = {
    # --- Breakout Pool ---
    'breakout_max_dist_high_pct': -5.0,     # within 5% of 250d high
    'breakout_min_vol_ratio': 1.3,          # volume ratio threshold
    'breakout_min_score': 50,               # minimum score to qualify

    # --- Trend Pool ---
    'trend_min_dist_low_pct': 30.0,         # at least 30% above 250d low
    'trend_max_dist_high_pct': -15.0,       # within 15% of 250d high (not too far from top)
    'trend_min_rs': 0.15,                   # RS must outperform CSI300 by ≥15%
    'trend_min_ma_spread': 0.05,            # MA50 must be > MA200 * 1.05 (real divergence)
    'trend_min_score': 50,

    # --- Pullback Pool ---
    'pullback_rsi_low': 35,
    'pullback_rsi_high': 60,
    'pullback_max_vol_shrink': 1.1,         # pullback volume < 1.1x of pre-rally
    'pullback_min_score': 50,

    # --- Common ---
    'min_history_days': 70,                 # minimum bars needed
    'trend_min_history_days': 210,          # for MA200 calculation
}


# ========================================================================
# 1. BREAKOUT — 放量创新高突破
# ========================================================================

def score_breakout(df: pd.DataFrame, rs_6m: float = 0.0) -> StrategyScore:
    """
    🚀 Breakout Pool: Volume breakout near 250-day high.

    Hard gates (any fail → score=0):
      • Within 5% of 250-day high
      • Recent volume surge (today or 3d avg > 1.3x of 20d avg)
      • Price above MA50
      • MA50 > MA200 (bull alignment)

    Scoring (100 pts):
      30  Breakout confirmation (new 60d high + volume)
      25  Box consolidation → explosion (low volatility then breakout)
      15  Relative strength vs CSI300
      15  MA alignment strength (MA20>MA50>MA150>MA200 + direction)
      10  Volume quality (strong body, no distribution wick)
       5  Proximity to new high
    """
    s = StrategyScore(name="breakout", label=STRATEGY_LABELS["breakout"])
    if df is None or len(df) < A_STRATEGY_CONFIG['min_history_days']:
        s.warnings.append("历史数据不足70根")
        return s

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["vol"].astype(float)
    open_ = df["open"].astype(float)

    price = float(close.iloc[-1])
    ma20 = _sma(close, 20).iloc[-1]
    ma50 = _sma(close, 50).iloc[-1]
    ma150 = _sma(close, 150).iloc[-1] if len(close) >= 150 else np.nan
    ma200 = _sma(close, 200).iloc[-1] if len(close) >= 200 else np.nan
    ma50_prev = _sma(close, 50).iloc[-6] if len(close) >= 55 else ma50

    # 250-day high (A-share "52 week" equivalent)
    win = close.tail(250) if len(close) >= 250 else close
    high_250d = float(win.max())
    dist_high = _pct(price, high_250d)  # negative = below high

    # Volume metrics
    vol20 = float(vol.tail(20).mean())
    vol_ratio_today = float(vol.iloc[-1] / vol20) if vol20 > 0 else 1.0
    vol_ratio_3d = float(vol.tail(3).mean() / vol20) if vol20 > 0 else 1.0

    s.details = {
        "price": round(price, 2),
        "dist_250d_high_pct": round(dist_high, 2),
        "vol_ratio_today": round(vol_ratio_today, 2),
        "vol_ratio_3d": round(vol_ratio_3d, 2),
        "rs_6m_pct": round(rs_6m * 100, 1),
    }

    # ------- Hard Gates -------
    gates_ok = True
    if dist_high < A_STRATEGY_CONFIG['breakout_max_dist_high_pct']:
        s.warnings.append(f"距250日高 {dist_high:.1f}% (需 ≥{A_STRATEGY_CONFIG['breakout_max_dist_high_pct']}%)")
        gates_ok = False
    if not (vol_ratio_today >= A_STRATEGY_CONFIG['breakout_min_vol_ratio']
            or vol_ratio_3d >= A_STRATEGY_CONFIG['breakout_min_vol_ratio']):
        s.warnings.append(f"近期无放量 (today={vol_ratio_today:.2f}x, 3d={vol_ratio_3d:.2f}x)")
        gates_ok = False
    if pd.isna(ma50) or price < ma50:
        s.warnings.append("价 < MA50")
        gates_ok = False
    if pd.notna(ma200) and pd.notna(ma50) and ma50 < ma200:
        s.warnings.append("MA50 < MA200（非多头排列）")
        gates_ok = False

    s.passed_gates = gates_ok
    if not gates_ok:
        return s

    score = 0.0

    # 30 pts: Breakout confirmation — new 60d high + volume
    high_60 = float(close.tail(60).max())
    break_price = float(close.tail(3).max())
    if break_price >= high_60 * 0.998 and vol_ratio_3d >= 1.3:
        score += 30
        s.reasons.append(f"突破60日新高+放量 {vol_ratio_3d:.2f}x")
    elif price >= high_60 * 0.98:
        score += 15
        s.reasons.append(f"接近60日新高 ({_pct(price, high_60):+.1f}%)")

    # 25 pts: Box consolidation → explosion
    if len(close) >= 50:
        pre_range = float(close.iloc[-50:-10].max() - close.iloc[-50:-10].min()) / float(close.iloc[-30])
        recent_move = (price - float(close.iloc[-11])) / float(close.iloc[-11])
        if pre_range < 0.20 and recent_move > 0.05:
            score += 25
            s.reasons.append(f"箱体(前40日振幅{pre_range:.1%})后爆发 +{recent_move:.1%}")
        elif pre_range < 0.30 and recent_move > 0.03:
            score += 15
            s.reasons.append("温和收敛后启动")

    # 15 pts: Relative strength vs CSI300
    if rs_6m >= 0.20:
        score += 15
        s.reasons.append(f"RS 跑赢沪深300 {rs_6m*100:+.1f}%")
    elif rs_6m >= 0.05:
        score += 8
    elif rs_6m < -0.05:
        s.warnings.append(f"RS 落后沪深300 {rs_6m*100:+.1f}%")

    # 15 pts: MA alignment strength
    aligned = 0
    if pd.notna(ma20) and pd.notna(ma50) and ma20 > ma50:
        aligned += 1
    if pd.notna(ma50) and pd.notna(ma150) and ma50 > ma150:
        aligned += 1
    if pd.notna(ma150) and pd.notna(ma200) and ma150 > ma200:
        aligned += 1
    if pd.notna(ma50) and pd.notna(ma50_prev) and ma50 > ma50_prev:
        aligned += 1
    score += aligned * 3.75  # max 15
    if aligned >= 3:
        s.reasons.append(f"多头排列强 ({aligned}/4)")

    # 10 pts: Volume quality (strong body, no distribution wick)
    last_close = float(close.iloc[-1])
    last_open = float(open_.iloc[-1])
    last_high = float(high.iloc[-1])
    body = abs(last_close - last_open)
    upper_wick = last_high - max(last_close, last_open)
    if vol_ratio_today >= 1.5:
        if body > 0 and upper_wick < body * 1.5:
            score += 10
            s.reasons.append(f"放量 {vol_ratio_today:.2f}x 实体强")
        else:
            score += 3
            s.warnings.append("放量但长上影（警惕出货）")
    elif vol_ratio_today >= 1.2:
        score += 6

    # 5 pts: Proximity to new high
    if dist_high >= -1:
        score += 5
    elif dist_high >= -3:
        score += 3

    s.score = round(min(100.0, score), 1)
    s.grade = _grade(s.score)
    return s


# ========================================================================
# 2. TREND — Minervini 强势趋势
# ========================================================================

def score_trend(df: pd.DataFrame, rs_6m: float = 0.0) -> StrategyScore:
    """
    🎯 Trend Pool: Minervini Stage 2 template (strong momentum continuation).

    Hard gates (any fail → score=0):
      • Price > MA50 > MA150 > MA200 (strict bull alignment)
      • MA200 trending up over last 20 days
      • At least 25% above 250-day low
      • Within 30% of 250-day high

    Scoring (100 pts):
      30  Minervini template perfection (alignment + divergence)
      25  Relative strength vs CSI300
      20  MA spread health (MA50-MA200 gap)
      15  Position above 250d low (higher = better, capped)
      10  Volume trend (rising volume on up days)
    """
    s = StrategyScore(name="trend", label=STRATEGY_LABELS["trend"])
    if df is None or len(df) < A_STRATEGY_CONFIG['trend_min_history_days']:
        s.warnings.append("历史数据不足210根（无法算MA200趋势）")
        return s

    close = df["close"].astype(float)
    vol = df["vol"].astype(float)
    price = float(close.iloc[-1])

    ma50 = _sma(close, 50).iloc[-1]
    ma150 = _sma(close, 150).iloc[-1]
    ma200_series = _sma(close, 200)
    ma200 = ma200_series.iloc[-1]
    ma200_20ago = ma200_series.iloc[-21] if len(ma200_series) >= 21 else ma200

    # 250-day range
    win = close.tail(250) if len(close) >= 250 else close
    high_250d = float(win.max())
    low_250d = float(win.min())
    dist_high = _pct(price, high_250d)
    dist_low = _pct(price, low_250d)

    s.details = {
        "price": round(price, 2),
        "ma50": round(float(ma50), 2) if pd.notna(ma50) else 0,
        "ma150": round(float(ma150), 2) if pd.notna(ma150) else 0,
        "ma200": round(float(ma200), 2) if pd.notna(ma200) else 0,
        "dist_250d_high_pct": round(dist_high, 2),
        "dist_250d_low_pct": round(dist_low, 2),
        "rs_6m_pct": round(rs_6m * 100, 1),
    }

    # ------- Hard Gates -------
    gates_ok = True
    if not (pd.notna(ma50) and pd.notna(ma150) and pd.notna(ma200)
            and price > ma50 > ma150 > ma200):
        s.warnings.append("未形成严格多头排列 价>MA50>MA150>MA200")
        gates_ok = False
    if pd.notna(ma200) and pd.notna(ma200_20ago) and ma200 <= ma200_20ago:
        s.warnings.append("MA200 方向非向上")
        gates_ok = False
    if dist_low < A_STRATEGY_CONFIG['trend_min_dist_low_pct']:
        s.warnings.append(f"距250日低仅 {dist_low:.1f}% (需 ≥{A_STRATEGY_CONFIG['trend_min_dist_low_pct']}%)")
        gates_ok = False
    if dist_high < A_STRATEGY_CONFIG['trend_max_dist_high_pct']:
        s.warnings.append(f"距250日高 {dist_high:.1f}% (需 ≥{A_STRATEGY_CONFIG['trend_max_dist_high_pct']}%)")
        gates_ok = False
    # RS gate: must outperform CSI300
    if rs_6m < A_STRATEGY_CONFIG['trend_min_rs']:
        s.warnings.append(f"RS {rs_6m*100:+.1f}% 不足 (需 ≥+{A_STRATEGY_CONFIG['trend_min_rs']*100:.0f}%)")
        gates_ok = False
    # MA spread gate: MA50 must be meaningfully above MA200
    if pd.notna(ma50) and pd.notna(ma200):
        ma_spread = (float(ma50) - float(ma200)) / float(ma200)
        if ma_spread < A_STRATEGY_CONFIG['trend_min_ma_spread']:
            s.warnings.append(f"均线发散度 {ma_spread:.1%} 不足 (需 ≥{A_STRATEGY_CONFIG['trend_min_ma_spread']:.0%})")
            gates_ok = False

    s.passed_gates = gates_ok
    if not gates_ok:
        return s

    score = 0.0

    # 30 pts: Minervini template perfection
    ma20 = _sma(close, 20).iloc[-1]
    template = 0
    if pd.notna(ma20) and ma20 > ma50: template += 1
    if price > ma50 * 1.02: template += 1
    if ma50 > ma150 * 1.03: template += 1
    if ma150 > ma200 * 1.02: template += 1
    if ma200 > ma200_20ago * 1.01: template += 1
    score += template * 6  # max 30
    if template >= 4:
        s.reasons.append(f"Minervini模板强度 {template}/5")

    # 25 pts: Relative strength vs CSI300
    if rs_6m >= 0.30:
        score += 25
        s.reasons.append(f"RS +{rs_6m*100:.1f}% (强势)")
    elif rs_6m >= 0.15:
        score += 18
    elif rs_6m >= 0.05:
        score += 10
    else:
        s.warnings.append(f"RS 仅 {rs_6m*100:+.1f}%")

    # 20 pts: MA spread health (MA50/MA200 ratio)
    spread = (float(ma50) - float(ma200)) / float(ma200)
    if 0.08 <= spread <= 0.30:
        score += 20
        s.reasons.append(f"均线发散健康 {spread:.1%}")
    elif 0.04 <= spread < 0.08:
        score += 12
    elif spread > 0.30:
        score += 8
        s.warnings.append(f"均线发散过大 {spread:.1%} (过热)")
    else:
        score += 5

    # 15 pts: Position above 250d low
    if 40 <= dist_low <= 80:
        score += 15
        s.reasons.append(f"距250日低 +{dist_low:.0f}% (黄金位)")
    elif 25 <= dist_low < 40:
        score += 10
    elif 80 < dist_low <= 150:
        score += 10
    else:
        score += 5

    # 10 pts: Volume trend (up-day volume > down-day volume)
    if len(close) >= 20:
        pct_chg = close.pct_change()
        recent_20 = pct_chg.tail(20)
        vol_20 = vol.tail(20)
        up_vol = float(vol_20[recent_20 > 0].mean()) if (recent_20 > 0).any() else 0
        down_vol = float(vol_20[recent_20 < 0].mean()) if (recent_20 < 0).any() else 1
        if down_vol > 0 and up_vol / down_vol >= 1.3:
            score += 10
            s.reasons.append(f"量能趋势健康 (涨日量/跌日量={up_vol/down_vol:.2f})")
        elif down_vol > 0 and up_vol / down_vol >= 1.1:
            score += 6
        else:
            score += 3

    s.score = round(min(100.0, score), 1)
    s.grade = _grade(s.score)
    return s


# ========================================================================
# 3. PULLBACK — 强势股回踩不破
# ========================================================================

def score_pullback(df: pd.DataFrame, rs_6m: float = 0.0) -> StrategyScore:
    """
    🔄 Pullback Pool: Strong stock pulling back to MA20/MA50 support.

    Hard gates (any fail → score=0):
      • Made a 60-day high within last 50 days (proof of strength)
      • Current price above MA50 (not broken)
      • RSI between 35-60 (pulled back but not oversold)
      • Volume shrinking during pullback

    Scoring (100 pts):
      30  Pullback depth + position (near MA20/MA50, -5% to -15% from high)
      25  Relative strength still strong
      20  Volume shrinkage quality
      15  MA50 still trending up
      10  Bottom signal (hammer candle / close above MA20)
    """
    s = StrategyScore(name="pullback", label=STRATEGY_LABELS["pullback"])
    if df is None or len(df) < A_STRATEGY_CONFIG['min_history_days']:
        s.warnings.append("历史数据不足70根")
        return s

    close = df["close"].astype(float)
    vol = df["vol"].astype(float)
    open_ = df["open"].astype(float)
    low = df["low"].astype(float)
    price = float(close.iloc[-1])

    ma20 = _sma(close, 20).iloc[-1]
    ma50_series = _sma(close, 50)
    ma50 = ma50_series.iloc[-1]
    ma50_10ago = ma50_series.iloc[-11] if len(ma50_series) >= 11 else ma50

    # 50-day high and days since
    high_50 = float(close.tail(50).max())
    high_50_idx = close.tail(50).idxmax()
    try:
        days_since_high = len(close) - 1 - close.index.get_loc(high_50_idx)
    except Exception:
        days_since_high = 0

    dist_from_high_pct = _pct(price, high_50)  # negative = pulled back
    rsi = _rsi(close, 14)

    # 60-day high confirmation (proof stock was strong)
    high_60 = float(close.tail(60).max())
    made_60d_high_recently = (days_since_high <= 50 and high_50 >= high_60 * 0.98)

    # Volume shrinkage during pullback
    if days_since_high > 0 and days_since_high <= len(vol):
        pullback_len = min(days_since_high, 10)
        vol_during_pullback = float(vol.iloc[-pullback_len:].mean())
    else:
        vol_during_pullback = float(vol.tail(5).mean())

    # Volume before the high (the rally phase)
    if days_since_high >= 5 and len(vol) >= days_since_high + 10:
        vol_before_high = float(vol.iloc[-days_since_high - 10:-days_since_high].mean())
    else:
        vol_before_high = float(vol.tail(20).mean())

    vol_shrink_ratio = vol_during_pullback / vol_before_high if vol_before_high > 0 else 1.0

    s.details = {
        "price": round(price, 2),
        "ma20": round(float(ma20), 2) if pd.notna(ma20) else 0,
        "ma50": round(float(ma50), 2) if pd.notna(ma50) else 0,
        "days_since_high": days_since_high,
        "dist_from_high_pct": round(dist_from_high_pct, 2),
        "rsi_14": round(rsi, 1),
        "vol_shrink_ratio": round(vol_shrink_ratio, 2),
        "rs_6m_pct": round(rs_6m * 100, 1),
    }

    # ------- Hard Gates -------
    gates_ok = True
    if not made_60d_high_recently:
        s.warnings.append("50日内未创60日新高（非强势股）")
        gates_ok = False
    if pd.isna(ma50) or price < float(ma50) * 0.97:  # allow 3% wick below
        s.warnings.append("价跌破 MA50*0.97")
        gates_ok = False
    if not (A_STRATEGY_CONFIG['pullback_rsi_low'] <= rsi <= A_STRATEGY_CONFIG['pullback_rsi_high']):
        s.warnings.append(f"RSI {rsi:.0f} 超出 {A_STRATEGY_CONFIG['pullback_rsi_low']}-{A_STRATEGY_CONFIG['pullback_rsi_high']} 区间")
        gates_ok = False
    if vol_shrink_ratio >= A_STRATEGY_CONFIG['pullback_max_vol_shrink']:
        s.warnings.append(f"回踩未缩量 (量能比 {vol_shrink_ratio:.2f})")
        gates_ok = False

    s.passed_gates = gates_ok
    if not gates_ok:
        return s

    score = 0.0

    # 30 pts: Pullback depth + position near MA
    dist_to_ma20 = abs(_pct(price, float(ma20))) if pd.notna(ma20) else 99
    dist_to_ma50 = abs(_pct(price, float(ma50)))
    if -15 <= dist_from_high_pct <= -5 and (dist_to_ma20 <= 3 or dist_to_ma50 <= 3):
        score += 30
        s.reasons.append(f"回踩{dist_from_high_pct:.1f}% 至均线附近")
    elif -20 <= dist_from_high_pct <= -3:
        score += 18
        s.reasons.append(f"温和回踩{dist_from_high_pct:.1f}%")
    elif -5 < dist_from_high_pct <= -1:
        score += 12
        s.reasons.append(f"浅回踩{dist_from_high_pct:.1f}%")

    # 25 pts: Relative strength
    if rs_6m >= 0.20:
        score += 25
        s.reasons.append(f"RS 仍强 +{rs_6m*100:.1f}%")
    elif rs_6m >= 0.08:
        score += 15
    elif rs_6m >= 0:
        score += 8

    # 20 pts: Volume shrinkage
    if vol_shrink_ratio <= 0.6:
        score += 20
        s.reasons.append(f"显著缩量 {vol_shrink_ratio:.2f}x")
    elif vol_shrink_ratio <= 0.75:
        score += 14
        s.reasons.append(f"缩量 {vol_shrink_ratio:.2f}x")
    elif vol_shrink_ratio <= 0.9:
        score += 8

    # 15 pts: MA50 still trending up
    if pd.notna(ma50) and pd.notna(ma50_10ago) and float(ma50) > float(ma50_10ago) * 1.005:
        score += 15
        s.reasons.append("MA50 趋势完好")
    elif pd.notna(ma50) and pd.notna(ma50_10ago) and float(ma50) >= float(ma50_10ago):
        score += 8

    # 10 pts: Bottom signal (bullish candle at support)
    last_close = float(close.iloc[-1])
    last_open = float(open_.iloc[-1])
    last_low = float(low.iloc[-1])
    body = last_close - last_open
    lower_wick = min(last_open, last_close) - last_low
    if body > 0 and pd.notna(ma20) and price > float(ma20):
        score += 10
        s.reasons.append("阳线收回 MA20 之上")
    elif lower_wick > abs(body) * 1.5 and lower_wick > 0:
        score += 7
        s.reasons.append("长下影见底信号")
    elif body > 0:
        score += 4

    s.score = round(min(100.0, score), 1)
    s.grade = _grade(s.score)
    return s


# ========================================================================
# Unified Entry Point
# ========================================================================

STRATEGY_FUNCS = {
    "breakout": score_breakout,
    "trend":    score_trend,
    "pullback": score_pullback,
}


def score_all_strategies(df: pd.DataFrame, rs_6m: float = 0.0) -> Dict[str, StrategyScore]:
    """
    Run all 3 strategy pools on a single stock.
    Returns {name: StrategyScore}.
    """
    return {name: fn(df, rs_6m) for name, fn in STRATEGY_FUNCS.items()}


def best_strategy(scores: Dict[str, StrategyScore]) -> Optional[StrategyScore]:
    """
    Get the highest-scoring strategy pool that passed gates.
    Returns None if all pools failed.
    """
    passed = [s for s in scores.values() if s.passed_gates and s.score > 0]
    if not passed:
        return None
    return max(passed, key=lambda s: s.score)


def format_strategy_summary(scores: Dict[str, StrategyScore]) -> str:
    """
    One-line summary of all strategy results for display.
    Example: "🚀突破 72B | 🎯趋势 -- | 🔄回踩 55C"
    """
    parts = []
    for name in ["breakout", "trend", "pullback"]:
        s = scores.get(name)
        if s and s.passed_gates and s.score > 0:
            parts.append(f"{s.label} {s.score:.0f}{s.grade}")
        else:
            parts.append(f"{STRATEGY_LABELS[name]} --")
    return " | ".join(parts)
