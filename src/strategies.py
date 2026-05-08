"""
strategies.py - 多策略池评分模块

为解决"USv1.0 单一打分体系偏爱超跌反弹"问题，按操作风格拆分 4 个独立策略池：

  🚀 breakout   放量创新高突破       胜率 60-70%  (动量右侧)
  🎯 trend      Minervini 强势趋势   胜率 55-65%  (强者恒强，持有期长)
  🔄 pullback   强势股回踩不破       胜率 55-65%  (强势股最佳介入时机)
  🔥 reversal   底部放量确认反转     胜率 40-50%  (赔率高，原 USv1.0 偏向)

每个策略独立打分 0-100，独立判级 A/B/C/D；同一只票可同时属于多个池，
取"最高分池"作为主标签。所有策略函数签名一致：
    score_<name>(df, info, sc) -> StrategyScore
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


# ------------------------ 通用数据结构 ------------------------

@dataclass
class StrategyScore:
    name: str                           # breakout / trend / pullback / reversal
    label: str                          # 中文显示名（🚀 突破 等）
    score: float = 0.0                  # 0-100
    grade: str = ""                     # A(>=80) / B(>=65) / C(>=50) / D
    passed_gates: bool = False          # 是否通过硬门槛（否则 score 直接清零）
    reasons: List[str] = field(default_factory=list)     # 加分理由
    warnings: List[str] = field(default_factory=list)    # 扣分/警告
    details: Dict[str, float] = field(default_factory=dict)  # 关键指标快照

    def to_dict(self) -> dict:
        return asdict(self)


STRATEGY_LABELS = {
    "breakout": "🚀 突破",
    "trend":    "🎯 趋势",
    "pullback": "🔄 回踩",
    "reversal": "🔥 反转",
}


def _grade(score: float) -> str:
    if score >= 80: return "A"
    if score >= 65: return "B"
    if score >= 50: return "C"
    return "D"


# ------------------------ 小工具 ------------------------

def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _rsi(close: pd.Series, n: int = 14) -> float:
    """最后一个 RSI 值"""
    if len(close) < n + 1:
        return 50.0
    delta = close.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    rs = up / down.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    v = rsi.iloc[-1]
    return float(v) if pd.notna(v) else 50.0


def _recent_high(close: pd.Series, n: int) -> float:
    return float(close.tail(n).max())


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100 if b else 0.0


# ========================================================================
# 1. BREAKOUT —— 放量创新高突破
# ========================================================================

def score_breakout(df: pd.DataFrame, info: dict, rs_6m: float) -> StrategyScore:
    """
    🚀 突破池：放量突破新高 / 箱体上沿

    硬门槛（任一不满足 score=0）：
      • 距 52W 高 ≤ 5%（必须非常接近新高）
      • 当日或近 3 日出现放量（vol > 20d_avg * 1.3）
      • 价在 MA50 之上
      • MA50 > MA200（多头排列）

    评分（100分）：
      30  突破确认（当日/近3日创60日新高 + 放量）
      25  箱体 → 突破（前40-60日波动率 < 后10日，形态收敛后爆发）
      15  相对强度 RS 跑赢 SPY
      15  多头排列强度（MA20>MA50>MA150>MA200 + 方向一致向上）
      10  成交量质量（放量 + 不是出货长上影）
       5  距离新高越近越好
    """
    s = StrategyScore(name="breakout", label=STRATEGY_LABELS["breakout"])
    if df is None or len(df) < 70:
        s.warnings.append("历史数据不足70根")
        return s

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    vol = df["Volume"]

    price = float(close.iloc[-1])
    ma20 = _sma(close, 20).iloc[-1]
    ma50 = _sma(close, 50).iloc[-1]
    ma150 = _sma(close, 150).iloc[-1] if len(close) >= 150 else np.nan
    ma200 = _sma(close, 200).iloc[-1] if len(close) >= 200 else np.nan
    ma50_prev = _sma(close, 50).iloc[-6] if len(close) >= 55 else ma50

    # 52W 高
    win = close.tail(252) if len(close) >= 252 else close
    high_52w = float(win.max())
    dist_high = _pct(price, high_52w)  # 负数表示在高点下方

    # 量能
    vol20 = vol.tail(20).mean()
    vol_ratio_today = float(vol.iloc[-1] / vol20) if vol20 else 1.0
    vol_ratio_3d = float(vol.tail(3).mean() / vol20) if vol20 else 1.0

    s.details = {
        "price": round(price, 2),
        "dist_52w_high_pct": round(dist_high, 2),
        "vol_ratio_today": round(vol_ratio_today, 2),
        "vol_ratio_3d": round(vol_ratio_3d, 2),
        "rs_6m_pct": round(rs_6m * 100, 1),
    }

    # ------- 硬门槛 -------
    gates_ok = True
    if dist_high < -5:
        s.warnings.append(f"距52W高 {dist_high:.1f}% (需 ≥-5%)")
        gates_ok = False
    if not (vol_ratio_today >= 1.3 or vol_ratio_3d >= 1.3):
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

    # 30 分：突破确认 —— 近3日创 60日新高 且放量
    high_60 = float(close.tail(60).max())
    # 取近 3 日最高收盘（即突破点）
    break_price = float(close.tail(3).max())
    if break_price >= high_60 * 0.998 and vol_ratio_3d >= 1.3:
        score += 30
        s.reasons.append(f"突破60日新高+放量 {vol_ratio_3d:.2f}x")
    elif price >= high_60 * 0.98:
        score += 15
        s.reasons.append(f"接近60日新高 ({_pct(price, high_60):+.1f}%)")

    # 25 分：箱体收敛 → 爆发（前 40 日振幅 vs 后 10 日振幅）
    if len(close) >= 50:
        pre_range = float(close.iloc[-50:-10].max() - close.iloc[-50:-10].min()) / close.iloc[-30]
        recent_move = (price - float(close.iloc[-11])) / close.iloc[-11]
        if pre_range < 0.20 and recent_move > 0.05:
            score += 25
            s.reasons.append(f"箱体(前40日振幅{pre_range:.1%})后爆发 +{recent_move:.1%}")
        elif pre_range < 0.30 and recent_move > 0.03:
            score += 15
            s.reasons.append("温和收敛后启动")

    # 15 分：相对强度
    if rs_6m >= 0.20:
        score += 15
        s.reasons.append(f"RS 跑赢 SPY {rs_6m*100:+.1f}%")
    elif rs_6m >= 0.05:
        score += 8
    elif rs_6m < -0.05:
        s.warnings.append(f"RS 落后 SPY {rs_6m*100:+.1f}%")

    # 15 分：多头排列强度
    aligned = 0
    if pd.notna(ma20) and pd.notna(ma50) and ma20 > ma50:
        aligned += 1
    if pd.notna(ma50) and pd.notna(ma150) and ma50 > ma150:
        aligned += 1
    if pd.notna(ma150) and pd.notna(ma200) and ma150 > ma200:
        aligned += 1
    if pd.notna(ma50) and ma50 > ma50_prev:
        aligned += 1
    score += aligned * 3.75  # 最多 15
    if aligned >= 3:
        s.reasons.append(f"多头排列强 ({aligned}/4)")

    # 10 分：成交量质量（放量 + 不是长上影出货）
    last = df.iloc[-1]
    body = abs(last["Close"] - last["Open"])
    upper_wick = last["High"] - max(last["Close"], last["Open"])
    if vol_ratio_today >= 1.5:
        if body > 0 and upper_wick < body * 1.5:
            score += 10
            s.reasons.append(f"放量 {vol_ratio_today:.2f}x 实体强")
        else:
            score += 3
            s.warnings.append("放量但长上影（警惕出货）")
    elif vol_ratio_today >= 1.2:
        score += 6

    # 5 分：越近新高越好
    if dist_high >= -1:
        score += 5
    elif dist_high >= -3:
        score += 3

    s.score = round(min(100.0, score), 1)
    s.grade = _grade(s.score)
    return s


# ========================================================================
# 2. TREND —— Minervini 强势趋势
# ========================================================================

def score_trend(df: pd.DataFrame, info: dict, rs_6m: float) -> StrategyScore:
    """
    🎯 趋势池：强者恒强（Mark Minervini 模板）

    硬门槛（任一不满足 score=0）：
      • 价 > MA50 > MA150 > MA200（严格多头排列）
      • MA200 最近 20 天向上
      • 距 52W 低 ≥ 30%
      • 距 52W 高 ≤ 25%（不在半山腰）

    评分：
      30  Minervini 2-Stage 模板完美度
      25  RS 强度（跑赢 SPY）
      20  均线发散度（MA50 - MA200 越大越好，但要<30%防止过热）
      15  距52W 低位置（越远越好，但上限 60%）
      10  基本面增长
    """
    s = StrategyScore(name="trend", label=STRATEGY_LABELS["trend"])
    if df is None or len(df) < 210:
        s.warnings.append("历史数据不足210根（无法算 MA200 趋势）")
        return s

    close = df["Close"]
    price = float(close.iloc[-1])

    ma50 = _sma(close, 50).iloc[-1]
    ma150 = _sma(close, 150).iloc[-1]
    ma200_series = _sma(close, 200)
    ma200 = ma200_series.iloc[-1]
    ma200_20ago = ma200_series.iloc[-21] if len(ma200_series) >= 21 else ma200

    win = close.tail(252) if len(close) >= 252 else close
    high_52w = float(win.max())
    low_52w = float(win.min())
    dist_high = _pct(price, high_52w)
    dist_low = _pct(price, low_52w)

    s.details = {
        "price": round(price, 2),
        "ma50": round(float(ma50), 2) if pd.notna(ma50) else 0,
        "ma150": round(float(ma150), 2) if pd.notna(ma150) else 0,
        "ma200": round(float(ma200), 2) if pd.notna(ma200) else 0,
        "dist_52w_high_pct": round(dist_high, 2),
        "dist_52w_low_pct": round(dist_low, 2),
        "rs_6m_pct": round(rs_6m * 100, 1),
    }

    # 硬门槛
    gates_ok = True
    if not (pd.notna(ma50) and pd.notna(ma150) and pd.notna(ma200)
            and price > ma50 > ma150 > ma200):
        s.warnings.append("未形成严格多头排列 价>MA50>MA150>MA200")
        gates_ok = False
    if pd.notna(ma200) and pd.notna(ma200_20ago) and ma200 <= ma200_20ago:
        s.warnings.append("MA200 方向非向上")
        gates_ok = False
    if dist_low < 30:
        s.warnings.append(f"距52W低仅 {dist_low:.1f}% (需 ≥30%)")
        gates_ok = False
    if dist_high < -25:
        s.warnings.append(f"距52W高 {dist_high:.1f}% (需 ≥-25%，不在半山腰)")
        gates_ok = False

    s.passed_gates = gates_ok
    if not gates_ok:
        return s

    score = 0.0

    # 30 分：模板完美度（多头排列已过门槛，这里评分"发散度+一致性"）
    ma20 = _sma(close, 20).iloc[-1]
    template = 0
    if pd.notna(ma20) and ma20 > ma50: template += 1  # 短期也在长期之上
    if price > ma50 * 1.02: template += 1             # 价远离MA50
    if ma50 > ma150 * 1.03: template += 1             # MA50 远离 MA150
    if ma150 > ma200 * 1.02: template += 1            # MA150 远离 MA200
    if ma200 > ma200_20ago * 1.01: template += 1      # MA200 明确向上
    score += template * 6
    if template >= 4:
        s.reasons.append(f"Minervini模板强度 {template}/5")

    # 25 分：RS 跑赢 SPY
    if rs_6m >= 0.30:
        score += 25
        s.reasons.append(f"RS +{rs_6m*100:.1f}% (强势)")
    elif rs_6m >= 0.15:
        score += 18
    elif rs_6m >= 0.05:
        score += 10
    else:
        s.warnings.append(f"RS 仅 {rs_6m*100:+.1f}%")

    # 20 分：均线发散度 —— MA50/MA200 比值
    spread = (ma50 - ma200) / ma200
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

    # 15 分：距低位置（越高越好，但超过 100% 反而可能接近顶）
    if 40 <= dist_low <= 80:
        score += 15
        s.reasons.append(f"距52W低 +{dist_low:.0f}% (黄金位)")
    elif 30 <= dist_low < 40:
        score += 10
    elif 80 < dist_low <= 150:
        score += 10
    else:
        score += 5

    # 10 分：基本面增长
    rg = info.get("revenueGrowth")
    eg = info.get("earningsGrowth")
    vals = [v for v in (rg, eg) if v is not None]
    if vals:
        avg = sum(vals) / len(vals)
        if avg >= 0.20:
            score += 10
            s.reasons.append(f"营收/EPS增速 +{avg*100:.0f}%")
        elif avg >= 0.10:
            score += 7
        elif avg >= 0:
            score += 3
    else:
        score += 4  # 拿不到数据给中性

    s.score = round(min(100.0, score), 1)
    s.grade = _grade(s.score)
    return s


# ========================================================================
# 3. PULLBACK —— 强势股回踩不破
# ========================================================================

def score_pullback(df: pd.DataFrame, info: dict, rs_6m: float) -> StrategyScore:
    """
    🔄 回踩池：强势股回踩到 MA20/MA50 且未破位

    硬门槛：
      • 50日内曾创 60日新高（证明它是强势股）
      • 当前价在 MA50 之上（未跌破重要均线）
      • RSI 在 35-60 之间（回踩但未超卖）
      • 回踩期间缩量（volume shrink）

    评分：
      30  回踩深度合理（距近期高 5%~15%，在 MA20/MA50 附近）
      25  相对强度 RS 仍强
      20  缩量回踩（回踩的量能 < 前涨势的量能）
      15  大趋势未破（MA50 仍向上）
      10  当日见底信号（小锤线 / 日线收在 MA20 之上）
    """
    s = StrategyScore(name="pullback", label=STRATEGY_LABELS["pullback"])
    if df is None or len(df) < 70:
        s.warnings.append("历史数据不足70根")
        return s

    close = df["Close"]
    vol = df["Volume"]
    price = float(close.iloc[-1])

    ma20 = _sma(close, 20).iloc[-1]
    ma50_series = _sma(close, 50)
    ma50 = ma50_series.iloc[-1]
    ma50_10ago = ma50_series.iloc[-11] if len(ma50_series) >= 11 else ma50

    # 50 日内的最高收盘
    high_50 = float(close.tail(50).max())
    idx_high = close.tail(50).idxmax()
    try:
        days_since_high = len(close) - 1 - close.index.get_loc(idx_high)
    except Exception:
        days_since_high = 0

    dist_from_high_pct = _pct(price, high_50)   # 负数=已回踩
    rsi = _rsi(close, 14)

    # 60 日新高确认（强势股证明）
    high_60 = float(close.tail(60).max())
    made_60d_high_recently = days_since_high <= 50 and high_50 >= high_60 * 0.98

    # 缩量
    vol_during_pullback = float(vol.iloc[-min(days_since_high, 10):].mean()) if days_since_high > 0 else float(vol.tail(5).mean())
    vol_before_high = float(vol.iloc[-max(days_since_high, 5) - 10:-max(days_since_high, 5)].mean()) if days_since_high >= 5 and len(vol) >= days_since_high + 10 else float(vol.tail(20).mean())
    vol_shrink_ratio = vol_during_pullback / vol_before_high if vol_before_high else 1.0

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

    # 硬门槛
    gates_ok = True
    if not made_60d_high_recently:
        s.warnings.append("50日内未创60日新高（非强势股）")
        gates_ok = False
    if pd.isna(ma50) or price < ma50 * 0.97:   # 允许下插 3%
        s.warnings.append("价跌破 MA50*0.97")
        gates_ok = False
    if not (35 <= rsi <= 60):
        s.warnings.append(f"RSI {rsi:.0f} 超出 35-60 区间")
        gates_ok = False
    if vol_shrink_ratio >= 1.1:
        s.warnings.append(f"回踩未缩量 (量能比 {vol_shrink_ratio:.2f})")
        gates_ok = False

    s.passed_gates = gates_ok
    if not gates_ok:
        return s

    score = 0.0

    # 30 分：回踩深度 + 位置
    dist_to_ma20 = abs(_pct(price, float(ma20))) if pd.notna(ma20) else 99
    dist_to_ma50 = abs(_pct(price, float(ma50)))
    if -15 <= dist_from_high_pct <= -5 and (dist_to_ma20 <= 3 or dist_to_ma50 <= 3):
        score += 30
        s.reasons.append(f"回踩{dist_from_high_pct:.1f}% 至均线附近")
    elif -20 <= dist_from_high_pct <= -3:
        score += 18
        s.reasons.append(f"温和回踩{dist_from_high_pct:.1f}%")

    # 25 分：RS
    if rs_6m >= 0.20:
        score += 25
        s.reasons.append(f"RS 仍强 +{rs_6m*100:.1f}%")
    elif rs_6m >= 0.08:
        score += 15
    elif rs_6m >= 0:
        score += 8

    # 20 分：缩量
    if vol_shrink_ratio <= 0.7:
        score += 20
        s.reasons.append(f"显著缩量 {vol_shrink_ratio:.2f}x")
    elif vol_shrink_ratio <= 0.85:
        score += 14
        s.reasons.append(f"缩量 {vol_shrink_ratio:.2f}x")
    elif vol_shrink_ratio <= 1.0:
        score += 8

    # 15 分：MA50 仍向上
    if pd.notna(ma50) and pd.notna(ma50_10ago) and ma50 > ma50_10ago * 1.005:
        score += 15
        s.reasons.append("MA50 趋势完好")
    elif pd.notna(ma50) and pd.notna(ma50_10ago) and ma50 >= ma50_10ago:
        score += 8

    # 10 分：见底信号
    last = df.iloc[-1]
    body = last["Close"] - last["Open"]
    lower_wick = min(last["Open"], last["Close"]) - last["Low"]
    if body > 0 and pd.notna(ma20) and price > ma20:
        score += 10
        s.reasons.append("阳线收回 MA20 之上")
    elif lower_wick > abs(body) * 1.5 and lower_wick > 0:
        score += 7
        s.reasons.append("长下影见底信号")

    s.score = round(min(100.0, score), 1)
    s.grade = _grade(s.score)
    return s


# ========================================================================
# 4. REVERSAL —— 底部放量确认反转
# ========================================================================

def score_reversal(df: pd.DataFrame, info: dict, rs_6m: float) -> StrategyScore:
    """
    🔥 反转池：深跌后底部放量确认反转（赔率高但胜率低）

    硬门槛：
      • 距 52W 高 ≤ -25%（确实跌深了）
      • 近 20 日出现过放量突破（vol>1.5x + 红K）
      • 当前价 >= MA20（短期均线翻红）
      • 近 30 日出现 W 底/双底 或 MA20 上穿 MA50

    评分：
      30  反转结构确认（W底 / MA20上穿MA50 / 突破下降趋势线）
      25  放量确认强度
      15  MA50 开始走平或上翘（底部迹象）
      15  RSI 从 <30 区回升至 40-60
      10  估值修复空间（fwdPE 低）
       5  距底部涨幅已有 10-30%（最佳介入区间）
    """
    s = StrategyScore(name="reversal", label=STRATEGY_LABELS["reversal"])
    if df is None or len(df) < 70:
        s.warnings.append("历史数据不足70根")
        return s

    close = df["Close"]
    vol = df["Volume"]
    price = float(close.iloc[-1])

    ma20_series = _sma(close, 20)
    ma50_series = _sma(close, 50)
    ma20 = ma20_series.iloc[-1]
    ma50 = ma50_series.iloc[-1]
    ma50_20ago = ma50_series.iloc[-21] if len(ma50_series) >= 21 else ma50

    win = close.tail(252) if len(close) >= 252 else close
    high_52w = float(win.max())
    low_52w = float(win.min())
    dist_high = _pct(price, high_52w)
    dist_low = _pct(price, low_52w)

    vol20 = vol.tail(20).mean()
    # 近20日最大量能比
    max_vol_ratio_20d = float((vol.tail(20) / vol20).max()) if vol20 else 1.0

    rsi = _rsi(close, 14)
    rsi_min_30d = min(
        _rsi(close.iloc[:-i], 14) if i > 0 else rsi
        for i in range(0, min(30, len(close) - 15), 5)
    )

    # MA20 上穿 MA50 检测（近 20 日）
    ma20_cross_ma50 = False
    if len(ma20_series) >= 21 and len(ma50_series) >= 21:
        for i in range(-20, 0):
            if (pd.notna(ma20_series.iloc[i-1]) and pd.notna(ma50_series.iloc[i-1])
                and pd.notna(ma20_series.iloc[i]) and pd.notna(ma50_series.iloc[i])):
                if ma20_series.iloc[i-1] <= ma50_series.iloc[i-1] and ma20_series.iloc[i] > ma50_series.iloc[i]:
                    ma20_cross_ma50 = True
                    break

    s.details = {
        "price": round(price, 2),
        "dist_52w_high_pct": round(dist_high, 2),
        "dist_52w_low_pct": round(dist_low, 2),
        "max_vol_ratio_20d": round(max_vol_ratio_20d, 2),
        "rsi_14": round(rsi, 1),
        "rsi_min_30d": round(rsi_min_30d, 1),
        "ma20_cross_ma50_recent": ma20_cross_ma50,
    }

    # 硬门槛
    gates_ok = True
    if dist_high > -25:
        s.warnings.append(f"距52W高仅 {dist_high:.1f}% (未深跌)")
        gates_ok = False
    if max_vol_ratio_20d < 1.5:
        s.warnings.append(f"近20日无放量突破 ({max_vol_ratio_20d:.2f}x)")
        gates_ok = False
    if pd.isna(ma20) or price < ma20:
        s.warnings.append("价 < MA20")
        gates_ok = False

    s.passed_gates = gates_ok
    if not gates_ok:
        return s

    score = 0.0

    # 30 分：反转结构
    if ma20_cross_ma50:
        score += 30
        s.reasons.append("MA20 上穿 MA50（金叉）")
    elif pd.notna(ma20) and pd.notna(ma50) and ma20 > ma50 * 0.98:
        score += 18
        s.reasons.append("MA20 接近 MA50（将穿）")
    else:
        score += 8

    # 25 分：放量确认
    if max_vol_ratio_20d >= 2.5:
        score += 25
        s.reasons.append(f"强放量 {max_vol_ratio_20d:.2f}x")
    elif max_vol_ratio_20d >= 2.0:
        score += 18
    elif max_vol_ratio_20d >= 1.5:
        score += 12

    # 15 分：MA50 走平/上翘
    if pd.notna(ma50) and pd.notna(ma50_20ago):
        ma50_slope = (ma50 - ma50_20ago) / ma50_20ago
        if ma50_slope > 0.01:
            score += 15
            s.reasons.append("MA50 已上翘")
        elif ma50_slope > -0.01:
            score += 10
            s.reasons.append("MA50 走平")
        else:
            score += 3

    # 15 分：RSI 从超卖回升
    if rsi_min_30d < 30 and 40 <= rsi <= 60:
        score += 15
        s.reasons.append(f"RSI 从{rsi_min_30d:.0f}回升至{rsi:.0f}")
    elif rsi_min_30d < 35 and 40 <= rsi <= 65:
        score += 10
    elif 40 <= rsi <= 65:
        score += 5

    # 10 分：估值
    fwd_pe = info.get("forwardPE")
    if fwd_pe and 0 < fwd_pe < 15:
        score += 10
        s.reasons.append(f"FwdPE {fwd_pe:.1f} 便宜")
    elif fwd_pe and fwd_pe < 25:
        score += 6
    elif fwd_pe and fwd_pe < 40:
        score += 3

    # 5 分：距底部涨幅 10-30% 区间
    if 10 <= dist_low <= 30:
        score += 5
        s.reasons.append(f"距底 +{dist_low:.0f}% (最佳介入)")
    elif 30 < dist_low <= 50:
        score += 3
    elif dist_low > 50:
        s.warnings.append(f"距底 +{dist_low:.0f}% 反弹过多")

    s.score = round(min(100.0, score), 1)
    s.grade = _grade(s.score)
    return s


# ========================================================================
# 统一入口：对一只票跑全部策略池
# ========================================================================

STRATEGY_FUNCS = {
    "breakout": score_breakout,
    "trend":    score_trend,
    "pullback": score_pullback,
    "reversal": score_reversal,
}


def score_all_strategies(df: pd.DataFrame, info: dict, rs_6m: float) -> Dict[str, StrategyScore]:
    """
    对一只票跑全部 4 个策略池，返回 {name: StrategyScore}
    """
    return {name: fn(df, info, rs_6m) for name, fn in STRATEGY_FUNCS.items()}


def best_strategy(scores: Dict[str, StrategyScore]) -> Optional[StrategyScore]:
    """
    取得分最高的策略池；如果所有池都未通过门槛，返回 None
    """
    passed = [s for s in scores.values() if s.passed_gates and s.score > 0]
    if not passed:
        return None
    return max(passed, key=lambda s: s.score)
