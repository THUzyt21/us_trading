# -*- coding: utf-8 -*-
"""
chip_common.py — Shared utilities for chip screeners
=====================================================
Extracted from chip_pool_screener.py (v1.3) and chip_dynamic_screener.py (v2.0).

Contains:
  - Local cache system (load / save / incremental update)
  - Name resolution (STOCK_POOL names → ts_codes)
  - Market stage detection (Weinstein MA200 switch)
  - Chip distribution core algorithm
  - Moneyflow filter
  - API helpers
  - TeeWriter for dual output
  - Trade date utilities

Author: 龙虾 x 老哥
Date: 2026-04-19
"""
import sys
import io
import os
import time
import pickle
import numpy as np
import pandas as pd
import tushare as ts
from datetime import datetime, timedelta

import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from config import TUSHARE_TOKEN, STOCK_POOL, NAME_FIXES, HARDCODED_CODES

# ============================================================
# Constants
# ============================================================
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cache')
CACHE_FILE = os.path.join(CACHE_DIR, 'chip_pool_history.pkl')
HS300_CODE = '000300.SH'

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


# ============================================================
# API helper
# ============================================================
def api_call_with_retry(fn, *args, max_retry=3, **kwargs):
    """API call with rate-limit retry."""
    for attempt in range(max_retry):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if '每分钟' in msg or 'limit' in msg.lower() or '800' in msg:
                wait = 65
                print(f"  [RATE LIMIT] API 限速，等待 {wait}s 后重试 (attempt {attempt+1}/{max_retry})...")
                time.sleep(wait)
            else:
                raise
    raise Exception(f"API 调用失败，已重试 {max_retry} 次")


# ============================================================
# TeeWriter (dual output: terminal + buffer)
# ============================================================
class TeeWriter:
    """Write to both terminal and buffer simultaneously."""
    def __init__(self, *writers):
        self.writers = writers
    def write(self, s):
        for w in self.writers:
            w.write(s)
    def flush(self):
        for w in self.writers:
            w.flush()


# ============================================================
# Local cache: load / save / incremental update
# ============================================================
def load_cache():
    """Load history data cache from local pickle, returns {ts_code: DataFrame} or {}."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, 'rb') as f:
            data = pickle.load(f)
        print(f"  [CACHE] Loaded {len(data)} stocks from local cache")
        return data
    except Exception as e:
        print(f"  [CACHE] Load failed ({e}), will rebuild from scratch")
        return {}


def save_cache(cache_data):
    """Save history data cache to local pickle."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(cache_data, f)
    print(f"  [CACHE] Saved {len(cache_data)} stocks to {CACHE_FILE}")


def _get_trade_dates_between(pro, start_date, end_date):
    """Get all trade dates between two dates (ascending)."""
    try:
        cal = pro.trade_cal(
            exchange='SSE',
            start_date=start_date,
            end_date=end_date,
            fields='cal_date,is_open'
        )
        if cal is not None and not cal.empty:
            return sorted(cal[cal['is_open'] == 1]['cal_date'].tolist())
    except Exception:
        pass
    return []


def get_recent_trade_dates(pro, trade_date, n=5):
    """Get the most recent n trade dates up to and including trade_date (ascending)."""
    start = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=n * 3)).strftime('%Y%m%d')
    dates = _get_trade_dates_between(pro, start, trade_date)
    return dates[-n:] if len(dates) >= n else dates


def update_cache_incremental(cache_data, ts_codes, trade_date, pro):
    """
    Incremental cache update (batch by date, not per-stock):

    Strategy:
    1. Find the earliest gap date across all cached stocks
    2. Batch-pull full market daily data for each missing date
    3. For uncached stocks, do a full per-stock pull (first run only)

    Returns {ts_code: DataFrame} with daily data each.
    NOTE: no longer truncates to 250 rows — keeps all available history.
    """
    ts_code_set = set(ts_codes)

    # Step A: classify stocks
    need_full = []
    need_incremental = {}
    from_cache = 0

    for ts_code in ts_codes:
        if ts_code not in cache_data or cache_data[ts_code].empty:
            need_full.append(ts_code)
        else:
            last_date = cache_data[ts_code]['trade_date'].max()
            if last_date > trade_date:
                from_cache += 1
            else:
                need_incremental[ts_code] = last_date

    print(f"  [CACHE STATUS] 直接用缓存={from_cache} | 需增量={len(need_incremental)} | 需全量={len(need_full)}")

    # Step B: batch pull incremental data by date
    batch_daily_cache = {}

    if need_incremental:
        earliest_missing = min(
            (datetime.strptime(d, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
            for d in need_incremental.values()
        )
        print(f"  [BATCH] 需补数据日期范围: {earliest_missing} ~ {trade_date}")

        missing_trade_dates = _get_trade_dates_between(pro, earliest_missing, trade_date)
        print(f"  [BATCH] 共 {len(missing_trade_dates)} 个交易日需要拉取，开始批量下载...")

        for idx, td in enumerate(missing_trade_dates):
            for attempt in range(3):
                try:
                    df_day = pro.daily(
                        trade_date=td,
                        fields='ts_code,trade_date,open,high,low,close,vol'
                    )
                    if df_day is not None and not df_day.empty:
                        batch_daily_cache[td] = df_day
                    break
                except Exception as e:
                    msg = str(e)
                    if '每分钟' in msg or '800' in msg or 'limit' in msg.lower():
                        print(f"  [RATE LIMIT] 批量拉取限速，等待 65s... (td={td})")
                        time.sleep(65)
                    else:
                        print(f"  [WARN] 拉取 {td} 失败: {e}，跳过")
                        break
            if (idx + 1) % 10 == 0:
                print(f"    ... {idx+1}/{len(missing_trade_dates)} 天已拉取")
            time.sleep(0.2)

        print(f"  [BATCH] 批量下载完成，共获取 {len(batch_daily_cache)} 天数据")

    # Step C: full pull for uncached stocks (first run only)
    full_pulled = 0
    api_calls_full = 0
    # Pull up to 750 trading days (~3 years) of history for uncached stocks
    start_full = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=1200)).strftime('%Y%m%d')

    if need_full:
        print(f"  [FULL PULL] 首次拉取 {len(need_full)} 只无缓存股票（逐票，仅首次）...")
        for i, ts_code in enumerate(need_full):
            if (i + 1) % 50 == 0:
                print(f"    ... {i+1}/{len(need_full)} 全量拉取中")
            try:
                hist = pro.daily(
                    ts_code=ts_code,
                    start_date=start_full,
                    end_date=trade_date,
                    fields='trade_date,open,high,low,close,vol'
                )
                api_calls_full += 1
                if api_calls_full % 80 == 0:
                    print(f"  [RATE LIMIT] Pausing 62s... (calls={api_calls_full})")
                    time.sleep(62)
                elif api_calls_full % 5 == 0:
                    time.sleep(0.3)

                if hist is not None and not hist.empty:
                    cache_data[ts_code] = hist.sort_values('trade_date').reset_index(drop=True)
                    full_pulled += 1
            except Exception as e:
                if 'too fast' in str(e).lower() or 'limit' in str(e).lower():
                    print(f"  [RATE LIMIT HIT] Sleeping 90s...")
                    time.sleep(90)
                    try:
                        hist = pro.daily(
                            ts_code=ts_code,
                            start_date=start_full,
                            end_date=trade_date,
                            fields='trade_date,open,high,low,close,vol'
                        )
                        api_calls_full += 1
                        if hist is not None and not hist.empty:
                            cache_data[ts_code] = hist.sort_values('trade_date').reset_index(drop=True)
                            full_pulled += 1
                    except Exception:
                        pass

    # Step D: merge incremental data into cache
    updated = {}
    incremental_merged = 0

    for ts_code in ts_codes:
        if ts_code in need_incremental:
            last_date = need_incremental[ts_code]
            cached_df = cache_data[ts_code].copy()

            new_rows = []
            for td, df_day in batch_daily_cache.items():
                if td >= last_date:
                    stock_row = df_day[df_day['ts_code'] == ts_code]
                    if not stock_row.empty:
                        new_rows.append(stock_row)

            if new_rows:
                new_df = pd.concat(new_rows, ignore_index=True)
                merged = pd.concat([cached_df, new_df], ignore_index=True)
                merged = merged.drop_duplicates(subset='trade_date').sort_values('trade_date').reset_index(drop=True)
                updated[ts_code] = merged
            else:
                updated[ts_code] = cached_df
            incremental_merged += 1

        elif ts_code in cache_data and not cache_data[ts_code].empty:
            updated[ts_code] = cache_data[ts_code]

    # Add full-pulled stocks
    for ts_code in need_full:
        if ts_code in cache_data:
            updated[ts_code] = cache_data[ts_code]

    total_api = len(batch_daily_cache) + api_calls_full + 1
    print(f"  [CACHE SUMMARY] from_cache={from_cache} | incremental={incremental_merged} | "
          f"full_pull={full_pulled} | API调用总计≈{total_api}次 (批量日期={len(batch_daily_cache)}次)")

    return updated


# ============================================================
# Name → ts_code resolution
# ============================================================
def build_name_to_code_map(pro):
    """Build full A-share name→ts_code mapping from tushare."""
    df = pro.stock_basic(fields='ts_code,name')
    return dict(zip(df['name'], df['ts_code']))


def resolve_pool_to_codes(pro):
    """Resolve STOCK_POOL Chinese names to ts_codes, preserving sector tags."""
    name_map = build_name_to_code_map(pro)

    code_list = []   # [(ts_code, name, sector), ...]
    not_found = []

    for sector, names in STOCK_POOL.items():
        for name in names:
            if name in HARDCODED_CODES:
                code_list.append((HARDCODED_CODES[name], name, sector))
                continue

            fixed_name = NAME_FIXES.get(name, name)

            if fixed_name in name_map:
                code_list.append((name_map[fixed_name], name, sector))
            elif name in name_map:
                code_list.append((name_map[name], name, sector))
            else:
                not_found.append((name, sector))

    return code_list, not_found


# ============================================================
# Chip distribution core algorithm
# ============================================================
def count_chip_peaks(dist, config):
    """Detect number of significant peaks in chip distribution."""
    window = max(3, len(dist) // 20)
    smoothed = np.convolve(dist, np.ones(window) / window, mode='same')

    peaks = []
    for i in range(1, len(smoothed) - 1):
        if smoothed[i] > smoothed[i - 1] and smoothed[i] > smoothed[i + 1]:
            peaks.append((i, smoothed[i]))

    if not peaks:
        return 0

    peaks.sort(key=lambda x: x[1], reverse=True)
    main_peak_height = peaks[0][1]
    significant_peaks = [p for p in peaks if p[1] >= main_peak_height * config['peak_prominence_ratio']]
    return len(significant_peaks)


def compute_chip_distribution(df_hist, current_price, config):
    """
    Compute chip distribution from historical OHLCV data.

    Uses decay-weighted volume distribution across price bins.
    Vectorized with NumPy for performance (no iterrows).
    Returns dict with concentration, winner_ratio, median_cost, peaks, etc.
    """
    if df_hist.empty or len(df_hist) < 20:
        return None

    all_low = df_hist['low'].min() * 0.95
    all_high = df_hist['high'].max() * 1.05
    n_bins = config['price_bins']
    price_edges = np.linspace(all_low, all_high, n_bins + 1)
    price_centers = (price_edges[:-1] + price_edges[1:]) / 2

    decay = config['decay_factor']

    # Extract arrays (much faster than iterrows)
    highs = df_hist['high'].values.astype(np.float64)
    lows = df_hist['low'].values.astype(np.float64)
    closes = df_hist['close'].values.astype(np.float64)
    vols = df_hist['vol'].values.astype(np.float64)
    n_rows = len(highs)

    # Pre-compute decay powers: newest row gets decay^0=1, oldest gets decay^(n-1)
    # We process oldest→newest, accumulating chip_dist = chip_dist * decay + day_dist
    chip_dist = np.zeros(n_bins)

    # Vectorized: build mask matrix (n_rows x n_bins) for which bins are active
    # price_centers is (n_bins,), lows/highs are (n_rows,)
    pc = price_centers[np.newaxis, :]  # (1, n_bins)
    lo = lows[:, np.newaxis]           # (n_rows, 1)
    hi = highs[:, np.newaxis]          # (n_rows, 1)
    cl = closes[:, np.newaxis]         # (n_rows, 1)

    # Valid rows: vol > 0 and high > low
    valid = (vols > 0) & (highs > lows)

    # Active bins per row: price_center in [low, high]
    active_mask = (pc >= lo) & (pc <= hi)  # (n_rows, n_bins)

    # Distance from close for each bin
    distances = np.abs(pc - cl)  # (n_rows, n_bins)

    # Max distance per row (only among active bins)
    # Set inactive bins to 0 distance to avoid affecting max
    dist_active = np.where(active_mask, distances, 0.0)  # (n_rows, n_bins)
    max_dists = dist_active.max(axis=1, keepdims=True)    # (n_rows, 1)
    max_dists = np.where(max_dists > 0, max_dists, 1.0)

    # Weights: 1 - 0.5 * (dist / max_dist), only for active bins
    raw_weights = 1.0 - 0.5 * (distances / max_dists)  # (n_rows, n_bins)
    raw_weights = np.where(active_mask, raw_weights, 0.0)

    # Normalize weights per row
    row_sums = raw_weights.sum(axis=1, keepdims=True)  # (n_rows, 1)
    row_sums = np.where(row_sums > 0, row_sums, 1.0)
    norm_weights = raw_weights / row_sums

    # Day distributions: vol * normalized_weights
    day_dists = vols[:, np.newaxis] * norm_weights  # (n_rows, n_bins)

    # Zero out invalid rows
    day_dists[~valid] = 0.0

    # Accumulate with decay: chip_dist = chip_dist * decay + day_dist (oldest first)
    for i in range(n_rows):
        chip_dist = chip_dist * decay + day_dists[i]

    total_chips = chip_dist.sum()
    if total_chips <= 0:
        return None
    chip_dist_norm = chip_dist / total_chips

    # Winner ratio
    winner_mask = price_centers <= current_price
    winner_ratio = chip_dist_norm[winner_mask].sum()

    # Median cost (50th percentile)
    cum_dist = np.cumsum(chip_dist_norm)
    median_idx = np.searchsorted(cum_dist, 0.50)
    median_cost = price_centers[min(median_idx, len(price_centers) - 1)]

    # Average cost
    avg_cost = np.sum(price_centers * chip_dist_norm)

    # 90% concentration
    p5_idx = np.searchsorted(cum_dist, 0.05)
    p95_idx = np.searchsorted(cum_dist, 0.95)
    p5_price = price_centers[min(p5_idx, len(price_centers) - 1)]
    p95_price = price_centers[min(p95_idx, len(price_centers) - 1)]
    concentration_90 = (p95_price - p5_price) / median_cost if median_cost > 0 else 999

    # Cost deviation
    cost_deviation = abs(current_price - median_cost) / median_cost if median_cost > 0 else 999

    # Peak count
    peak_count = count_chip_peaks(chip_dist_norm, config)

    # Main peak price (highest density bin)
    main_peak_idx = np.argmax(chip_dist_norm)
    main_peak_price = price_centers[main_peak_idx]

    return {
        'concentration_90': round(concentration_90, 4),
        'winner_ratio': round(winner_ratio, 4),
        'median_cost': round(median_cost, 2),
        'avg_cost': round(avg_cost, 2),
        'cost_deviation': round(cost_deviation, 4),
        'peak_count': peak_count,
        'main_peak_price': round(main_peak_price, 2),
        'p5_price': round(p5_price, 2),
        'p95_price': round(p95_price, 2),
        'chip_dist_norm': chip_dist_norm,
        'price_centers': price_centers,
    }


# ============================================================
# Market stage detection (Weinstein MA200 switch)
# ============================================================
def _compute_volume_metrics(closes, amounts):
    """
    Compute volume-based market metrics.

    Returns:
      vol_ratio: 5-day avg amount / 20-day avg amount (volume strength)
      diverge_pct: % of last 20 days with price-volume divergence
    """
    vol_ratio = 1.0
    diverge_pct = 0.0

    if amounts is not None and len(amounts) >= 20 and len(closes) >= 20:
        avg5 = np.mean(amounts[-5:])
        avg20 = np.mean(amounts[-20:])
        vol_ratio = avg5 / avg20 if avg20 > 0 else 1.0

        # Price-volume divergence: count days where price and volume disagree
        # "price down + volume up" or "price up + volume down"
        diverge_days = 0
        for i in range(-19, 0):
            price_up = closes[i] > closes[i - 1]
            vol_up = amounts[i] > amounts[i - 1]
            if price_up != vol_up:
                diverge_days += 1
        diverge_pct = diverge_days / 19.0  # 19 comparisons in 20 days

    return vol_ratio, diverge_pct


def _decide_market_stage(ma200_dir, ma60_dir, price_above_200, price_above_60,
                         drawdown_pct, market_temp, vol_ratio, diverge_pct, up_days,
                         profit_ratio=None):
    """
    Simple 3-tier speed limiter based on profit effect (赚钱效应).

    The ONLY thing that matters for position sizing:
      - profit_ratio > 35% → GREEN: max 3 positions, unlimited shots
      - profit_ratio 20-35% → YELLOW: max 2 positions, 1 shot/day
      - profit_ratio < 20% → RED: STOP trading, 0 positions

    Hard RED overrides (always apply regardless of profit_ratio):
      - MA200 falling + price below MA200 → Stage 4 confirmed
      - Drawdown from 60d high > 10% → crash protection
    """
    # ── Hard RED: structural bear market or crash ──
    # Only apply Hard RED if PR is NOT strong (PR > 35% overrides structural bear)
    # This allows catching early reversals (like 2024-Sep rally)
    if profit_ratio is None or profit_ratio <= 0.35:
        if ma200_dir == 'falling' and not price_above_200:
            return 'RED', 'Stage 4 下跌期 | MA200↓ 股价在下方', 0.0, 0

    # Crash protection: -10% drawdown is ALWAYS hard RED (no override)
    if drawdown_pct < -10:
        return 'RED', f'急跌保护 | 距60日高点{drawdown_pct:.1f}%', 0.0, 0

    # ── 3-tier profit effect speed limiter ──
    if profit_ratio is not None:
        # Build context label from MA/volume for display
        if ma200_dir == 'rising' and ma60_dir == 'rising':
            ctx = f'MA200↑ MA60↑ 量比{vol_ratio:.2f}'
        elif ma200_dir == 'rising':
            ctx = f'MA200↑ MA60{ma60_dir} 量比{vol_ratio:.2f}'
        else:
            ctx = f'MA200{ma200_dir} 量比{vol_ratio:.2f}'

        if profit_ratio > 0.35:
            return 'GREEN', f'🔥赚钱效应强 | 盈利股{profit_ratio:.0%} {ctx}', 1.0, 3
        elif profit_ratio >= 0.30:
            return 'YELLOW', f'⚡赚钱效应中 | 盈利股{profit_ratio:.0%} 精选出击(限1笔/天) {ctx}', 0.67, 1
        else:
            return 'RED', f'🧊赚钱效应冰点 | 盈利股{profit_ratio:.0%} 停手观望', 0.0, 0

    # ── Fallback: profit_ratio unavailable, use MA/volume logic ──
    if (ma200_dir == 'rising' and ma60_dir == 'rising'
            and price_above_200 and price_above_60
            and vol_ratio >= 0.8):
        return 'GREEN', f'Stage 2 上升期 | MA200↑ MA60↑ 量比{vol_ratio:.2f}', 1.0, 3
    elif ma200_dir == 'rising':
        return 'YELLOW', f'Stage 2 震荡 | MA200↑ 量比{vol_ratio:.2f}', 0.50, 2
    else:
        return 'ORANGE', f'趋势不明 | MA200{ma200_dir} 量比{vol_ratio:.2f}', 0.33, 1

def get_market_stage(pro, trade_date):
    """
    Determine market stage based on CSI 300 — 5-layer filter.

    Layer 1 (long-term):  MA200 direction — Weinstein stage
    Layer 2 (mid-term):   MA60 direction + price vs MA60
    Layer 3 (short-term): Drawdown from 60-day high
    Layer 4 (volume):     5d/20d turnover ratio — volume strength
    Layer 5 (health):     Price-volume divergence ratio

    Combined output:
      GREEN  = All clear, full position (3 picks/day)
      YELLOW = Caution, half position (1-2 picks/day)
      ORANGE = Weak, minimal position (1 pick/day)
      RED    = STOP, no trading (0 picks/day)
    """
    start_date = (datetime.strptime(trade_date, '%Y%m%d') - timedelta(days=400)).strftime('%Y%m%d')

    _default_err = {
        'stage': 'GREEN', 'label': '数据获取失败，默认开机',
        'max_position': 1.0, 'max_picks': 3,
        'ma200': 0, 'ma200_slope': 'unknown',
        'ma60': 0, 'ma60_slope': 'unknown',
        'price': 0, 'price_vs_ma': 'unknown',
        'drawdown_pct': 0, 'market_temp': 1.0,
        'vol_ratio': 1.0, 'diverge_pct': 0.0,
    }

    try:
        idx_df = pro.index_daily(
            ts_code=HS300_CODE,
            start_date=start_date,
            end_date=trade_date,
            fields='trade_date,close,amount'
        )
    except Exception as e:
        print(f"  [WARN] Cannot fetch CSI 300 data: {e}. Defaulting to GREEN.")
        return _default_err

    if idx_df is None or len(idx_df) < 210:
        print(f"  [WARN] CSI 300 data insufficient ({len(idx_df) if idx_df is not None else 0} rows). Defaulting to GREEN.")
        _default_err['label'] = '数据不足，默认开机'
        return _default_err

    idx_df = idx_df.sort_values('trade_date').reset_index(drop=True)
    idx_df['close'] = pd.to_numeric(idx_df['close'], errors='coerce')
    idx_df['amount'] = pd.to_numeric(idx_df['amount'], errors='coerce')
    idx_df = idx_df.dropna(subset=['close'])

    if len(idx_df) < 210:
        _default_err['label'] = '数据不足，默认开机'
        return _default_err

    closes = idx_df['close'].values
    amounts = idx_df['amount'].values if 'amount' in idx_df.columns else None
    current_price = closes[-1]

    # ── Layer 1: MA200 long-term direction ──
    ma200_today = np.mean(closes[-200:])
    ma200_20ago = np.mean(closes[-220:-20])
    ma200_change_pct = (ma200_today - ma200_20ago) / ma200_20ago * 100
    price_vs_ma200 = (current_price - ma200_today) / ma200_today * 100

    FLAT_THRESHOLD_200 = 0.5
    if ma200_change_pct > FLAT_THRESHOLD_200:
        ma200_dir = 'rising'
    elif ma200_change_pct < -FLAT_THRESHOLD_200:
        ma200_dir = 'falling'
    else:
        ma200_dir = 'flat'

    price_above_200 = current_price > ma200_today

    # ── Layer 2: MA60 mid-term direction ──
    ma60_today = np.mean(closes[-60:])
    ma60_20ago = np.mean(closes[-80:-20]) if len(closes) >= 80 else ma60_today
    ma60_change_pct = (ma60_today - ma60_20ago) / ma60_20ago * 100 if ma60_20ago > 0 else 0
    price_above_60 = current_price > ma60_today

    FLAT_THRESHOLD_60 = 0.3
    if ma60_change_pct > FLAT_THRESHOLD_60:
        ma60_dir = 'rising'
    elif ma60_change_pct < -FLAT_THRESHOLD_60:
        ma60_dir = 'falling'
    else:
        ma60_dir = 'flat'

    # ── Layer 3: Drawdown from 60-day high ──
    high_60 = float(np.max(closes[-60:])) if len(closes) >= 60 else float(np.max(closes[-20:]))
    drawdown_pct = (current_price - high_60) / high_60 * 100

    # ── Layer 3b: Market temperature ──
    recent_20 = closes[-20:] if len(closes) >= 20 else closes
    up_days = sum(1 for i in range(1, len(recent_20)) if recent_20[i] > recent_20[i - 1])
    market_temp = up_days / (len(recent_20) - 1) if len(recent_20) > 1 else 0.5

    # ── Layer 4 & 5: Volume strength + Price-volume divergence ──
    vol_ratio, diverge_pct = _compute_volume_metrics(closes, amounts)

    # ── Combined decision ──
    stage, label, max_pos, max_picks = _decide_market_stage(
        ma200_dir, ma60_dir, price_above_200, price_above_60,
        drawdown_pct, market_temp, vol_ratio, diverge_pct, up_days
    )

    return {
        'stage': stage,
        'label': label,
        'max_position': max_pos,
        'max_picks': max_picks,
        'ma200': round(ma200_today, 2),
        'ma200_slope': ma200_dir,
        'ma200_change_pct': round(ma200_change_pct, 2),
        'ma60': round(ma60_today, 2),
        'ma60_slope': ma60_dir,
        'ma60_change_pct': round(ma60_change_pct, 2),
        'price': round(current_price, 2),
        'price_vs_ma': f"{'↑' if price_above_200 else '↓'}{abs(price_vs_ma200):.1f}%",
        'price_deviation_pct': round(price_vs_ma200, 2),
        'drawdown_pct': round(drawdown_pct, 2),
        'market_temp': round(market_temp, 2),
        'vol_ratio': round(vol_ratio, 2),
        'diverge_pct': round(diverge_pct, 2),
    }


def print_market_stage(stage_info):
    """Print market stage banner with visual indicators."""
    stage = stage_info['stage']
    icons = {'GREEN': '🟢', 'YELLOW': '🟡', 'ORANGE': '🟠', 'RED': '🔴'}
    actions = {
        'GREEN': '策略全仓运行 (≤3枪/天)',
        'YELLOW': '半仓运行，减频 (≤2枪/天)',
        'ORANGE': '仅允许试探 (≤1枪/天)',
        'RED': '⚠️  策略停机！持币等待！(0枪)',
    }
    icon = icons.get(stage, '⚪')
    action = actions.get(stage, '未知')

    print(f"\n  {'━' * 60}")
    print(f"  {icon} 【市场环境开关】{stage_info['label']}")
    print(f"  {'━' * 60}")
    print(f"    沪深300: {stage_info['price']}  |  MA200: {stage_info['ma200']}  |  偏离: {stage_info['price_vs_ma']}")
    print(f"    MA200斜率: {stage_info['ma200_slope']} ({stage_info.get('ma200_change_pct', 0):+.2f}%/20日)")
    ma60 = stage_info.get('ma60', 0)
    ma60_slope = stage_info.get('ma60_slope', 'unknown')
    ma60_chg = stage_info.get('ma60_change_pct', 0)
    drawdown = stage_info.get('drawdown_pct', 0)
    temp = stage_info.get('market_temp', 0)
    max_picks = stage_info.get('max_picks', 3)
    vol_ratio = stage_info.get('vol_ratio', 1.0)
    diverge = stage_info.get('diverge_pct', 0)
    print(f"    MA60: {ma60}  |  MA60斜率: {ma60_slope} ({ma60_chg:+.2f}%/20日)")
    print(f"    距60日高点: {drawdown:+.1f}%  |  市场温度: {temp:.0%}")
    # Volume strength indicator
    if vol_ratio >= 1.2:
        vol_icon = '🔥'
        vol_desc = '放量活跃'
    elif vol_ratio >= 0.8:
        vol_icon = '✅'
        vol_desc = '量能正常'
    elif vol_ratio >= 0.6:
        vol_icon = '⚠️'
        vol_desc = '缩量警告'
    else:
        vol_icon = '🚨'
        vol_desc = '地量危险'
    print(f"    量能: {vol_icon} {vol_desc} (5d/20d={vol_ratio:.2f})  |  量价背离: {diverge:.0%}")
    print(f"    最大仓位: {stage_info['max_position']*100:.0f}%  |  每日最多: {max_picks}枪  |  操作: {action}")
    print(f"  {'━' * 60}")


# ============================================================
# Moneyflow filter
# ============================================================
def filter_by_moneyflow(pro, trade_date, candidate_codes):
    """
    Moneyflow health check on candidates (5-day lookback).

    Filters out:
      1. Single-day net outflow > 50M (暴力砸盘)
      2. 4+ days of net outflow in 5 days (钝刀割肉)
      3. 5-day cumulative net outflow > 100M (持续出货)

    Returns: (passed_codes, filtered_info)
    """
    if not candidate_codes:
        return [], []

    recent_dates = get_recent_trade_dates(pro, trade_date, n=5)
    if not recent_dates:
        print("  [WARN] 无法获取近期交易日，跳过资金面过滤")
        return list(candidate_codes), []

    print(f"  [MONEYFLOW] 拉取近 {len(recent_dates)} 个交易日资金流数据: {recent_dates[0]} ~ {recent_dates[-1]}")

    all_rows = []
    for td in recent_dates:
        df_day = None
        for attempt in range(3):
            try:
                df_day = pro.moneyflow(
                    trade_date=td,
                    fields='ts_code,trade_date,net_mf_amount'
                )
                break
            except Exception as e:
                msg = str(e)
                if '每分钟' in msg or '800' in msg or 'limit' in msg.lower():
                    print(f"  [RATE LIMIT] moneyflow 限速，等待 65s...")
                    time.sleep(65)
                else:
                    print(f"  [WARN] moneyflow {td} 拉取失败: {e}")
                    break
        if df_day is not None and not df_day.empty:
            all_rows.append(df_day)
        time.sleep(0.2)

    if not all_rows:
        print("  [WARN] moneyflow 数据全部为空，跳过资金面过滤")
        return list(candidate_codes), []

    mf_df = pd.concat(all_rows, ignore_index=True)
    mf_df['net_mf_amount'] = pd.to_numeric(mf_df['net_mf_amount'], errors='coerce').fillna(0)

    candidate_set = set(candidate_codes)
    mf_df = mf_df[mf_df['ts_code'].isin(candidate_set)]

    passed_codes = []
    filtered_info = []

    for code in candidate_codes:
        df_code = mf_df[mf_df['ts_code'] == code]

        if df_code.empty:
            passed_codes.append(code)
            continue

        net_vals = df_code['net_mf_amount'].values
        min_day = float(net_vals.min())
        total_net = float(net_vals.sum())
        green_days = int((net_vals < 0).sum())

        reason = None
        if min_day < -5000:
            reason = f"单日主力暴砸 {min_day/10000:.1f}亿"
        elif green_days >= 4:
            reason = f"近{len(net_vals)}天有{green_days}天净流出（钝刀割肉）"
        elif total_net < -10000:
            reason = f"5天累计净流出 {total_net/10000:.1f}亿"

        if reason:
            filtered_info.append({
                'ts_code': code,
                'reason': reason,
                'net_5d': round(total_net / 10000, 2),
                'min_day': round(min_day / 10000, 2),
                'green_days': green_days,
            })
        else:
            passed_codes.append(code)

    return passed_codes, filtered_info


# ============================================================
# Trade date finder
# ============================================================
def find_latest_trade_date(pro):
    """Find the latest trade date, returns (trade_date_str, is_today).
    
    If today is a trade day but daily data hasn't been ingested yet
    (e.g. market still open), fall back to the previous trade day that
    has actual data.
    """
    today = datetime.now()
    today_str = today.strftime('%Y%m%d')
    trade_date = None
    open_days = []

    try:
        start_cal = (today - timedelta(days=15)).strftime('%Y%m%d')
        cal_df = pro.trade_cal(exchange='SSE', start_date=start_cal, end_date=today_str,
                               fields='cal_date,is_open')
        if cal_df is not None and not cal_df.empty:
            open_days = sorted(cal_df[cal_df['is_open'] == 1]['cal_date'].tolist())
            valid_days = [d for d in open_days if d <= today_str]
            if valid_days:
                # Try from the most recent trade day backwards
                for candidate in reversed(valid_days):
                    try:
                        test_df = pro.daily(trade_date=candidate, limit=1)
                        if test_df is not None and not test_df.empty:
                            trade_date = candidate
                            break
                    except Exception:
                        continue
                if trade_date is None:
                    trade_date = valid_days[-1]  # fallback to calendar date
    except Exception as e:
        print(f"  [WARN] trade_cal failed: {e}")

    if trade_date is None:
        for i in range(30):
            test_date = (today - timedelta(days=i)).strftime('%Y%m%d')
            try:
                test_df = pro.daily(trade_date=test_date, limit=1)
                if test_df is not None and not test_df.empty:
                    trade_date = test_date
                    break
            except Exception:
                continue

    is_today = (trade_date == today_str) if trade_date else False
    return trade_date, is_today


# ============================================================
# Save results to txt
# ============================================================
def save_results_txt(output_buf, trade_date, prefix='chip_v3'):
    """Save captured output buffer to results/{prefix}_YYYY-MM-DD.txt"""
    try:
        results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'results')
        os.makedirs(results_dir, exist_ok=True)
        td_fmt = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}" if trade_date else 'unknown'
        txt_path = os.path.join(results_dir, f'{prefix}_{td_fmt}.txt')
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(output_buf.getvalue())
        print(f"\n  💾 结果已保存: {txt_path}")
    except Exception as e:
        print(f"\n  ⚠️  保存结果失败: {e}")
