# -*- coding: utf-8 -*-
"""
统一筹码选股器 v3.0 — chip_screener_v3.py
==========================================
Merges v1.3 (static chip snapshot) and v2.0 (dynamic chip tracking)
into a single unified screener.

Pipeline:
  Step 0: Market stage check (Weinstein MA200 switch)
  Step 1: Resolve stock pool → ts_codes
  Step 2: Load daily data + basic pre-filter (halted/limit/ST)
  Step 3: Load/update local cache
  Step 4: Layer 1 — Volume Behavior Scan (entry gate)
          Find "quiet zone → gentle recovery" pattern
  Step 5: Layer 2 — Dynamic Chip Convergence
          20-day chip snapshots → concentration trend + center drift
          Anti-fake checks (rally-driven, vol-price divergence, range position, distribution candles)
  Step 6: Layer 3 — Breakout Readiness + Scoring
          Winner ratio position, price vs chip peak, MA alignment
  Step 7: Moneyflow reference (not a hard filter)
  Step 8: Output + save to txt

Scoring: 100 pts base + bonus/penalty
  - Volume behavior:           20 pts
  - Concentration convergence: 15 pts  (降权: 易被对倒干扰)
  - Center drift:              30 pts  (提权: 几乎不受对倒影响)
  - Winner ratio position:     20 pts  (提权: 由价格决定, 抗干扰)
  - Price vs chip peak:        15 pts
  - VCP volatility contraction: +10 bonus
  - MA alignment:              +15 / -10 bonus/penalty
  - Dynamic precision:         +15 bonus (center monotonic +5, conc from high +5, winner 55-65% +5)

Public modules in chip_common.py:
  - Cache system, name resolution, market stage, chip distribution,
    moneyflow filter, API helpers, TeeWriter, trade date utilities

Author: 龙虾 x 老哥
Date: 2026-04-19
"""
import sys
import io
import os
import json
import numpy as np
import pandas as pd
import tushare as ts
from datetime import datetime, timedelta
from scipy import stats as scipy_stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import TUSHARE_TOKEN, STOCK_POOL

from chip_common import (
    # Cache
    load_cache, save_cache, update_cache_incremental,
    # Name resolution
    build_name_to_code_map, resolve_pool_to_codes,
    # Market stage
    get_market_stage, print_market_stage,
    # Chip core
    compute_chip_distribution, count_chip_peaks,
    # Moneyflow
    filter_by_moneyflow,
    # API / IO
    api_call_with_retry, TeeWriter,
    find_latest_trade_date, save_results_txt,
)

from a_strategies import (
    score_all_strategies, best_strategy, format_strategy_summary,
    compute_rs_vs_index, A_STRATEGY_CONFIG, STRATEGY_LABELS,
)


# ============================================================
# v3.0 Unified Config
# ============================================================
V3_CONFIG = {
    # --- Chip calculation ---
    'history_days': 250,
    'price_bins': 100,
    'decay_factor': 0.97,
    'peak_prominence_ratio': 0.3,

    # --- Layer 1: Volume Behavior ---
    'vol_baseline_days': 60,
    'vol_quiet_zone_days': 25,
    'vol_quiet_threshold': 0.50,       # 50% of 60d avg = ground volume threshold
    'vol_quiet_min_streak': 5,          # ← tightened: at least 5 consecutive days (was 3)
    'vol_recovery_window': 5,
    'vol_recovery_min': 0.60,           # recovery must be >= 60% of baseline
    'vol_recovery_max': 1.50,
    'vol_recovery_min_active_days': 3,      # at least N of 5 days > 40% baseline
    'vol_recovery_active_threshold': 0.40,  # threshold for "active day"

    # --- Layer 2: Chip Convergence ---
    'chip_snapshot_days': 20,
    'chip_snapshot_days_slow': 40,           # slow mode for 30-60 day accumulation cycles
    'chip_snapshot_interval': 5,
    'chip_snapshot_interval_slow': 10,       # slow mode interval
    'min_concentration_contraction': -0.015,
    'min_center_drift': 0.005,

    # --- Layer 2: Anti-fake ---
    'max_rally_during_convergence': 0.25,    # relaxed: 25% (was 15%, too strict in strong markets)
    'vol_fade_ratio': 0.55,                  # relaxed: 55% (was 70%, mild volume fade is normal in washing)
    'min_rally_for_vol_check': 0.10,
    'max_range_position': 0.80,          # relaxed: 80% (was 65%, washing at mid-high range is normal)
    'range_pullback_exemption_rally': 0.05,  # rally < 5% at high range = pullback, demote not kill
    'range_lookback_days': 60,
    'min_distribution_candles': 2,

    # --- VCP (Volatility Contraction Pattern) ---
    'vcp_lookback_days': 40,
    'vcp_window_size': 10,
    'vcp_min_contractions': 2,
    'vcp_contraction_ratio': 0.70,
    'vcp_max_bonus': 10,

    # --- Layer 3: Breakout Readiness ---
    'winner_sweet_low': 0.35,             # relaxed: 35% (was 50%, too strict for bottom washing)
    'winner_sweet_high': 0.80,            # relaxed: 80% (was 70%)
    'winner_high_penalty': 0.82,
    'max_drop_from_high': 0.35,           # relaxed: 35% (was 20%, allow deeper pullback bottoms)
    'alt_range_position_low': 0.55,         # OR branch: 60d range position < 55% also passes
    'max_peaks': 5,

    # --- Market cap / turnover ---
    'min_market_cap_yi': 50,
    'max_market_cap_yi': 2000,
    'min_turnover': 0.3,

    # --- Debug ---
    'debug_layer2': True,
}

# ============================================================
# Breakout Confirmation Config (Layer 4)
# ============================================================
BREAKOUT_CONFIG = {
    'signal_pool_max_age_days': 4,   # Signal expires after 4 calendar days
    'min_pct_chg_breakout': 1.0,     # Breakout candle: min +1.0% (strong yang line)
    'min_vol_ratio_breakout': 1.3,   # Breakout candle: vol > 1.3x of 5d avg
    'vol_avg_days': 5,               # Volume average lookback
}

SIGNAL_POOL_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'cache', 'chip_signal_pool.json'
)

# Breakout archive: records stocks that have had a confirmed breakout.
# Keeps the most recent N days so downstream tools (e.g. quick_check.py)
# can still surface today's breakouts after the pool has been pruned.
BREAKOUT_ARCHIVE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'cache', 'chip_breakout_history.json'
)
BREAKOUT_ARCHIVE_RETAIN_DAYS = 30


def _load_signal_pool():
    """Load pending signal pool from JSON file."""
    if os.path.exists(SIGNAL_POOL_FILE):
        try:
            with open(SIGNAL_POOL_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_signal_pool(pool):
    """Save pending signal pool to JSON file."""
    os.makedirs(os.path.dirname(SIGNAL_POOL_FILE), exist_ok=True)
    with open(SIGNAL_POOL_FILE, 'w', encoding='utf-8') as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


def _load_breakout_archive():
    """Load confirmed breakout history from JSON file."""
    if os.path.exists(BREAKOUT_ARCHIVE_FILE):
        try:
            with open(BREAKOUT_ARCHIVE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_breakout_archive(archive):
    """Save confirmed breakout history to JSON file."""
    os.makedirs(os.path.dirname(BREAKOUT_ARCHIVE_FILE), exist_ok=True)
    with open(BREAKOUT_ARCHIVE_FILE, 'w', encoding='utf-8') as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)


def _append_breakout_archive(today_breakouts, trade_date):
    """
    Merge today's confirmed breakouts into the archive.
    - Deduplicate by (ts_code, breakout_date): re-runs of the same day overwrite.
    - Drop entries older than BREAKOUT_ARCHIVE_RETAIN_DAYS.
    """
    if not today_breakouts:
        # Still prune old entries so the archive doesn't grow forever.
        existing = _load_breakout_archive()
        pruned = _prune_breakout_archive(existing, trade_date)
        if len(pruned) != len(existing):
            _save_breakout_archive(pruned)
        return

    existing = _load_breakout_archive()
    # Remove any prior record for today's (ts_code, breakout_date) — we'll replace them.
    today_keys = {(e['ts_code'], e.get('breakout_date')) for e in today_breakouts}
    merged = [e for e in existing
              if (e.get('ts_code'), e.get('breakout_date')) not in today_keys]
    merged.extend(today_breakouts)
    merged = _prune_breakout_archive(merged, trade_date)
    _save_breakout_archive(merged)


def _prune_breakout_archive(archive, trade_date):
    """Keep only entries whose breakout_date is within retain window."""
    try:
        ref_dt = datetime.strptime(trade_date, '%Y%m%d')
    except Exception:
        return archive
    kept = []
    for e in archive:
        try:
            bdt = datetime.strptime(e.get('breakout_date', ''), '%Y%m%d')
            if (ref_dt - bdt).days <= BREAKOUT_ARCHIVE_RETAIN_DAYS:
                kept.append(e)
        except Exception:
            # Keep malformed entries rather than silently lose data.
            kept.append(e)
    return kept


def _check_breakout_today(ts_code, history_data, trade_date, pro, bcfg):
    """
    Check if a stock has a breakout candle on trade_date.
    Returns (is_breakout, details_dict) tuple.

    Breakout = yang line (+1% vs prev close) + volume > 1.3x of 5d avg
    """
    hist = history_data.get(ts_code)
    if hist is None or hist.empty:
        return False, {}

    hist = hist.sort_values('trade_date').copy()
    for col in ['open', 'high', 'low', 'close', 'vol']:
        hist[col] = pd.to_numeric(hist[col], errors='coerce')
    hist = hist.dropna(subset=['close', 'vol'])

    # Find today's row
    td_str = trade_date if isinstance(trade_date, str) else trade_date.strftime('%Y%m%d')
    today_mask = hist['trade_date'] == td_str
    if not today_mask.any():
        return False, {}

    today_idx = hist.index[today_mask][-1]
    today_row = hist.loc[today_idx]
    today_close = float(today_row['close'])
    today_vol = float(today_row['vol'])

    # Previous close for pct_chg calculation
    row_pos = hist.index.get_loc(today_idx)
    if row_pos < 1:
        return False, {}
    prev_row = hist.iloc[row_pos - 1]
    prev_close = float(prev_row['close'])
    if prev_close <= 0:
        return False, {}

    pct_chg = (today_close - prev_close) / prev_close * 100

    # Volume ratio: today vs 5d average
    vol_avg_days = bcfg.get('vol_avg_days', 5)
    if row_pos < vol_avg_days:
        return False, {}
    vol_5d = float(hist.iloc[row_pos - vol_avg_days:row_pos]['vol'].mean())
    vol_ratio = today_vol / vol_5d if vol_5d > 0 else 0

    is_breakout = (
        pct_chg >= bcfg['min_pct_chg_breakout'] and
        vol_ratio >= bcfg['min_vol_ratio_breakout']
    )

    details = {
        'pct_chg': round(pct_chg, 2),
        'vol_ratio': round(vol_ratio, 2),
        'close': round(today_close, 2),
    }
    return is_breakout, details


# ============================================================
# Layer 1: Volume Behavior Scan
# ============================================================
def scan_volume_behavior(hist_df, cfg):
    """
    Detect "waking up from dead volume" pattern:
    1. 60-day average volume as baseline
    2. Quiet zone (3+ consecutive days < 50% of baseline) in last 25 days
    3. Recent 5 days show gentle recovery (55%-150% of baseline)

    Returns dict or None if pattern not found.
    """
    if hist_df is None or len(hist_df) < cfg['vol_baseline_days']:
        return None

    hist = hist_df.sort_values('trade_date').copy()
    vols = hist['vol'].values.astype(float)

    if len(vols) < cfg['vol_baseline_days']:
        return None

    baseline = np.mean(vols[-cfg['vol_baseline_days']:])
    if baseline <= 0:
        return None

    lookback = min(cfg['vol_quiet_zone_days'], len(vols) - 5)
    if lookback < cfg['vol_quiet_min_streak']:
        return None

    quiet_threshold = baseline * cfg['vol_quiet_threshold']
    scan_region = vols[-(lookback + 5):-5]

    max_streak = 0
    current_streak = 0
    for v in scan_region:
        if v < quiet_threshold:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    if max_streak < cfg['vol_quiet_min_streak']:
        return None

    recent_5d = vols[-cfg['vol_recovery_window']:]
    avg_recent = np.mean(recent_5d)
    recovery_ratio = avg_recent / baseline

    if recovery_ratio < cfg['vol_recovery_min'] or recovery_ratio > cfg['vol_recovery_max']:
        return None

    # Anti single-day spike: at least N of 5 days must individually exceed active threshold
    active_threshold = baseline * cfg.get('vol_recovery_active_threshold', 0.40)
    min_active = cfg.get('vol_recovery_min_active_days', 3)
    active_days = int(np.sum(recent_5d > active_threshold))
    if active_days < min_active:
        return None

    vol_cv = np.std(recent_5d) / np.mean(recent_5d) if np.mean(recent_5d) > 0 else 999
    rhythm_good = vol_cv < 0.6

    return {
        'baseline_vol': round(baseline, 0),
        'quiet_streak': max_streak,
        'recovery_ratio': round(recovery_ratio, 3),
        'active_days': active_days,
        'vol_cv': round(vol_cv, 3),
        'rhythm_good': rhythm_good,
    }


# ============================================================
# Layer 2: Chip Convergence Confirmation
# ============================================================
def _compute_snapshots_for_window(hist, cfg, max_offset, interval):
    """Internal: compute chip snapshots for a given window/interval."""
    total_rows = len(hist)
    offsets = list(range(0, max_offset + 1, interval))

    snapshots = []
    for offset in offsets:
        if offset == 0:
            sub_hist = hist.tail(cfg['history_days'])
            price_at = float(hist.iloc[-1]['close'])
        else:
            if total_rows <= offset:
                continue
            sub_hist = hist.iloc[:total_rows - offset].tail(cfg['history_days'])
            price_at = float(hist.iloc[total_rows - offset - 1]['close'])

        if len(sub_hist) < 60:
            continue

        chip = compute_chip_distribution(sub_hist, price_at, cfg)
        if chip is None:
            continue

        snapshots.append({
            'day_offset': offset,
            'concentration_90': chip['concentration_90'],
            'median_cost': chip['median_cost'],
            'winner_ratio': chip['winner_ratio'],
            'peak_count': chip['peak_count'],
        })

    return snapshots if len(snapshots) >= 3 else None


def compute_chip_snapshots(hist_df, cfg):
    """
    Compute chip distribution snapshots in two parallel windows:
      - Fast: 20-day window, 5-day interval  → [0, 5, 10, 15, 20]
      - Slow: 40-day window, 10-day interval → [0, 10, 20, 30, 40]
    Returns (fast_snapshots, slow_snapshots). Either may be None.
    """
    min_required = max(60, cfg['history_days'] // 2)
    if hist_df is None or len(hist_df) < min_required:
        return None, None

    hist = hist_df.sort_values('trade_date').copy()

    fast = _compute_snapshots_for_window(
        hist, cfg,
        max_offset=cfg['chip_snapshot_days'],
        interval=cfg['chip_snapshot_interval'],
    )
    slow = _compute_snapshots_for_window(
        hist, cfg,
        max_offset=cfg.get('chip_snapshot_days_slow', 40),
        interval=cfg.get('chip_snapshot_interval_slow', 10),
    )

    return fast, slow


def analyze_chip_dynamics(snapshots, cfg):
    """
    Analyze chip convergence from snapshots:
    1. Concentration trend (converging / flat / diverging)
    2. Center drift (up / flat / down)
    3. Winner velocity
    4. Combined signal (ACCUMULATING / WASHING / TRAPPED / DEAD / DISTRIBUTING)
    """
    if not snapshots or len(snapshots) < 3:
        return None

    snaps = sorted(snapshots, key=lambda x: x['day_offset'], reverse=True)

    conc_series = [s['concentration_90'] for s in snaps]
    median_series = [s['median_cost'] for s in snaps]
    winner_series = [s['winner_ratio'] for s in snaps]

    conc_oldest = conc_series[0]
    conc_newest = conc_series[-1]
    conc_change = conc_newest - conc_oldest

    x_vals = np.array(range(len(conc_series)), dtype=float)
    conc_slope, _, conc_r, _, _ = scipy_stats.linregress(x_vals, conc_series)

    is_converging = conc_change < cfg['min_concentration_contraction']
    is_mildly_converging = (not is_converging) and (conc_change < -0.005)
    conc_trend = 'converging' if is_converging else (
        'mild_converging' if is_mildly_converging else (
            'diverging' if conc_change > 0.02 else 'flat'
        )
    )

    median_oldest = median_series[0]
    median_newest = median_series[-1]
    center_drift_pct = (median_newest - median_oldest) / median_oldest if median_oldest > 0 else 0

    median_slope, _, median_r, _, _ = scipy_stats.linregress(x_vals, median_series)

    drift_direction = 'up' if center_drift_pct > cfg['min_center_drift'] else (
        'down' if center_drift_pct < -cfg['min_center_drift'] else 'flat'
    )

    winner_oldest = winner_series[0]
    winner_newest = winner_series[-1]
    winner_change = winner_newest - winner_oldest
    n_intervals = len(winner_series) - 1
    winner_velocity = winner_change / n_intervals if n_intervals > 0 else 0

    # --- Dynamic Precision signals (v3.0+) ---
    # 1. Center mostly up: median_cost rises in ≥75% of intervals (relaxed from 100%)
    if len(median_series) >= 3:
        up_count = sum(1 for i in range(len(median_series) - 1) if median_series[i] < median_series[i + 1])
        total_intervals = len(median_series) - 1
        center_mostly_up = (up_count / total_intervals) >= 0.75
    else:
        center_mostly_up = False
    center_monotonic_up = center_mostly_up  # backward compat for dict key

    # 2. Concentration from high: was dispersed (>0.25) and now converging
    conc_from_high = (conc_oldest > 0.25) and (conc_change < -0.02)

    # 3. Winner stabilizing in 55-65% zone
    winner_in_sweet_zone = (0.55 <= winner_newest <= 0.65)

    any_converging = is_converging or is_mildly_converging

    if any_converging and drift_direction == 'up':
        signal, signal_cn = 'ACCUMULATING', '🔥 主力吸筹'
    elif any_converging and drift_direction == 'flat':
        signal, signal_cn = 'WASHING', '🟡 底部洗盘'
    elif any_converging and drift_direction == 'down':
        signal, signal_cn = 'TRAPPED', '⚠️ 套牢集中'
    elif conc_trend == 'flat' and drift_direction == 'flat':
        signal, signal_cn = 'DEAD', '💀 死水无量'
    elif conc_trend == 'diverging':
        signal, signal_cn = 'DISTRIBUTING', '🔴 筹码发散'
    else:
        signal, signal_cn = 'UNCLEAR', '❓ 信号不明'

    return {
        'conc_change': round(conc_change, 4),
        'conc_oldest': round(conc_oldest, 4),
        'conc_newest': round(conc_newest, 4),
        'conc_slope': round(conc_slope, 6),
        'conc_r2': round(conc_r ** 2, 3),
        'conc_trend': conc_trend,
        'center_drift_pct': round(center_drift_pct, 4),
        'median_oldest': round(median_oldest, 2),
        'median_newest': round(median_newest, 2),
        'median_slope': round(median_slope, 4),
        'median_r2': round(median_r ** 2, 3),
        'drift_direction': drift_direction,
        'winner_change': round(winner_change, 4),
        'winner_velocity': round(winner_velocity, 4),
        'winner_oldest': round(winner_oldest, 4),
        'winner_newest': round(winner_newest, 4),
        'center_monotonic_up': center_monotonic_up,
        'conc_from_high': conc_from_high,
        'winner_in_sweet_zone': winner_in_sweet_zone,
        'signal': signal,
        'signal_cn': signal_cn,
    }


def run_anti_fake_checks(hist, dynamics, cfg):
    """
    Run all anti-fake checks on a Layer 2 candidate.
    Returns (passed: bool, reason: str, extra_info: dict).
    """
    snap_days = cfg['chip_snapshot_days']
    hist_sorted = hist.sort_values('trade_date')

    # --- Price rally during convergence window ---
    price_rally_pct = 0.0
    try:
        if len(hist_sorted) > snap_days:
            p_old = float(hist_sorted.iloc[-snap_days - 1]['close'])
            p_new = float(hist_sorted.iloc[-1]['close'])
            if p_old > 0:
                price_rally_pct = (p_new - p_old) / p_old
    except Exception:
        pass

    max_rally = cfg.get('max_rally_during_convergence', 0.15)
    if price_rally_pct > max_rally:
        return False, f'⛔ 反弹集中({price_rally_pct*100:+.1f}%>{max_rally*100:.0f}%)', {'price_rally': price_rally_pct}
    if price_rally_pct < -0.15:
        return False, f'⛔ 暴跌集中({price_rally_pct*100:+.1f}%)', {'price_rally': price_rally_pct}

    # --- Volume-price divergence ---
    vol_fade = cfg.get('vol_fade_ratio', 0.70)
    rally_for_check = cfg.get('min_rally_for_vol_check', 0.10)
    try:
        if price_rally_pct > rally_for_check and len(hist_sorted) > snap_days:
            recent_n = hist_sorted.tail(snap_days)
            half = snap_days // 2
            vol_first = recent_n.head(half)['vol'].mean()
            vol_second = recent_n.tail(half)['vol'].mean()
            if vol_first > 0:
                vr = vol_second / vol_first
                if vr < vol_fade:
                    return False, f'⛔ 量价背离(涨{price_rally_pct*100:+.1f}%但量萎缩至{vr:.0%})', {'price_rally': price_rally_pct}
    except Exception:
        pass

    # --- Price position in range (with breakout-pullback exemption) ---
    max_pos = cfg.get('max_range_position', 0.75)
    range_lb = cfg.get('range_lookback_days', 60)
    pullback_exemption_rally = cfg.get('range_pullback_exemption_rally', 0.05)
    range_pos = 0.0
    range_demote = False
    try:
        recent_range = hist_sorted.tail(range_lb)
        if len(recent_range) >= 20:
            r_low = float(recent_range['low'].min())
            r_high = float(recent_range['high'].max())
            p_now = float(recent_range.iloc[-1]['close'])
            if r_high > r_low:
                range_pos = (p_now - r_low) / (r_high - r_low)
                if range_pos > max_pos:
                    # Breakout-pullback exemption: rally < 5% at high range = likely consolidation after breakout
                    if abs(price_rally_pct) <= pullback_exemption_rally:
                        # Demote (penalty in scoring) instead of hard kill
                        range_demote = True
                    else:
                        return False, f'⛔ 高位收敛(60天区间{range_pos:.0%}>{max_pos:.0%})', {'price_rally': price_rally_pct, 'range_pos': range_pos}
    except Exception:
        pass

    # --- Distribution candle count ---
    # Use a wider window (max of snap_days and 30) to prevent day-to-day flip-flop
    # where a distribution candle slides out of a narrow window and the stock
    # suddenly passes the next day.
    min_dist = cfg.get('min_distribution_candles', 2)
    dist_window = max(snap_days, 30)
    dist_count = 0
    try:
        vol_avg_60 = hist_sorted.tail(60)['vol'].mean() if len(hist_sorted) >= 60 else hist_sorted['vol'].mean()
        recent_bars = hist_sorted.tail(dist_window)
        for _, bar in recent_bars.iterrows():
            o, h, l, c, v = float(bar['open']), float(bar['high']), float(bar['low']), float(bar['close']), float(bar['vol'])
            if vol_avg_60 <= 0:
                continue
            vr_bar = v / vol_avg_60
            if vr_bar < 1.3:
                continue
            chg = (c - o) / o if o > 0 else 0
            body = abs(c - o)
            upper_shadow = h - max(o, c)
            if chg < -0.02:
                dist_count += 1
            elif body > 0 and upper_shadow > body * 1.5:
                dist_count += 1
            elif body <= 0.001 * h and upper_shadow > 0.01 * h:
                dist_count += 1

        if dist_count >= min_dist:
            return False, f'⛔ 出货K线({dist_count}根放量异常)', {'price_rally': price_rally_pct, 'dist_candles': dist_count}
    except Exception:
        pass

    return True, '', {'price_rally': price_rally_pct, 'range_pos': range_pos, 'dist_candles': dist_count, 'range_demote': range_demote}


# ============================================================
# Layer 3: Breakout Readiness
# ============================================================
def evaluate_breakout_readiness(row_data, hist_df, chip_today, dynamics, cfg):
    """Evaluate how close a stock is to breakout."""
    close = float(row_data['close'])
    pct_chg = float(row_data['pct_chg'])
    open_price = float(row_data['open'])

    signals = []
    warnings = []

    # Winner ratio position
    wr = chip_today['winner_ratio']
    if cfg['winner_sweet_low'] <= wr <= cfg['winner_sweet_high']:
        wr_zone = 'sweet'
        signals.append(f"获利盘甜区({wr*100:.0f}%)")
    elif wr > 0.85:
        wr_zone = 'high'
        warnings.append(f"获利盘过高({wr*100:.0f}%)，追高风险")
    elif wr < 0.30:
        wr_zone = 'low'
        warnings.append(f"获利盘偏低({wr*100:.0f}%)，底部未确认")
    elif wr > cfg['winner_sweet_high']:
        wr_zone = 'above_sweet'
        signals.append(f"获利盘偏高({wr*100:.0f}%)，已有涨幅")
    else:
        wr_zone = 'below_sweet'
        signals.append(f"获利盘({wr*100:.0f}%)接近甜区")

    # Price vs main chip peak
    main_peak = chip_today['main_peak_price']
    peak_deviation = 0
    if main_peak > 0:
        peak_deviation = (close - main_peak) / main_peak
        if 0 <= peak_deviation <= 0.05:
            signals.append(f"价格在筹码峰上沿(偏{peak_deviation*100:.1f}%)")
        elif -0.03 <= peak_deviation < 0:
            signals.append(f"价格紧贴筹码峰(偏{peak_deviation*100:.1f}%)")
        elif peak_deviation > 0.05:
            signals.append(f"已突破筹码峰({peak_deviation*100:.1f}%)")
        else:
            warnings.append(f"价格在筹码峰下方({peak_deviation*100:.1f}%)")

    # MA30 direction
    ma30_dir = 'unknown'
    if hist_df is not None and len(hist_df) >= 35:
        closes = hist_df.sort_values('trade_date')['close'].values.astype(float)
        ma30_today = np.mean(closes[-30:])
        ma30_5ago = np.mean(closes[-35:-5])
        ma30_change = (ma30_today - ma30_5ago) / ma30_5ago if ma30_5ago > 0 else 0
        if ma30_change > 0.005:
            signals.append(f"MA30向上({ma30_change*100:.1f}%)")
            ma30_dir = 'up'
        elif ma30_change > -0.005:
            signals.append("MA30走平")
            ma30_dir = 'flat'
        else:
            warnings.append(f"MA30向下({ma30_change*100:.1f}%)")
            ma30_dir = 'down'

    # K-line pattern
    is_yang = (close > open_price) and (pct_chg > 0)
    if is_yang and pct_chg >= 2.0:
        signals.append(f"长阳+{pct_chg:.1f}%")
    elif is_yang and pct_chg >= 1.0:
        signals.append(f"阳线+{pct_chg:.1f}%")
    elif pct_chg <= -2.0:
        warnings.append(f"大阴线{pct_chg:.1f}%")

    # Volume ratio
    vol_ratio = row_data.get('volume_ratio', 0)
    vr_valid = vol_ratio is not None and not (isinstance(vol_ratio, float) and pd.isna(vol_ratio)) and float(vol_ratio) > 0.1
    vr = float(vol_ratio) if vr_valid else 0.0
    if vr_valid:
        if vr >= 2.0:
            signals.append(f"放量({vr:.1f}x)")
        elif vr >= 1.5:
            signals.append(f"温和放量({vr:.1f}x)")
        elif vr < 0.7:
            warnings.append(f"缩量({vr:.1f}x)")

    # MA5 trend
    if hist_df is not None and len(hist_df) >= 10:
        recent = hist_df.sort_values('trade_date').tail(10)['close'].values.astype(float)
        ma5_today = np.mean(recent[-5:])
        ma5_yesterday = np.mean(recent[-6:-1])
        if ma5_today > ma5_yesterday:
            signals.append("MA5拐头向上")
        elif ma5_today < ma5_yesterday:
            warnings.append("MA5下行")

    # Dynamic signal integration
    if dynamics:
        if dynamics['signal'] == 'ACCUMULATING':
            signals.append("筹码收敛+重心上移(吸筹确认)")
        elif dynamics['signal'] == 'WASHING':
            signals.append("筹码收敛中(洗盘)")

    return {
        'wr_zone': wr_zone,
        'peak_deviation': round(peak_deviation, 4) if main_peak > 0 else None,
        'ma30_dir': ma30_dir,
        'vol_ratio': round(vr, 1) if vr_valid else None,
        'signals': signals,
        'warnings': warnings,
    }


# ============================================================
# Scoring System (100 pts base + MA bonus)
# ============================================================
def compute_vcp_score(hist, cfg):
    """
    VCP (Volatility Contraction Pattern) bonus score.
    Detects Minervini-style amplitude contraction: successive 10-day windows
    with shrinking high-low range.
    Returns (vcp_bonus: int, vcp_info: dict).
    """
    if hist is None or len(hist) < cfg.get('vcp_lookback_days', 40):
        return 0, {'contractions': 0, 'ratios': []}

    hist_s = hist.sort_values('trade_date')
    lookback = cfg.get('vcp_lookback_days', 40)
    window = cfg.get('vcp_window_size', 10)
    min_contractions = cfg.get('vcp_min_contractions', 2)
    contraction_ratio = cfg.get('vcp_contraction_ratio', 0.70)
    max_bonus = cfg.get('vcp_max_bonus', 10)

    recent = hist_s.tail(lookback)
    if len(recent) < lookback:
        return 0, {'contractions': 0, 'ratios': []}

    highs = recent['high'].values.astype(float)
    lows = recent['low'].values.astype(float)

    # Split into non-overlapping windows
    n_windows = lookback // window
    ranges = []
    for i in range(n_windows):
        start = i * window
        end = start + window
        w_range = np.max(highs[start:end]) - np.min(lows[start:end])
        mid = (np.max(highs[start:end]) + np.min(lows[start:end])) / 2
        pct_range = w_range / mid if mid > 0 else 0
        ranges.append(pct_range)

    if len(ranges) < 2:
        return 0, {'contractions': 0, 'ratios': []}

    # Count successive contractions
    contractions = 0
    ratios = []
    for i in range(1, len(ranges)):
        if ranges[i - 1] > 0:
            ratio = ranges[i] / ranges[i - 1]
            ratios.append(round(ratio, 3))
            if ratio <= contraction_ratio:
                contractions += 1
        else:
            ratios.append(0)

    if contractions >= min_contractions:
        bonus = max_bonus
    elif contractions >= 1:
        bonus = max_bonus // 2
    else:
        bonus = 0

    return bonus, {'contractions': contractions, 'ratios': ratios, 'ranges': [round(r * 100, 2) for r in ranges]}


def compute_score(vol_behavior, dynamics, chip_today, readiness, cfg, hist=None, extra_info=None):
    """
    Weighted scoring (anti-manipulation optimized):
      Volume behavior:           20 pts
      Concentration convergence: 15 pts  (降权: 易被对倒干扰)
      Center drift:              30 pts  (提权: 几乎不受对倒影响)
      Winner ratio position:     20 pts  (提权: 由价格决定, 抗干扰)
      Price vs chip peak:        15 pts
      VCP contraction:           +10 bonus
      MA alignment:              +15 / -10 bonus/penalty
      Dynamic precision:         +15 bonus
    """
    scores = {}

    # 1. Volume Behavior (20 pts)
    s_vol = 0
    if vol_behavior:
        rr = vol_behavior['recovery_ratio']
        if 0.75 <= rr <= 1.05:
            s_vol += 10
        elif 0.60 <= rr <= 1.20:
            s_vol += 6
        else:
            s_vol += 2

        qs = vol_behavior['quiet_streak']
        if qs >= 5:
            s_vol += 6
        elif qs >= 3:
            s_vol += 4
        else:
            s_vol += 2

        if vol_behavior['rhythm_good']:
            s_vol += 4
        else:
            s_vol += 1
    scores['s_vol'] = min(s_vol, 20)

    # 2. Concentration Convergence (15 pts — 降权: 易被对倒/对敲干扰)
    #    Only trend direction matters, absolute values unreliable
    s_conc = 0
    if dynamics:
        cc = dynamics['conc_change']
        if cc < -0.06:
            s_conc += 10
        elif cc < -0.04:
            s_conc += 8
        elif cc < -0.025:
            s_conc += 6
        elif cc < -0.015:
            s_conc += 4
        elif cc < -0.005:
            s_conc += 2
        else:
            s_conc += 0

        if dynamics['conc_r2'] > 0.7:
            s_conc += 5
        elif dynamics['conc_r2'] > 0.4:
            s_conc += 3
        elif dynamics['conc_r2'] > 0.2:
            s_conc += 1
        else:
            s_conc += 0
    scores['s_conc'] = min(s_conc, 15)

    # 3. Center Drift (30 pts — 提权: 中位成本位移几乎不受对倒影响)
    s_drift = 0
    if dynamics:
        drift = dynamics['center_drift_pct']
        if drift > 0.04:
            s_drift += 22
        elif drift > 0.02:
            s_drift += 18
        elif drift > 0.01:
            s_drift += 13
        elif drift > 0.005:
            s_drift += 9
        elif drift > -0.005:
            s_drift += 6
        elif drift > -0.01:
            s_drift += 3
        else:
            s_drift += 0

        if dynamics['median_r2'] > 0.7:
            s_drift += 8
        elif dynamics['median_r2'] > 0.4:
            s_drift += 5
        elif dynamics['median_r2'] > 0.2:
            s_drift += 3
        else:
            s_drift += 1
    scores['s_drift'] = min(s_drift, 30)

    # 4. Winner Ratio Position (20 pts — 提权: 获利比由价格决定, 抗对倒干扰)
    s_wr = 0
    if readiness:
        zone = readiness['wr_zone']
        if zone == 'sweet':
            s_wr = 20
        elif zone == 'below_sweet':
            s_wr = 14
        elif zone == 'above_sweet':
            s_wr = 10
        elif zone == 'low':
            s_wr = 5
        elif zone == 'high':
            s_wr = 2
    scores['s_wr'] = s_wr

    # 5. Price vs Chip Peak (15 pts)
    s_peak = 0
    if readiness and readiness['peak_deviation'] is not None:
        pd_val = readiness['peak_deviation']
        if 0 <= pd_val <= 0.05:
            s_peak = 15
        elif -0.03 <= pd_val < 0:
            s_peak = 12
        elif 0.05 < pd_val <= 0.10:
            s_peak = 10
        elif -0.08 <= pd_val < -0.03:
            s_peak = 6
        else:
            s_peak = 2
    scores['s_peak'] = s_peak

    # Range-position demote penalty (breakout-pullback exemption: -5 instead of kill)
    if extra_info and extra_info.get('range_demote'):
        s_peak = max(0, s_peak - 5)
        scores['s_peak'] = s_peak
        scores['range_demote'] = True
    else:
        scores['range_demote'] = False

    total = sum(v for k, v in scores.items() if isinstance(v, (int, float)) and not isinstance(v, bool))

    # 6. VCP — REMOVED (backtest proves VCP=10 → 21% WR vs VCP=5 → 63% WR)
    #    VCP completely removed from scoring AND green_count.
    scores['s_vcp'] = 0
    scores['vcp_info'] = {}

    # 7. MA Alignment Bonus/Penalty (+15 / -10)
    s_ma = 0
    ma_desc = ''
    try:
        if hist is not None and len(hist) >= 60:
            hist_s = hist.sort_values('trade_date')
            closes = hist_s['close'].astype(float)
            price_now = float(closes.iloc[-1])
            ma5 = float(closes.tail(5).mean())
            ma10 = float(closes.tail(10).mean())
            ma20 = float(closes.tail(20).mean())
            ma30 = float(closes.tail(30).mean())
            ma60 = float(closes.tail(60).mean())

            bull_count = 0
            bear_count = 0
            pairs = [(price_now, ma5), (ma5, ma10), (ma10, ma20), (ma20, ma30), (ma30, ma60)]
            for short_ma, long_ma in pairs:
                if short_ma > long_ma * 1.002:
                    bull_count += 1
                elif short_ma < long_ma * 0.998:
                    bear_count += 1

            ma30_5d_ago = float(closes.tail(35).head(5).mean()) if len(closes) >= 35 else ma30
            ma30_rising = ma30 > ma30_5d_ago * 1.001

            if bull_count >= 4:
                s_ma, ma_desc = 15, '多头排列'
            elif bull_count >= 3 and ma30_rising:
                s_ma, ma_desc = 12, '偏多排列+MA30上行'
            elif bull_count >= 3:
                s_ma, ma_desc = 8, '偏多排列'
            elif bull_count >= 2 and bear_count <= 1:
                s_ma, ma_desc = 5, '均线粘合偏多'
            elif bear_count >= 4:
                s_ma, ma_desc = -10, '空头排列'
            elif bear_count >= 3:
                s_ma, ma_desc = -5, '偏空排列'
            else:
                s_ma, ma_desc = 0, '均线粘合'
    except Exception:
        pass

    scores['s_ma'] = s_ma
    scores['ma_desc'] = ma_desc
    total += s_ma

    # 8. Dynamic Precision — "2 of 3" gate + bonus scoring
    #    Anti-manipulation weighted: drift/winner checks > concentration checks
    #    - Center mostly up:     +5 (high reliability, immune to wash trades)
    #    - Concentration ok:     +3 (lower reliability, susceptible to wash trades)
    #    - Winner ratio sweet:   +5 (high reliability, price-determined)
    #    - Chip tightness:       +3 (lower reliability, absolute value unreliable)
    #    Gate: at least 3 of 4 must pass, otherwise rejected
    s_dyn = 0
    dyn_checks = {'center_up': False, 'conc_ok': False, 'winner_ok': False, 'conc_tight': False}
    dyn_details = []
    if dynamics:
        if dynamics.get('center_monotonic_up'):
            dyn_checks['center_up'] = True
            s_dyn += 5
            dyn_details.append('重心上移✓')
        else:
            dyn_details.append('重心未上移✗')
        if dynamics.get('conc_change', 0) <= 0:
            dyn_checks['conc_ok'] = True
            s_dyn += 3
            dyn_details.append('集中度收敛✓')
        else:
            dyn_details.append('集中度发散✗')
        winner = dynamics.get('winner_newest', 0)
        w_low = cfg.get('winner_sweet_low', 0.35)
        w_high = cfg.get('winner_sweet_high', 0.80)
        if w_low <= winner <= w_high:
            dyn_checks['winner_ok'] = True
            s_dyn += 5
            dyn_details.append(f'获利盘{winner*100:.0f}%✓')
        else:
            dyn_details.append(f'获利盘{winner*100:.0f}%✗')
        # Absolute chip tightness — lower weight (absolute values unreliable)
        conc_abs = dynamics.get('conc_newest', 999)
        if conc_abs < 0.30:
            dyn_checks['conc_tight'] = True
            s_dyn += 3
            dyn_details.append(f'筹码密集{conc_abs:.3f}✓')
        else:
            dyn_details.append(f'筹码分散{conc_abs:.3f}✗')
    dyn_pass_count = sum(dyn_checks.values())
    dyn_pass = dyn_pass_count >= 2  # 2-of-4 gate (relaxed: washing signals inherently miss center_up)
    scores['dyn_pass'] = dyn_pass
    scores['dyn_checks'] = dyn_checks
    scores['dyn_pass_count'] = dyn_pass_count
    scores['s_dyn'] = s_dyn
    scores['dyn_details'] = ','.join(dyn_details) if dyn_details else ''
    total += s_dyn  # bonus for each passed dimension

    # Winner ratio penalty
    penalty_threshold = cfg.get('winner_high_penalty', 0.80)
    if chip_today and chip_today['winner_ratio'] > penalty_threshold:
        total = int(total * 0.75)
        scores['penalty'] = f"获利比>{penalty_threshold*100:.0f}%打7.5折"
    else:
        scores['penalty'] = None

    scores['total'] = total
    return total, scores


# ============================================================
# Action Signal
# ============================================================
def determine_action(total_score, dynamics):
    """Determine action level based on total score and dynamic signal."""
    signal = dynamics['signal'] if dynamics else 'UNCLEAR'

    if signal in ('TRAPPED', 'DEAD', 'DISTRIBUTING'):
        return {
            'level': '🔴 排除',
            'action': f"动态信号为{dynamics['signal_cn']}，不符合吸筹特征",
            'priority': 99,
        }

    if total_score >= 75:
        return {'level': '🟢 信号确认', 'action': '吸筹信号强烈+临界突破，等待入场时机(选股器是雷达不是扳机)', 'priority': 0}
    elif total_score >= 60:
        return {'level': '🟢 接近', 'action': '吸筹确认，等放量阳线突破筹码峰即可入场', 'priority': 1}
    elif total_score >= 45:
        return {'level': '🟡 观望', 'action': '吸筹进行中但未到临界点，加入自选跟踪', 'priority': 2}
    elif total_score >= 30:
        return {'level': '🟡 早期', 'action': '有吸筹迹象但尚早，持续关注集中度变化', 'priority': 3}
    else:
        return {'level': '🔴 等待', 'action': '信号不足，耐心等待', 'priority': 4}


# ============================================================
# Main Screening Logic
# ============================================================
def run_chip_screen_v3():
    """
    Unified Chip Screener v3.0

    Pipeline:
      Layer 1: Volume Behavior Scan → quiet zone + gentle recovery
      Layer 2: Chip Convergence → concentration trend + center drift + anti-fake
      Layer 3: Breakout Readiness + Scoring
    """
    print("\n" + "=" * 80)
    total_pool = sum(len(v) for v in STOCK_POOL.values())
    print(f"[START] 统一筹码选股器 v3.0 ({total_pool} stocks, {len(STOCK_POOL)} sectors)")
    print(f"[TIME]  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[MODE]  量能行为扫描 → 动态筹码收敛 → 临界点评分")
    print("=" * 80)

    cfg = V3_CONFIG
    pro = ts.pro_api(TUSHARE_TOKEN)

    # ---- Step 0: Latest trade date ----
    print("\n[STEP 0] Finding latest trade date...")
    trade_date, is_today = find_latest_trade_date(pro)
    if trade_date is None:
        print("[ERROR] Cannot find recent trade date. Exiting.")
        return None
    print(f"  Latest trade date: {trade_date}")
    today_str = datetime.now().strftime('%Y%m%d')
    if trade_date < today_str and datetime.now().hour >= 15:
        print(f"  ⚠️  tushare 今日({today_str})数据尚未入库，使用 {trade_date} 数据。")

    # ---- Step 0.5: Market stage check ----
    print("\n[STEP 0.5] Checking market environment (CSI 300 x MA200)...")
    stage_info = get_market_stage(pro, trade_date)
    print_market_stage(stage_info)

    if stage_info['stage'] == 'RED':
        print("\n" + "!" * 80)
        print("[WARN] 沪深300 MA200向下，Stage 4下跌期。正常应停机，但继续扫描供观察。")
        print("!" * 80)

    # ---- Step 1: Resolve stock pool ----
    print("\n[STEP 1] Resolving stock pool...")
    code_list, not_found = resolve_pool_to_codes(pro)
    print(f"  Resolved: {len(code_list)} stocks")
    if not_found:
        print(f"  Skipped {len(not_found)}: {', '.join([n for n, s in not_found[:10]])}"
              + (f" ...+{len(not_found)-10}" if len(not_found) > 10 else ""))

    # ---- Step 2: Daily data + pre-filter ----
    print("\n[STEP 2] Loading daily data...")
    ts_codes = [c[0] for c in code_list]
    code_sector_map = {c[0]: (c[1], c[2]) for c in code_list}

    try:
        df_daily = api_call_with_retry(
            pro.daily, trade_date=trade_date,
            fields='ts_code,trade_date,open,high,low,close,vol,amount,pct_chg'
        )
        df_basic = api_call_with_retry(
            pro.daily_basic, trade_date=trade_date,
            fields='ts_code,turnover_rate,volume_ratio,total_mv'
        )
    except Exception as e:
        print(f"[ERROR] API调用失败: {e}")
        print("[ERROR] 可能原因: TUSHARE_TOKEN无效、网络连接问题或API服务异常")
        return None

    if df_daily.empty or df_basic.empty:
        print(f"[ERROR] 数据获取失败: daily数据={len(df_daily)}行, basic数据={len(df_basic)}行")
        print(f"[ERROR] 可能原因: 交易日期{trade_date}无数据、股票池为空或API返回异常")
        return None

    df = df_daily.merge(df_basic, on='ts_code', how='inner')
    df = df[df['ts_code'].isin(ts_codes)].copy()
    print(f"  Pool stocks with data: {len(df)}/{len(ts_codes)}")

    num_cols = ['open', 'high', 'low', 'close', 'vol', 'amount', 'pct_chg',
                'turnover_rate', 'volume_ratio', 'total_mv']
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df[df['vol'] > 0]
    df = df[(df['pct_chg'] > -7.5) & (df['pct_chg'] < 7.5)]

    # Remove ST / 北交所
    st_codes = set()
    try:
        name_map = build_name_to_code_map(pro)
        code_to_name = {v: k for k, v in name_map.items()}
        for code in df['ts_code'].tolist():
            n = code_to_name.get(code, '')
            nm = code_sector_map.get(code, ('', ''))[0]
            check = n or nm
            if 'ST' in check.upper():
                st_codes.add(code)
    except Exception:
        for code in df['ts_code'].tolist():
            nm = code_sector_map.get(code, ('', ''))[0]
            if 'ST' in nm.upper():
                st_codes.add(code)

    bj_codes = {c for c in df['ts_code'].tolist() if '.BJ' in c}
    excluded = st_codes | bj_codes
    if excluded:
        df = df[~df['ts_code'].isin(excluded)]
        if st_codes:
            print(f"  [排除] ST股 {len(st_codes)} 只")
        if bj_codes:
            print(f"  [排除] 北交所 {len(bj_codes)} 只")

    print(f"  After pre-filter: {len(df)}")
    if df.empty:
        print("[DONE] No stocks passed basic filter.")
        return None

    # ---- Step 3: Load/update cache ----
    pool_codes = list(df['ts_code'].unique())

    # Also include signal pool stocks (for breakout confirmation on old signals)
    _old_pool = _load_signal_pool()
    _pool_extra_codes = [e['ts_code'] for e in _old_pool if e['ts_code'] not in pool_codes]
    if _pool_extra_codes:
        pool_codes.extend(_pool_extra_codes)
        print(f"  [信号池] 追加 {len(_pool_extra_codes)} 只旧信号股票到缓存加载列表")

    print(f"\n[STEP 3] Loading history for {len(pool_codes)} stocks...")
    cache_data = load_cache()
    history_data = update_cache_incremental(cache_data, pool_codes, trade_date, pro)
    if history_data:
        save_cache(history_data)

    # ============================================================
    # MULTI-STRATEGY POOL SCAN (Breakout / Trend / Pullback)
    # Runs in parallel with the chip funnel — independent scoring
    # ============================================================
    print(f"\n{'=' * 80}")
    print(f"[STRATEGY POOLS] 多策略池扫描 — {len(pool_codes)} stocks")
    print(f"  🚀 突破池: 放量创新高 | 🎯 趋势池: Minervini强势 | 🔄 回踩池: 强势股回踩")
    print(f"{'=' * 80}")

    # Load CSI300 index data for RS calculation
    _hs300_close = None
    try:
        _start_rs = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=400)).strftime('%Y%m%d')
        _idx_df = api_call_with_retry(
            pro.index_daily, ts_code='000300.SH',
            start_date=_start_rs, end_date=trade_date,
            fields='trade_date,close'
        )
        if _idx_df is not None and len(_idx_df) >= 120:
            _idx_df = _idx_df.sort_values('trade_date').reset_index(drop=True)
            _hs300_close = pd.to_numeric(_idx_df['close'], errors='coerce')
            print(f"  [RS] 沪深300数据加载成功 ({len(_hs300_close)} bars)")
        else:
            print(f"  [RS] 沪深300数据不足，RS默认为0")
    except Exception as e:
        print(f"  [RS] 沪深300加载失败: {e}，RS默认为0")

    strategy_results = []  # [{ts_code, name, sector, best_pool, scores, ...}]
    _strat_count = {'breakout': 0, 'trend': 0, 'pullback': 0}

    for ts_code in pool_codes:
        hist = history_data.get(ts_code)
        if hist is None or hist.empty or len(hist) < 70:
            continue

        hist = hist.sort_values('trade_date').copy()
        for col in ['open', 'high', 'low', 'close', 'vol']:
            hist[col] = pd.to_numeric(hist[col], errors='coerce')
        hist = hist.dropna(subset=['close', 'vol'])

        if len(hist) < 70:
            continue

        # Compute RS vs CSI300
        rs_6m = 0.0
        if _hs300_close is not None and len(hist) >= 120:
            rs_6m = compute_rs_vs_index(hist['close'], _hs300_close, lookback=120)

        # Score all 3 strategy pools
        scores = score_all_strategies(hist, rs_6m)
        best = best_strategy(scores)

        if best is not None and best.score >= 50:
            name, sector = code_sector_map.get(ts_code, ('N/A', 'N/A'))
            _strat_count[best.name] += 1
            strategy_results.append({
                'ts_code': ts_code,
                'name': name,
                'sector': sector,
                'best_pool': best.name,
                'best_label': best.label,
                'best_score': best.score,
                'best_grade': best.grade,
                'best_reasons': best.reasons,
                'best_warnings': best.warnings,
                'best_details': best.details,
                'all_scores': scores,
                'summary': format_strategy_summary(scores),
                'rs_6m': rs_6m,
                'close': float(hist['close'].iloc[-1]),
            })

    # Sort by score descending
    strategy_results.sort(key=lambda x: x['best_score'], reverse=True)

    print(f"  ✅ 多策略池结果: {len(strategy_results)} 只通过")
    print(f"     🚀突破: {_strat_count['breakout']} | 🎯趋势: {_strat_count['trend']} | 🔄回踩: {_strat_count['pullback']}")

    # ============================================================
    # THREE-LAYER FUNNEL (Original Chip Strategy)
    # ============================================================

    # ---- Layer 1: Volume Behavior Scan ----
    total = len(df)
    print(f"\n{'=' * 80}")
    print(f"[LAYER 1] 量能行为扫描 — {total} stocks")
    print(f"  寻找: 地量区(连续{cfg['vol_quiet_min_streak']}天<基准{cfg['vol_quiet_threshold']*100:.0f}%) → "
          f"温和回升({cfg['vol_recovery_min']*100:.0f}%-{cfg['vol_recovery_max']*100:.0f}%)")
    print(f"{'=' * 80}")

    layer1_pass = []
    layer1_fail = 0

    for _, row in df.iterrows():
        ts_code = row['ts_code']
        hist = history_data.get(ts_code)
        if hist is None or hist.empty or len(hist) < cfg['vol_baseline_days']:
            layer1_fail += 1
            continue

        hist = hist.sort_values('trade_date').copy()
        for col in ['open', 'high', 'low', 'close', 'vol']:
            hist[col] = pd.to_numeric(hist[col], errors='coerce')
        hist = hist.dropna(subset=['close', 'vol'])

        vol_result = scan_volume_behavior(hist, cfg)
        if vol_result is None:
            layer1_fail += 1
            continue

        layer1_pass.append({
            'ts_code': ts_code,
            'row': row,
            'hist': hist,
            'vol_behavior': vol_result,
        })

    print(f"  ✅ Layer 1 通过: {len(layer1_pass)} stocks (淘汰 {layer1_fail})")

    if not layer1_pass:
        print("\n[DONE] Layer 1 无股票通过量能行为扫描。")
        return None

    # ---- Layer 2: Chip Convergence Confirmation ----
    print(f"\n{'=' * 80}")
    print(f"[LAYER 2] 动态筹码收敛确认 — {len(layer1_pass)} stocks")
    print(f"  计算: 20天+40天双窗口筹码快照 → 集中度趋势 + 重心位移 + 反作弊")
    print(f"{'=' * 80}")

    layer2_pass = []
    layer2_all = []
    layer2_stats = {'accumulating': 0, 'washing': 0, 'trapped': 0, 'dead': 0, 'distributing': 0, 'unclear': 0}

    for idx, item in enumerate(layer1_pass):
        ts_code = item['ts_code']
        hist = item['hist']
        name, sector = code_sector_map.get(ts_code, ('N/A', 'N/A'))

        if (idx + 1) % 50 == 0:
            print(f"  ... {idx+1}/{len(layer1_pass)} done, found {len(layer2_pass)} candidates")

        fast_snaps, slow_snaps = compute_chip_snapshots(hist, cfg)
        if fast_snaps is None and slow_snaps is None:
            layer2_stats['unclear'] += 1
            layer2_all.append({
                'ts_code': ts_code, 'name': name, 'sector': sector,
                'signal': '数据不足', 'conc_change': 'N/A', 'drift_pct': 'N/A',
                'price_rally': 'N/A', 'range_pos': 'N/A', 'dist_candles': 0,
                'passed': False, 'reason': '快照不足',
            })
            continue

        # Analyze both windows, pick the better signal
        dyn_fast = analyze_chip_dynamics(fast_snaps, cfg) if fast_snaps else None
        dyn_slow = analyze_chip_dynamics(slow_snaps, cfg) if slow_snaps else None

        # Priority: ACCUMULATING > WASHING > others; pick the stronger signal
        _signal_rank = {'ACCUMULATING': 0, 'WASHING': 1, 'UNCLEAR': 2, 'TRAPPED': 3, 'DEAD': 4, 'DISTRIBUTING': 5}
        def _pick_better(d1, d2):
            if d1 is None:
                return d2, 'slow'
            if d2 is None:
                return d1, 'fast'
            r1 = _signal_rank.get(d1['signal'], 9)
            r2 = _signal_rank.get(d2['signal'], 9)
            if r1 < r2:
                return d1, 'fast'
            elif r2 < r1:
                return d2, 'slow'
            # Same signal type: prefer stronger convergence
            return (d1, 'fast') if d1['conc_change'] < d2['conc_change'] else (d2, 'slow')

        dynamics, snap_mode = _pick_better(dyn_fast, dyn_slow)
        snapshots = fast_snaps if snap_mode == 'fast' else slow_snaps

        if dynamics is None:
            layer2_stats['unclear'] += 1
            layer2_all.append({
                'ts_code': ts_code, 'name': name, 'sector': sector,
                'signal': '分析失败', 'conc_change': 'N/A', 'drift_pct': 'N/A',
                'price_rally': 'N/A', 'range_pos': 'N/A', 'dist_candles': 0,
                'passed': False, 'reason': '动态分析失败',
            })
            continue

        sig = dynamics['signal']
        conc_change = dynamics['conc_change']

        # Determine pass/fail by signal type
        passed = False
        reason = ''

        if sig == 'ACCUMULATING':
            layer2_stats['accumulating'] += 1
            passed, reason = True, '吸筹确认'
        elif sig == 'WASHING':
            layer2_stats['washing'] += 1
            passed, reason = True, '洗盘收敛'
        elif sig == 'TRAPPED':
            layer2_stats['trapped'] += 1
            reason = '套牢被动集中(重心下移)'
        elif sig == 'DEAD':
            layer2_stats['dead'] += 1
            reason = '死水无变化'
        elif sig == 'DISTRIBUTING':
            layer2_stats['distributing'] += 1
            reason = '筹码发散(出货)'
        else:
            layer2_stats['unclear'] += 1
            if conc_change < -0.005:
                passed, reason = True, '微弱收敛(观察)'
            else:
                reason = f'信号不明(Δ{conc_change*100:+.1f}%)'

        # Anti-fake checks
        extra_info = {'price_rally': 0.0, 'range_pos': 0.0, 'dist_candles': 0}
        if passed:
            af_passed, af_reason, extra_info = run_anti_fake_checks(hist, dynamics, cfg)
            if not af_passed:
                passed = False
                reason = af_reason
                # Adjust stats
                if sig == 'ACCUMULATING':
                    layer2_stats['accumulating'] -= 1
                elif sig == 'WASHING':
                    layer2_stats['washing'] -= 1
                layer2_stats['trapped'] += 1

        layer2_all.append({
            'ts_code': ts_code, 'name': name, 'sector': sector,
            'signal': dynamics['signal_cn'],
            'conc_change': f"{conc_change*100:+.1f}%",
            'drift_pct': f"{dynamics['center_drift_pct']*100:+.2f}%",
            'price_rally': f"{extra_info.get('price_rally', 0)*100:+.1f}%",
            'range_pos': f"{extra_info.get('range_pos', 0):.0%}",
            'dist_candles': extra_info.get('dist_candles', 0),
            'passed': passed, 'reason': reason,
        })

        if not passed:
            continue

        # Compute today's chip distribution for Layer 3
        chip_hist = hist.tail(cfg['history_days'])
        close_today = float(item['row']['close'])
        chip_today = compute_chip_distribution(chip_hist, close_today, cfg)
        if chip_today is None:
            continue

        item['snapshots'] = snapshots
        item['dynamics'] = dynamics
        item['chip_today'] = chip_today
        item['snap_mode'] = snap_mode
        item['extra_info'] = extra_info
        layer2_pass.append(item)

    print(f"  ✅ Layer 2 通过: {len(layer2_pass)} stocks")
    print(f"     🔥吸筹: {layer2_stats['accumulating']} | "
          f"🟡洗盘: {layer2_stats['washing']} | "
          f"⚠️套牢: {layer2_stats['trapped']} | "
          f"💀死水: {layer2_stats['dead']} | "
          f"🔴发散: {layer2_stats['distributing']} | "
          f"❓不明: {layer2_stats['unclear']}")

    # Debug diagnostic table
    if cfg.get('debug_layer2') and layer2_all:
        print(f"\n  {'─' * 90}")
        print(f"  === 【Layer 2 诊断表】全部 {len(layer2_all)} 只 ===")
        print(f"  {'─' * 90}")
        diag_df = pd.DataFrame(layer2_all)
        def sort_key(row):
            try:
                cc = float(str(row['conc_change']).replace('%', '').replace('+', ''))
            except:
                cc = 999
            return (0 if row['passed'] else 1, cc)
        diag_df['_sort'] = diag_df.apply(sort_key, axis=1)
        diag_df = diag_df.sort_values('_sort').drop(columns='_sort')
        diag_cols = ['ts_code', 'name', 'sector', 'signal', 'conc_change', 'drift_pct',
                     'price_rally', 'range_pos', 'dist_candles', 'passed', 'reason']
        col_names = {
            'ts_code': '代码', 'name': '名称', 'sector': '板块',
            'signal': '动态信号', 'conc_change': '集中度Δ',
            'drift_pct': '重心漂移', 'price_rally': '20天涨幅',
            'range_pos': '60天位置', 'dist_candles': '出货K线',
            'passed': '通过', 'reason': '判定',
        }
        available = [c for c in diag_cols if c in diag_df.columns]
        print(diag_df[available].rename(columns=col_names).to_string(index=False))
        print(f"  {'─' * 90}")

    if not layer2_pass:
        print("\n[DONE] Layer 2 无股票通过筹码收敛确认。")
        return None

    # ---- Layer 3: Breakout Readiness + Scoring ----
    print(f"\n{'=' * 80}")
    print(f"[LAYER 3] 临界点信号 + 综合评分 — {len(layer2_pass)} stocks")
    print(f"{'=' * 80}")

    results = []

    for item in layer2_pass:
        ts_code = item['ts_code']
        row = item['row']
        hist = item['hist']
        vol_behavior = item['vol_behavior']
        dynamics = item['dynamics']
        chip_today = item['chip_today']
        name, sector = code_sector_map.get(ts_code, ('N/A', 'N/A'))

        close_today = float(row['close'])
        total_mv_yi = float(row['total_mv']) / 10000
        turnover = float(row.get('turnover_rate', 0))

        # Basic filters (relaxed)
        if total_mv_yi < cfg['min_market_cap_yi'] or total_mv_yi > cfg['max_market_cap_yi']:
            continue
        if turnover < cfg['min_turnover']:
            continue
        high_250 = hist['high'].max()
        drop_from_high = (high_250 - close_today) / high_250 if high_250 > 0 else 0
        # OR logic: pass if drop <= 20% OR 60d range position < 50%
        # This avoids killing "shallow-drop + long-consolidation" breakout candidates
        range_lb_filt = cfg.get('range_lookback_days', 60)
        recent_filt = hist.sort_values('trade_date').tail(range_lb_filt)
        range_pos_filt = 0.0
        if len(recent_filt) >= 20:
            r_low_f = float(recent_filt['low'].min())
            r_high_f = float(recent_filt['high'].max())
            if r_high_f > r_low_f:
                range_pos_filt = (close_today - r_low_f) / (r_high_f - r_low_f)
        drop_ok = drop_from_high <= cfg['max_drop_from_high']
        pos_ok = range_pos_filt < cfg.get('alt_range_position_low', 0.50)
        if not (drop_ok or pos_ok):
            continue
        if chip_today['peak_count'] > cfg['max_peaks']:
            continue

        readiness = evaluate_breakout_readiness(row, hist, chip_today, dynamics, cfg)
        extra_info = item.get('extra_info', {})
        total_score, score_detail = compute_score(vol_behavior, dynamics, chip_today, readiness, cfg, hist=hist, extra_info=extra_info)

        # Dynamic Precision gate — 2 of 3 must pass
        if not score_detail.get('dyn_pass', False):
            continue

        action_info = determine_action(total_score, dynamics)

        if action_info['priority'] >= 99:
            continue

        med_cost = chip_today['median_cost']
        low_250 = hist['low'].min()
        rise_from_low = (close_today - low_250) / low_250 if low_250 > 0 else 0

        results.append({
            'ts_code': ts_code,
            'name': name,
            'sector': sector,
            'close': round(close_today, 2),
            'pct_chg': round(float(row['pct_chg']), 2),
            'total_mv_yi': round(total_mv_yi, 1),
            'turnover': round(turnover, 2),
            'drop_pct': f"{drop_from_high*100:.1f}%",
            'rise_pct': f"{rise_from_low*100:.1f}%",
            # Chip static
            'chip_conc': f"{chip_today['concentration_90']*100:.1f}%",
            'winner': f"{chip_today['winner_ratio']*100:.1f}%",
            'med_cost': med_cost,
            'peaks': chip_today['peak_count'],
            'main_peak': chip_today['main_peak_price'],
            # Volume behavior
            'quiet_streak': vol_behavior['quiet_streak'],
            'recovery_ratio': vol_behavior['recovery_ratio'],
            'baseline_vol': vol_behavior['baseline_vol'],
            'vol_rhythm': '✓' if vol_behavior['rhythm_good'] else '✗',
            # Dynamic signals
            'dyn_signal': dynamics['signal_cn'],
            'conc_change': f"{dynamics['conc_change']*100:+.1f}%",
            'conc_oldest': f"{dynamics['conc_oldest']*100:.1f}%",
            'conc_newest': f"{dynamics['conc_newest']*100:.1f}%",
            'drift_pct': f"{dynamics['center_drift_pct']*100:+.2f}%",
            'drift_dir': dynamics['drift_direction'],
            'winner_vel': f"{dynamics['winner_velocity']*100:+.2f}%/期",
            # Scores
            'score': total_score,
            's_vol': score_detail['s_vol'],
            's_conc': score_detail['s_conc'],
            's_drift': score_detail['s_drift'],
            's_wr': score_detail['s_wr'],
            's_peak': score_detail['s_peak'],
            's_ma': score_detail.get('s_ma', 0),
            'ma_desc': score_detail.get('ma_desc', ''),
            's_dyn': score_detail.get('s_dyn', 0),
            'dyn_details': score_detail.get('dyn_details', ''),
            'snap_mode': item.get('snap_mode', 'fast'),
            'range_demote': score_detail.get('range_demote', False),
            # Action
            'signal': action_info['level'],
            'action': action_info['action'],
            'priority': action_info['priority'],
            # Readiness detail
            'sig_detail': readiness['signals'],
            'sig_warn': readiness['warnings'],
            'vol_ratio': readiness['vol_ratio'],
            # Stop / target (v3.0: +10% partial sell, +25% second target, -7% stop)
            'stop_7': round(close_today * 0.93, 2),
            'target_1': round(close_today * 1.10, 2),
            'target_2': round(close_today * 1.25, 2),
        })

    print(f"  ✅ Layer 3 最终候选: {len(results)} stocks")

    # ---- Layer 4: Breakout Confirmation + Signal Pool Management ----
    print(f"\n{'=' * 80}")
    print(f"[LAYER 4] 突破确认 — 信号池管理 + 今日突破检测")
    print(f"  规则: 信号发出后等待最多{BREAKOUT_CONFIG['signal_pool_max_age_days']}天")
    print(f"  突破K线: 涨幅≥+{BREAKOUT_CONFIG['min_pct_chg_breakout']:.0f}% + 量比≥{BREAKOUT_CONFIG['min_vol_ratio_breakout']:.1f}x")
    print(f"{'=' * 80}")

    bcfg = BREAKOUT_CONFIG
    max_age = bcfg['signal_pool_max_age_days']

    # 4a. Load existing signal pool and purge expired entries
    old_pool = _load_signal_pool()
    today_dt = datetime.strptime(trade_date, '%Y%m%d')
    active_pool = []
    expired_count = 0
    for entry in old_pool:
        sig_dt = datetime.strptime(entry['signal_date'], '%Y%m%d')
        age_days = (today_dt - sig_dt).days
        if age_days <= max_age and entry.get('ts_code') not in [r['ts_code'] for r in results]:
            # Keep old signals that haven't expired and aren't re-signaled today
            active_pool.append(entry)
        elif age_days > max_age:
            expired_count += 1

    if expired_count:
        print(f"  [信号池] 清理 {expired_count} 个过期信号(>{max_age}天)")

    # 4b. Add today's new Layer 3 candidates to signal pool
    new_signals = []
    for r in results:
        new_signals.append({
            'ts_code': r['ts_code'],
            'name': r['name'],
            'sector': r['sector'],
            'signal_date': trade_date,
            'signal_close': r['close'],
            'score': r['score'],
            'dyn_signal': r['dyn_signal'],
            'chip_conc': r['chip_conc'],
            'winner': r['winner'],
            'med_cost': r['med_cost'],
            # Volume behavior data (for quick_check display)
            'quiet_streak': r['quiet_streak'],
            'recovery_ratio': r['recovery_ratio'],
            'baseline_vol': r.get('baseline_vol', 0),
        })
    print(f"  [信号池] 今日新增 {len(new_signals)} 个信号")

    # Merge: today's signals + old active signals
    full_pool = new_signals + active_pool
    print(f"  [信号池] 当前活跃信号: {len(full_pool)} 个")

    # 4c. Check breakout confirmation for ALL active signals
    breakout_confirmed = []
    still_waiting = []

    for entry in full_pool:
        ts_code = entry['ts_code']
        sig_dt = datetime.strptime(entry['signal_date'], '%Y%m%d')
        wait_days = (today_dt - sig_dt).days

        # BUG FIX: Signal and breakout on the same day (wait_days=0) is not actionable
        # in live trading — you never see it in the signal pool before it "confirms".
        # Require at least 1 day of waiting so quick_check.py can surface it first.
        if wait_days < 1:
            still_waiting.append(entry)
            continue

        is_bo, bo_details = _check_breakout_today(ts_code, history_data, trade_date, pro, bcfg)

        if is_bo:
            entry['breakout_date'] = trade_date
            entry['breakout_close'] = bo_details['close']
            entry['breakout_pct_chg'] = bo_details['pct_chg']
            entry['breakout_vol_ratio'] = bo_details['vol_ratio']
            entry['wait_days'] = wait_days
            breakout_confirmed.append(entry)
        else:
            still_waiting.append(entry)

    print(f"\n  🔫 今日突破确认: {len(breakout_confirmed)} 只 → 【直接买入】")
    print(f"  👀 等待突破中:   {len(still_waiting)} 只 → 【继续观察】")

    # 4d. Save updated signal pool (only those still waiting)
    _save_signal_pool(still_waiting)
    print(f"  [信号池] 已保存 {len(still_waiting)} 个待确认信号")

    # 4e. Archive today's confirmed breakouts for downstream tools
    #     (e.g. quick_check.py) to display after pool pruning.
    _append_breakout_archive(breakout_confirmed, trade_date)
    if breakout_confirmed:
        print(f"  [突破归档] 已归档 {len(breakout_confirmed)} 只今日突破"
              f"(保留最近{BREAKOUT_ARCHIVE_RETAIN_DAYS}天)")

    # 4f. Save multi-strategy pool signals to separate file (Top 5 per pool)
    if strategy_results:
        _strat_pool_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'cache', 'strategy_signal_pool.json'
        )
        MAX_PER_POOL = 5
        # Group by pool, sort by score desc, keep top N per pool
        _pool_buckets = {}
        for sr in strategy_results:
            if sr['best_score'] >= 80:
                pool_name = sr['best_pool']
                _pool_buckets.setdefault(pool_name, []).append(sr)
        _strat_signals = []
        for pool_name, items in _pool_buckets.items():
            items.sort(key=lambda x: x['best_score'], reverse=True)
            for sr in items[:MAX_PER_POOL]:
                _strat_signals.append({
                    'ts_code': sr['ts_code'],
                    'name': sr['name'],
                    'sector': sr['sector'],
                    'signal_date': trade_date,
                    'signal_close': sr['close'],
                    'pool': sr['best_pool'],
                    'pool_label': sr['best_label'],
                    'score': sr['best_score'],
                    'grade': sr['best_grade'],
                    'rs_6m': round(sr['rs_6m'] * 100, 1),
                    'reasons': sr['best_reasons'][:3],
                })
        os.makedirs(os.path.dirname(_strat_pool_file), exist_ok=True)
        with open(_strat_pool_file, 'w', encoding='utf-8') as f:
            json.dump(_strat_signals, f, ensure_ascii=False, indent=2)
        _pool_counts = {k: min(len(v), MAX_PER_POOL) for k, v in _pool_buckets.items()}
        print(f"  [策略池] 已保存 {len(_strat_signals)} 个信号(每池Top{MAX_PER_POOL}) → cache/strategy_signal_pool.json")
        print(f"           {' '.join(f'{k}:{v}' for k, v in _pool_counts.items())}")

    # ---- Moneyflow reference (not a hard filter) ----
    if results:
        candidate_codes = [r['ts_code'] for r in results]
        print(f"\n[STEP 4.5] Moneyflow check (参考, 不过滤) on {len(candidate_codes)} candidates...")
        try:
            passed_codes, mf_filtered = filter_by_moneyflow(pro, trade_date, candidate_codes)
            if mf_filtered:
                print(f"  [参考] 以下 {len(mf_filtered)} 只资金面数据异常(仅供参考,不淘汰):")
                for item in mf_filtered:
                    name_str = next((r['name'] for r in results if r['ts_code'] == item['ts_code']), item['ts_code'])
                    print(f"    ⚠️ {name_str}({item['ts_code']}) — {item['reason']}")
            print(f"  [参考] 资金面正常: {len(passed_codes)} 只")
        except Exception as e:
            print(f"  [参考] Moneyflow查询失败: {e}")

    # ============================================================
    # OUTPUT (with TeeWriter for txt export)
    # ============================================================
    _output_buf = io.StringIO()
    _orig_stdout = sys.stdout
    sys.stdout = TeeWriter(_orig_stdout, _output_buf)

    td_display = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

    print(f"\n{'=' * 80}")
    print(f"  🔬 统一筹码选股结果 v3.0 — {td_display}")
    print(f"{'=' * 80}")

    # Funnel summary
    print(f"\n{'=' * 80}")
    print(f"  筛选漏斗:")
    print(f"    票池总量: {total_pool} stocks")
    print(f"    → Layer 1 量能行为扫描: {len(layer1_pass)} 只通过 (地量→温和回升)")
    print(f"    → Layer 2 筹码收敛确认: {len(layer2_pass)} 只通过 (集中度↓ + 重心↑)")
    print(f"    → Layer 3 临界点+评分:  {len(results)} 只最终候选")
    print(f"    → Layer 4 突破确认:     🔫 {len(breakout_confirmed)} 只今日突破 | 👀 {len(still_waiting)} 只等待中")
    if strategy_results:
        print(f"    → 多策略池:  🚀{_strat_count['breakout']} 🎯{_strat_count['trend']} 🔄{_strat_count['pullback']}  共 {len(strategy_results)} 只")
    print(f"{'=' * 80}")

    # ═══════════════════════════════════════════════════════════
    # 🚀🎯🔄 MULTI-STRATEGY POOL RESULTS
    # ═══════════════════════════════════════════════════════════
    if strategy_results:
        print(f"\n{'═' * 80}")
        print(f"  🚀🎯🔄 【多策略池 — 突破/趋势/回踩】({len(strategy_results)} 只)")
        print(f"{'═' * 80}")
        print(f"  {'─' * 76}")
        print(f"  {'代码':<12}{'名称':<8}{'板块':<8}{'池子':<8}{'评分':<6}{'等级':<4}{'RS%':<7}{'价格':<8}  理由")
        print(f"  {'─' * 76}")

        # Group by pool type
        for pool_name, pool_icon in [('breakout', '🚀'), ('trend', '🎯'), ('pullback', '🔄')]:
            pool_items = [r for r in strategy_results if r['best_pool'] == pool_name]
            if not pool_items:
                continue
            pool_label = STRATEGY_LABELS[pool_name]
            print(f"\n  === {pool_label} ({len(pool_items)}只) ===")
            for r in pool_items[:10]:  # Top 10 per pool
                reasons_str = '; '.join(r['best_reasons'][:2]) if r['best_reasons'] else ''
                print(f"  {r['ts_code']:<12}{r['name']:<8}{r['sector']:<8}"
                      f"{pool_icon:<8}{r['best_score']:<6.0f}{r['best_grade']:<4}"
                      f"{r['rs_6m']*100:+5.1f}%  {r['close']:<8.2f}{reasons_str}")
                if r['best_warnings']:
                    warn_str = '; '.join(r['best_warnings'][:2])
                    print(f"  {'':>52}⚠️ {warn_str}")

        print(f"\n  {'─' * 76}")
        print(f"  多策略池说明:")
        print(f"    🚀 突破池: 距250日高≤5% + 放量≥1.3x + MA多头 → 目标+10%~+20%, 止损-5%")
        print(f"    🎯 趋势池: 价>MA50>MA150>MA200 + MA200向上 → 目标+15%~+30%, 止损-7%")
        print(f"    🔄 回踩池: 50日内创新高 + 回踩MA20/50 + 缩量 → 目标+10%~+15%, 止损-5%")
        print(f"    评分≥80=A(强烈), ≥65=B(可操作), ≥50=C(观察)")
        print(f"  {'─' * 76}")
    else:
        print(f"\n  ℹ️  多策略池(突破/趋势/回踩)今日无符合条件的股票。")

    # ═══════════════════════════════════════════════════════════
    # 🔫 BREAKOUT CONFIRMED — TODAY'S ACTION LIST
    # ═══════════════════════════════════════════════════════════
    if breakout_confirmed:
        print(f"\n{'🔫' * 40}")
        print(f"  🔫🔫🔫 【今日突破确认 — 直接买入】 🔫🔫🔫")
        print(f"{'🔫' * 40}")
        print(f"  以下 {len(breakout_confirmed)} 只股票今日出现突破K线(+{bcfg['min_pct_chg_breakout']:.0f}%阳线 + {bcfg['min_vol_ratio_breakout']:.1f}x量比):")
        print()
        for i, bo in enumerate(breakout_confirmed, 1):
            wait_str = f"(信号后第{bo['wait_days']}天)" if bo['wait_days'] > 0 else "(当日突破)"
            print(f"  {i}. 🔫 {bo['name']}({bo['ts_code']})  {bo['sector']}")
            print(f"     信号日: {bo['signal_date']}  信号价: {bo['signal_close']}")
            print(f"     突破日: {bo['breakout_date']}  突破价: {bo['breakout_close']}  {wait_str}")
            print(f"     今日涨幅: +{bo['breakout_pct_chg']:.1f}%  量比: {bo['breakout_vol_ratio']:.1f}x")
            print(f"     评分: {bo['score']}  信号: {bo['dyn_signal']}  集中度: {bo['chip_conc']}  获利比: {bo['winner']}")
            # Calculate stop/target based on breakout close
            bo_close = bo['breakout_close']
            print(f"     🎯 入场价: {bo_close}  止盈+10%: {bo_close*1.10:.2f}  止损-7%: {bo_close*0.93:.2f}")
            print()
    else:
        print(f"\n  ℹ️  今日无突破确认信号。")

    # ═══════════════════════════════════════════════════════════
    # 👀 SIGNAL POOL — WAITING FOR BREAKOUT
    # ═══════════════════════════════════════════════════════════
    if still_waiting:
        print(f"\n{'─' * 80}")
        print(f"  👀 【信号池 — 等待突破确认】({len(still_waiting)} 只)")
        print(f"{'─' * 80}")
        for i, sw in enumerate(still_waiting, 1):
            sig_dt = datetime.strptime(sw['signal_date'], '%Y%m%d')
            age = (today_dt - sig_dt).days
            remain = max_age - age
            print(f"  {i}. {sw['name']}({sw['ts_code']})  信号日:{sw['signal_date']}  "
                  f"信号价:{sw['signal_close']}  评分:{sw['score']}  "
                  f"剩余{remain}天  {sw['dyn_signal']}")
        print(f"{'─' * 80}")
        print(f"  ⏳ 以上股票已通过筹码筛选，等待突破K线出现(+{bcfg['min_pct_chg_breakout']:.0f}%阳线+{bcfg['min_vol_ratio_breakout']:.1f}x量比)")
        print(f"  ⏳ 超过{max_age}天未突破将自动移除")

    if not results and not breakout_confirmed:
        print(f'\n[DONE] 今日无新信号，信号池也无突破确认。')
        print('  建议：耐心等待，吸筹是慢过程，不是每天都有信号。')
        sys.stdout = _orig_stdout
        save_results_txt(_output_buf, trade_date, prefix='chip_v3')
        _output_buf.close()
        return None

    if not results:
        # No new Layer 3 results, but have breakout confirmations from pool
        print(f'\n[INFO] 今日无新筹码信号，但信号池中有 {len(breakout_confirmed)} 只突破确认。')
        sys.stdout = _orig_stdout
        save_results_txt(_output_buf, trade_date, prefix='chip_v3')
        _output_buf.close()
        return None

    result_df = pd.DataFrame(results)
    result_df = result_df.sort_values('score', ascending=False)

    # --- Score emoji helper: 🟢 ≥70%, 🟡 50-70%, 🔴 <50% ---
    _SCORE_THRESHOLDS = {
        's_vol':  (20, 14, 10),   # (max, green_min, yellow_min)
        's_conc': (25, 18, 13),
        's_drift':(25, 18, 13),
        's_wr':   (15, 11, 8),
        's_peak': (15, 11, 8),
        # 's_vcp':  (10, 5, 1),  # REMOVED — VCP proven negative
        's_ma':   (15, 8, 0),     # bonus: >=8 green, 0-7 yellow, <0 red
        's_dyn':  (15, 10, 5),    # dynamic precision bonus
    }
    def _score_emoji(key, val):
        """Return emoji for a score dimension."""
        if key not in _SCORE_THRESHOLDS:
            return ''
        _, g, y = _SCORE_THRESHOLDS[key]
        if val >= g:
            return '🟢'
        elif val >= y:
            return '🟡'
        else:
            return '🔴'

    def _fmt_score_col(df, col):
        """Format a score column with emoji suffix for table display."""
        if col not in _SCORE_THRESHOLDS:
            return df[col]
        return df[col].apply(lambda v: f"{int(v)}{_score_emoji(col, v)}")

    # By sector
    print(f'\n[RESULT] 找到 {len(result_df)} 只"正在吸筹"股票:\n')

    for sector in STOCK_POOL.keys():
        sector_df = result_df[result_df['sector'] == sector]
        if sector_df.empty:
            continue
        print(f"\n  === {sector} ({len(sector_df)}只) ===")
        display_cols = ['ts_code', 'name', 'close', 'pct_chg', 'total_mv_yi',
                        'dyn_signal', 'conc_change', 'drift_pct',
                        'chip_conc', 'winner', 'quiet_streak', 'recovery_ratio',
                        'score', 's_vol', 's_conc', 's_drift', 's_ma', 's_dyn', 's_wr', 's_peak']
        col_names = {
            'ts_code': '代码', 'name': '名称', 'close': '价格',
            'pct_chg': '涨跌%', 'total_mv_yi': '市值亿',
            'dyn_signal': '动态信号', 'conc_change': '集中度Δ', 'drift_pct': '重心漂移',
            'chip_conc': '集中度', 'winner': '获利比',
            'quiet_streak': '地量天', 'recovery_ratio': '量恢复',
            'score': '总分', 's_vol': '量能', 's_conc': '收敛',
            's_drift': '漂移', 's_ma': '均线', 's_dyn': '精度', 's_wr': '获利', 's_peak': '峰位',
        }
        sector_show = sector_df[display_cols].copy()
        for sc in ['s_vol', 's_conc', 's_drift', 's_ma', 's_dyn', 's_wr', 's_peak']:
            if sc in sector_show.columns:
                sector_show[sc] = _fmt_score_col(sector_show, sc)
        print(sector_show.rename(columns=col_names).to_string(index=False))

    # Total ranking
    print(f"\n\n  {'=' * 70}")
    print(f"  === 【总分排行榜】Top {len(result_df)} ===")
    print(f"  {'=' * 70}")
    rank_cols = ['ts_code', 'name', 'sector', 'close', 'dyn_signal',
                 'conc_change', 'drift_pct', 'winner',
                 'quiet_streak', 'recovery_ratio',
                 'score', 's_vol', 's_conc', 's_drift', 's_ma', 's_dyn', 's_wr', 's_peak']
    rank_names = {
        'ts_code': '代码', 'name': '名称', 'sector': '板块',
        'close': '价格', 'dyn_signal': '动态信号',
        'conc_change': '集中度Δ', 'drift_pct': '重心漂移',
        'winner': '获利比', 'quiet_streak': '地量天', 'recovery_ratio': '量恢复',
        'score': '总分', 's_vol': '量能', 's_conc': '收敛',
        's_drift': '漂移', 's_ma': '均线', 's_dyn': '精度', 's_wr': '获利', 's_peak': '峰位',
    }
    rank_df = result_df.sort_values('score', ascending=False).reset_index(drop=True)
    rank_df.index = rank_df.index + 1
    rank_df.index.name = '排名'
    rank_show = rank_df[rank_cols].copy()
    for sc in ['s_vol', 's_conc', 's_drift', 's_ma', 's_dyn', 's_wr', 's_peak']:
        if sc in rank_show.columns:
            rank_show[sc] = _fmt_score_col(rank_show, sc)
    print(rank_show.rename(columns=rank_names).to_string())

    # Detailed analysis per stock
    print(f"\n\n  {'=' * 70}")
    print(f"  === 【逐票深度分析】===")
    print(f"  {'=' * 70}")

    signal_df = result_df.sort_values(['priority', 'score'], ascending=[True, False])

    for _, r in signal_df.iterrows():
        print(f"\n  {r['signal']}  {r['name']}({r['ts_code']})  现价:{r['close']}  总分:{r['score']}")
        print(f"    市值:{r['total_mv_yi']}亿  换手:{r['turnover']}%  涨跌:{r['pct_chg']}%  "
              f"量比:{r['vol_ratio'] if r['vol_ratio'] is not None else 'N/A'}")

        print(f"    📊 动态信号: {r['dyn_signal']}")
        print(f"       集中度: {r['conc_oldest']} → {r['conc_newest']} (Δ{r['conc_change']})")
        print(f"       重心漂移: {r['drift_pct']} ({r['drift_dir']})")
        print(f"       获利速率: {r['winner_vel']}")

        print(f"    📈 量能行为: 地量{r['quiet_streak']}天 → 恢复{r['recovery_ratio']:.0%} 节奏{'好' if r['vol_rhythm'] == '✓' else '差'}")

        print(f"    🎯 筹码: 集中度{r['chip_conc']} | 获利比{r['winner']} | 中位成本{r['med_cost']} | "
              f"峰数{r['peaks']} | 主峰{r['main_peak']}")

        snap_mode_str = '快(20天)' if r.get('snap_mode', 'fast') == 'fast' else '慢(40天)'
        print(f"    📊 快照模式: {snap_mode_str}")
        if r.get('range_demote'):
            print(f"    ⚠️  高位横盘豁免: 突破回踩(涨幅<5%), 降分不淘汰")

        ma_bonus = r.get('s_ma', 0)
        ma_desc_str = r.get('ma_desc', '')
        e_vol = _score_emoji('s_vol', r['s_vol'])
        e_conc = _score_emoji('s_conc', r['s_conc'])
        e_drift = _score_emoji('s_drift', r['s_drift'])
        e_wr = _score_emoji('s_wr', r['s_wr'])
        e_peak = _score_emoji('s_peak', r['s_peak'])
        e_ma = _score_emoji('s_ma', ma_bonus)
        ma_display = f" | 均线{ma_bonus:+d}{e_ma}({ma_desc_str})" if ma_desc_str else f" | 均线{ma_bonus:+d}{e_ma}"
        dyn_bonus = r.get('s_dyn', 0)
        dyn_details_str = r.get('dyn_details', '')
        e_dyn = _score_emoji('s_dyn', dyn_bonus)
        dyn_display = f" | 精度{dyn_bonus:+d}{e_dyn}({dyn_details_str})" if dyn_details_str else f" | 精度{dyn_bonus:+d}{e_dyn}"
        print(f"    📝 评分: 量能{r['s_vol']}/20{e_vol} | 收敛{r['s_conc']}/25{e_conc} | "
              f"漂移{r['s_drift']}/25{e_drift} | 获利{r['s_wr']}/15{e_wr} | 峰位{r['s_peak']}/15{e_peak}{ma_display}{dyn_display}")

        if r['sig_detail']:
            print(f"    ✅ 利好: {' | '.join(r['sig_detail'])}")
        if r['sig_warn']:
            print(f"    ⚠️  注意: {' | '.join(r['sig_warn'])}")

        print(f"    📋 操作: {r['action']}")
        bo_close = r['close']
        print(f"    🎯 目标: +10%卖1/3 {bo_close*1.10:.2f} | +25%二目标 {bo_close*1.25:.2f}")
        print(f"    🛑 止损: -7%硬止损 {bo_close*0.93:.2f} | 止盈后移至成本(不败锁定)")

    # Signal summary
    green_count = len(result_df[result_df['signal'].str.contains('信号确认|接近')])
    yellow_count = len(result_df[result_df['signal'].str.contains('观望|早期')])
    red_count = len(result_df[result_df['signal'].str.contains('等待')])

    print(f"\n  {'─' * 70}")
    print(f"  信号汇总: 🟢信号确认/接近 {green_count}只 | 🟡观望/早期 {yellow_count}只 | 🔴等待 {red_count}只")
    print(f"  {'─' * 70}")

    # Market stage reminder
    max_pos_pct = stage_info['max_position'] * 100
    max_picks = stage_info.get('max_picks', 3)
    stage_icon = {'GREEN': '🟢', 'YELLOW': '🟡', 'ORANGE': '🟠', 'RED': '🔴'}.get(stage_info['stage'], '⚪')

    print(f"\n{'-' * 80}")
    print(f"[市场环境] {stage_icon} {stage_info['label']}")
    print(f"  沪深300: {stage_info['price']} | MA200: {stage_info['ma200']} | 偏离: {stage_info['price_vs_ma']}")
    ma60 = stage_info.get('ma60', 0)
    drawdown = stage_info.get('drawdown_pct', 0)
    temp = stage_info.get('market_temp', 0)
    if ma60:
        print(f"  MA60: {ma60} | 距60日高点: {drawdown:+.1f}% | 市场温度: {temp:.0%}")
    print(f"  当前最大仓位限制: {max_pos_pct:.0f}% | 每日最多: {max_picks}枪")

    if stage_info['stage'] == 'ORANGE':
        print(f"  ⚠️  中期走弱，仅允许1枪试探，严格止损！")
    elif stage_info['stage'] == 'YELLOW':
        print(f"  ⚠️  市场回调/走平中，减频操作，不追高！")

    # Discipline reminder
    print(f"\n{'-' * 80}")
    print('[纪律提醒 — v3.0 统一筹码战法 + 突破确认]')
    if max_pos_pct < 100:
        print(f'  0. ⚠️  当前市场环境限制最大仓位 {max_pos_pct:.0f}%')
    print('  1. �突破确认 = 信号池中的股票今日出现+1%阳线+1.3x量比 → 直接买入！')
    print('  2. �信号池   = 通过筹码筛选但尚未突破，系统自动跟踪，无需盯盘')
    print('  3. ⏳有效期   = 信号发出后4天内未突破自动移除，不追过期信号')
    print('  4. 止损铁律: 跌破硬止损(-7%)无条件出局')
    print('  5. 止盈规则: +10%卖1/3→止损移成本(不败锁定)→MA10/21追踪剩余2/3')
    print('  6. 追踪出场: 30天内用MA10，30天后切MA21，收盘破线清仓')
    print('  7. 横盘纪律: 10天横盘+趋势走弱出场，25天横盘强制出场')
    print('  8. 最长持仓: 35天强制出场')
    print('  9. 核心逻辑: 筹码收敛选股 → 突破确认入场 → 让赢家跑远')
    print(f"{'-' * 80}")

    # Save txt
    sys.stdout = _orig_stdout
    save_results_txt(_output_buf, trade_date, prefix='chip_v3')
    _output_buf.close()

    # Save CSV ranking
    try:
        results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'results')
        os.makedirs(results_dir, exist_ok=True)
        td_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}" if trade_date else 'unknown'
        csv_path = os.path.join(results_dir, f'chip_v3_{td_fmt}.csv')

        csv_cols = [
            'ts_code', 'name', 'sector', 'close', 'pct_chg', 'total_mv_yi',
            'signal', 'dyn_signal', 'score',
            's_vol', 's_conc', 's_drift', 's_wr', 's_peak', 's_ma', 's_dyn', 'dyn_details',
            'conc_change', 'drift_pct', 'winner', 'chip_conc',
            'quiet_streak', 'recovery_ratio',
            'med_cost', 'peaks', 'main_peak',
            'target_1', 'target_2', 'stop_7',
            'action',
        ]
        csv_names = {
            'ts_code': '代码', 'name': '名称', 'sector': '板块',
            'close': '现价', 'pct_chg': '涨跌%', 'total_mv_yi': '市值亿',
            'signal': '信号', 'dyn_signal': '动态信号', 'score': '总分',
            's_vol': '量能分', 's_conc': '收敛分', 's_drift': '漂移分',
            's_wr': '获利分', 's_peak': '峰位分', 's_ma': '均线分',
            's_dyn': '精度分', 'dyn_details': '精度明细',
            'conc_change': '集中度Δ', 'drift_pct': '重心漂移',
            'winner': '获利比', 'chip_conc': '集中度',
            'quiet_streak': '地量天', 'recovery_ratio': '量恢复',
            'med_cost': '中位成本', 'peaks': '峰数', 'main_peak': '主峰价',
            'target_1': '+10%卖1/3', 'target_2': '+25%二目标',
            'stop_7': '硬止损-7%',
            'action': '操作建议',
        }
        csv_df = result_df.sort_values('score', ascending=False).reset_index(drop=True)
        csv_df.index = csv_df.index + 1
        csv_df.index.name = '排名'
        # Only keep columns that exist
        csv_cols_exist = [c for c in csv_cols if c in csv_df.columns]
        csv_df[csv_cols_exist].rename(columns=csv_names).to_csv(csv_path, encoding='utf-8-sig')
        print(f"  📊 排行榜CSV已保存: {csv_path}")
    except Exception as e:
        print(f"  ⚠️  CSV保存失败: {e}")

    return result_df


# ============================================================
# Entry point
# ============================================================
if __name__ == "__main__":
    total_stocks = sum(len(v) for v in STOCK_POOL.values())
    print(f">>> 统一筹码选股器 v3.0")
    print(f"    票池: {total_stocks} stocks across {len(STOCK_POOL)} sectors")
    print(f"    模式: 量能行为扫描 → 动态筹码收敛 → 临界点评分")
    print(f"    核心: 从量能异动倒推吸筹行为")
    run_chip_screen_v3()
