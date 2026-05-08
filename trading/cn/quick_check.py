# -*- coding: utf-8 -*-
"""
quick_check.py — Fast breakout scan against chip_signal_pool.json
                 + chip_breakout_history.json.

Why this exists
---------------
`chip_screener_v3.py` needs Tushare EOD data (available ~16:00) just to tell
you "which signal-pool stock broke out today". That's too late. This script
reads the signal pool AND the breakout archive, then pings Tencent's public
realtime quote endpoint (https://qt.gtimg.cn/) with a single batched request.
Zero deps, runs in <1s.

Data sources
------------
- cache/chip_signal_pool.json        : stocks waiting for breakout
- cache/chip_breakout_history.json   : stocks already confirmed as breakouts
                                       (written by chip_screener_v3.py)
Merging both ensures that once the main screener moves a stock out of the
pending pool (because it broke out), quick_check still surfaces it for
follow-through monitoring in the days after.

Typical usage
-------------
  11:30 lunch break  ->  python quick_check.py                # auto: morning
  15:01 after close  ->  python quick_check.py                # auto: close
  force a session    ->  python quick_check.py --session morning
  strict (close only) ->  python quick_check.py --session close --require-green

Sessions & thresholds
---------------------
Intraday (morning / midday): tolerant, half-day data is naturally smaller.
  - gain vs signal_close >= 0.6%
  - price > open (green candle)
  - intraday new high since open is flagged separately
Close: matches chip_screener_v3.BREAKOUT_CONFIG.
  - gain vs signal_close >= 1.0%
  - volume ratio is NOT available via realtime quote; re-run the main
    screener after 16:00 for authoritative confirmation.

Volume ratio (量比) — verified 2026-04-22
----------------------------------------
Tencent field[49] (`vol_ratio`) is a "SAME-SESSION" ratio:
    numerator   = volume so far today
    denominator = average volume at the SAME elapsed time across the prev 5 days
So the ratio is already pace-normalized by Tencent. This means:
  * Intraday (11:30 / 13:30 / 14:30): value is directly comparable to
    chip_screener_v3's 1.3x bar without any /progress rescaling.
  * After close (15:00+): value degenerates to full-day vs 5d full-day avg,
    i.e. the authoritative EOD vol_ratio. Matches Tushare EOD daily_basic
    `volume_ratio` to within float rounding.
Empirically cross-checked against Tushare EOD for 4 stocks on 2026-04-22:
all four matched to 2 decimal places. Trust it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(PROJECT_DIR, 'cache')
SIGNAL_POOL_FILE = os.path.join(CACHE_DIR, 'chip_signal_pool.json')
STRATEGY_POOL_FILE = os.path.join(CACHE_DIR, 'strategy_signal_pool.json')
BREAKOUT_ARCHIVE_FILE = os.path.join(CACHE_DIR, 'chip_breakout_history.json')

# How many recent days of archived breakouts to show alongside the pending pool.
# Set to match the typical holding-window head you care about.
ARCHIVE_SHOW_DAYS = 5

TENCENT_QUOTE_URL = 'https://qt.gtimg.cn/q={codes}'
TENCENT_TIMEOUT = 5
TENCENT_BATCH_SIZE = 60

# =========================================================================== #
# chip_screener_v3 BREAKOUT_CONFIG — the authoritative "出手" threshold.
# Must stay in sync with chip_screener_v3.py::BREAKOUT_CONFIG.
# =========================================================================== #
SCREENER_MIN_GAIN = 0.010      # +1.0% vs prev close (today's pct_chg)
SCREENER_MIN_VOL_RATIO = 1.3   # volume / 5d-avg  (Tencent field[49] proxy)

# Session-specific display rules.
# Intraday gain threshold is looser only to surface "on track" candidates
# early — the final BUY verdict still requires SCREENER_MIN_GAIN + VOL_RATIO.
SESSION_RULES = {
    'morning': {'min_gain': 0.006, 'require_green': True,  'label': '早盘 09:30-11:30'},
    'midday':  {'min_gain': 0.006, 'require_green': True,  'label': '午间快照 11:30-13:00'},
    'close':   {'min_gain': 0.010, 'require_green': False, 'label': '收盘 13:00-15:00+'},
}

# Decision verdicts rendered in the output. Used by both pending pool and
# archived breakouts for consistent semantics.
VERDICT_BUY          = 'BUY'            # screener-grade: price AND volume
VERDICT_BUY_WEAK     = 'BUY_WEAK'       # price ok, volume marginally low (1.0-1.3x)
VERDICT_WAIT_VOLUME  = 'WAIT_VOLUME'    # price ok, volume clearly insufficient
VERDICT_WAIT_PRICE   = 'WAIT_PRICE'     # price not yet at +1.0%
VERDICT_HOLD_FIRE    = 'HOLD_FIRE'      # below signal price, drop it
VERDICT_NO_DATA      = 'NO_DATA'

VERDICT_ICON = {
    VERDICT_BUY:         '🟢',
    VERDICT_BUY_WEAK:    '🟡',
    VERDICT_WAIT_VOLUME: '🔶',
    VERDICT_WAIT_PRICE:  '⏳',
    VERDICT_HOLD_FIRE:   '⛔',
    VERDICT_NO_DATA:     '❓',
}

VERDICT_TEXT = {
    VERDICT_BUY:         '出手',
    VERDICT_BUY_WEAK:    '可试探',
    VERDICT_WAIT_VOLUME: '等放量',
    VERDICT_WAIT_PRICE:  '等突破',
    VERDICT_HOLD_FIRE:   '不出手',
    VERDICT_NO_DATA:     '无数据',
}

# A-share regular trading window. Used for session auto-detect and pace calc.
MORNING_OPEN  = dtime(9, 30)
MORNING_CLOSE = dtime(11, 30)
NOON_OPEN     = dtime(13, 0)
AFTERNOON_CLOSE = dtime(15, 0)
TRADING_SECONDS_FULL = 4 * 3600  # 2h morning + 2h afternoon


# --------------------------------------------------------------------------- #
# Session detection
# --------------------------------------------------------------------------- #
def auto_session(now: Optional[datetime] = None) -> str:
    """Pick a reasonable session based on wall-clock time."""
    now = now or datetime.now()
    t = now.time()
    if t < MORNING_OPEN:
        return 'close'        # pre-open: show yesterday's close as reference
    if t <= MORNING_CLOSE:
        return 'morning'
    if t < NOON_OPEN:
        return 'midday'
    if t < AFTERNOON_CLOSE:
        return 'morning'      # afternoon intraday — same looser rules
    return 'close'


def elapsed_trading_seconds(now: Optional[datetime] = None) -> int:
    """How many seconds of regular trading have elapsed today."""
    now = now or datetime.now()
    t = now.time()
    if t < MORNING_OPEN:
        return 0
    if t <= MORNING_CLOSE:
        return (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).seconds
    if t < NOON_OPEN:
        return 2 * 3600     # full morning
    if t < AFTERNOON_CLOSE:
        afternoon = (now - now.replace(hour=13, minute=0, second=0, microsecond=0)).seconds
        return 2 * 3600 + afternoon
    return TRADING_SECONDS_FULL


# --------------------------------------------------------------------------- #
# Ts code <-> Tencent code conversion
# --------------------------------------------------------------------------- #
def ts_to_tencent(ts_code: str) -> str:
    """'000962.SZ' -> 'sz000962', '600884.SH' -> 'sh600884'."""
    num, ex = ts_code.split('.')
    return f'{ex.lower()}{num}'


def _safe_float(s: str) -> Optional[float]:
    """Parse Tencent numeric field, tolerating empty strings / placeholders."""
    try:
        if s is None or s == '' or s == '-':
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Tencent quote fetcher
# --------------------------------------------------------------------------- #
def fetch_tencent_quotes(ts_codes: List[str]) -> Dict[str, dict]:
    """
    Fetch realtime quotes for a list of ts_codes from Tencent.
    Returns {ts_code: {name, price, open, prev_close, high, low, volume, pct_chg, ts}}.
    Missing codes are simply absent.
    """
    result: Dict[str, dict] = {}
    if not ts_codes:
        return result
    tencent_codes = [ts_to_tencent(c) for c in ts_codes]
    ts_lookup = dict(zip(tencent_codes, ts_codes))

    for i in range(0, len(tencent_codes), TENCENT_BATCH_SIZE):
        batch = tencent_codes[i:i + TENCENT_BATCH_SIZE]
        url = TENCENT_QUOTE_URL.format(codes=','.join(batch))

        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (quick_check)'},
            )
            with urllib.request.urlopen(req, timeout=TENCENT_TIMEOUT) as resp:
                raw = resp.read().decode('gbk', errors='ignore')
        except Exception as exc:
            print(f'  [WARN] Tencent batch request failed: {exc}', file=sys.stderr)
            continue

        for line in raw.strip().split('\n'):
            line = line.strip()
            if not line.startswith('v_') or '="' not in line:
                continue
            head, _, body = line.partition('="')
            tencent_code = head[2:]
            ts_code = ts_lookup.get(tencent_code)
            if ts_code is None:
                continue
            body = body.rstrip(';').rstrip('"')
            fields = body.split('~')
            if len(fields) < 35:
                continue
            try:
                parsed = {
                    'name':       fields[1],
                    'price':      float(fields[3] or 0),
                    'prev_close': float(fields[4] or 0),
                    'open':       float(fields[5] or 0),
                    'volume':     float(fields[6] or 0),   # lots (100 shares)
                    'high':       float(fields[33] or 0),
                    'low':        float(fields[34] or 0),
                    'pct_chg':    float(fields[32] or 0),  # percent, e.g. 2.35
                    'turnover':   _safe_float(fields[38]) if len(fields) > 38 else None,  # 成交额亿
                    'vol_ratio':  _safe_float(fields[49]) if len(fields) > 49 else None,  # 量比 vs 5d
                    'amplitude':  _safe_float(fields[43]) if len(fields) > 43 else None,  # 振幅%
                    'turnover_rate': _safe_float(fields[38]) if len(fields) > 38 else None,
                    'ts':         fields[30],              # "YYYYMMDDHHMMSS"
                }
            except (ValueError, IndexError):
                continue
            if parsed['price'] <= 0:
                continue
            result[ts_code] = parsed

    return result


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def _load_json(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def load_signal_pool(path: str) -> List[dict]:
    if not os.path.exists(path):
        print(f'[ERROR] Signal pool not found: {path}')
        print('        Run chip_screener_v3.py first to generate one.')
        sys.exit(1)
    return _load_json(path)


def load_recent_archive(path: str, days: int) -> List[dict]:
    """Return archive entries whose breakout_date is within the last N days."""
    data = _load_json(path)
    if not data:
        return []
    today = datetime.now()
    kept = []
    for e in data:
        try:
            bdt = datetime.strptime(e.get('breakout_date', ''), '%Y%m%d')
            age = (today - bdt).days
            if 0 <= age <= days:
                kept.append(e)
        except Exception:
            continue
    return kept


def _load_strategy_pool() -> List[dict]:
    """Load multi-strategy pool signals (breakout/trend/pullback)."""
    if not os.path.exists(STRATEGY_POOL_FILE):
        return []
    return _load_json(STRATEGY_POOL_FILE)


def _print_strategy_pool_section(strat_pool: List[dict], quotes: Dict[str, dict]) -> None:
    """
    Print multi-strategy pool signals with realtime quotes.
    Shows TOP 3 per pool, with pool-specific buy strategies.

    Buy strategies per pool:
      🚀 Breakout: 追涨买入 — 当日涨幅≥+2% + 量比≥1.5x (强势突破确认)
      🎯 Trend:    顺势加仓 — 当日涨幅≥+1% + 量比≥1.2x (趋势延续确认)
      🔄 Pullback: 低吸买入 — 当日涨幅≥+0.5% + 量比≥1.0x + 阳线 (止跌反弹确认)
    """
    if not strat_pool:
        return

    # ── Pool-specific entry rules ──
    POOL_ENTRY_RULES = {
        'breakout': {
            'name': '突破池',
            'icon': '🚀',
            'strategy': '追涨买入',
            'min_gain': 0.02,       # +2% today
            'min_vol_ratio': 1.5,   # strong volume
            'stop_loss': -0.05,     # -5%
            'target_1': 0.10,       # +10% sell 1/3
            'target_2': 0.20,       # +20% sell 1/3
            'desc': '放量突破新高 → 追涨 | 止损-5% | 目标+10%/+20%',
            'entry_desc': '涨幅≥+2% + 量比≥1.5x',
        },
        'trend': {
            'name': '趋势池',
            'icon': '🎯',
            'strategy': '顺势加仓',
            'min_gain': 0.015,      # +1.5% today (趋势股不急，要确认性强)
            'min_vol_ratio': 1.3,   # solid volume confirmation
            'stop_loss': -0.07,     # -7%
            'target_1': 0.15,       # +15% sell 1/3
            'target_2': 0.30,       # +30% sell 1/3
            'desc': 'Minervini强势 → 顺势 | 止损-7% | 目标+15%/+30%',
            'entry_desc': '涨幅≥+1.5% + 量比≥1.3x',
        },
        'pullback': {
            'name': '回踩池',
            'icon': '🔄',
            'strategy': '低吸买入',
            'min_gain': 0.01,       # +1% today (确认止跌反弹)
            'min_vol_ratio': 1.2,   # volume recovery signal
            'stop_loss': -0.05,     # -5%
            'target_1': 0.10,       # +10% sell 1/3
            'target_2': 0.15,       # +15% sell 1/3
            'desc': '强势股回踩 → 低吸 | 止损-5% | 目标+10%/+15%',
            'entry_desc': '涨幅≥+1% + 量比≥1.2x',
        },
    }

    width = 108
    print()
    print('═' * width)
    print(f'  🚀🎯🔄 【多策略池 — 今日买入策略】Top 3 per Pool')
    print('═' * width)

    # Group by pool type, with display-time quality filter
    pool_groups: Dict[str, List[dict]] = {}
    for s in strat_pool:
        pool = s.get('pool', 'unknown')
        score = s.get('score', 0)
        # Display-time filter: A-grade minimum
        if score < 80:
            continue
        # Trend pool: stricter filter (too many qualify in bull market)
        if pool == 'trend':
            if score < 90:
                continue
            # RS must be >= +20% (strong outperformance)
            if s.get('rs_6m', 0) < 20:
                continue
        pool_groups.setdefault(pool, []).append(s)

    total_actionable = 0

    for pool_name in ['breakout', 'trend', 'pullback']:
        items = pool_groups.get(pool_name, [])
        if not items:
            continue
        rule = POOL_ENTRY_RULES[pool_name]
        icon = rule['icon']
        pname = rule['name']

        # Enrich items with realtime data and classify
        enriched = []
        for s in items:
            ts_code = s['ts_code']
            q = quotes.get(ts_code)
            sig_close = s.get('signal_close', 0)
            entry = {
                'sig': s,
                'quote': q,
                'verdict': 'NO_DATA',
                'pct_day': 0,
                'pct_vs_sig': 0,
                'vol_ratio': None,
                'price': 0,
            }
            if q and q['price'] > 0 and sig_close > 0:
                entry['price'] = q['price']
                entry['pct_day'] = q['pct_chg'] / 100.0
                entry['pct_vs_sig'] = (q['price'] / sig_close - 1)
                entry['vol_ratio'] = q.get('vol_ratio')

                pct_day = entry['pct_day']
                vr = entry['vol_ratio'] or 0

                # Skip stocks with gain >= 6% (approaching limit-up, can't buy)
                if pct_day >= 0.06:
                    continue

                # Classify based on pool-specific rules
                if pct_day >= rule['min_gain'] and vr >= rule['min_vol_ratio']:
                    entry['verdict'] = 'BUY'
                elif pct_day >= rule['min_gain'] * 0.6 and vr >= rule['min_vol_ratio'] * 0.8:
                    entry['verdict'] = 'NEAR'
                elif pct_day >= 0:
                    entry['verdict'] = 'WATCH'
                else:
                    entry['verdict'] = 'WAIT'
            enriched.append(entry)

        # Sort: BUY first, then NEAR, then by score descending
        verdict_priority = {'BUY': 0, 'NEAR': 1, 'WATCH': 2, 'WAIT': 3, 'NO_DATA': 9}
        enriched.sort(key=lambda e: (
            verdict_priority.get(e['verdict'], 9),
            -e['sig'].get('score', 0),
        ))

        # Take top 3
        top3 = enriched[:6]
        buy_count = sum(1 for e in enriched if e['verdict'] == 'BUY')
        total_actionable += buy_count

        print(f'\n  {icon} === {pname} Top 3 / {len(items)}只 === 策略: {rule["strategy"]}')
        print(f'  {icon} 出手条件: {rule["entry_desc"]}')
        print(f'  {icon} 操作模板: {rule["desc"]}')
        print(f'  {"─" * 76}')

        for rank, e in enumerate(top3, 1):
            s = e['sig']
            q = e['quote']
            ts_code = s['ts_code']
            name = s.get('name', '?')
            score = s.get('score', 0)
            grade = s.get('grade', '?')
            rs = s.get('rs_6m', 0)
            reasons = s.get('reasons', [])

            if q is None or q['price'] <= 0:
                print(f'  {icon} #{rank} {name}({ts_code})  {score:.0f}{grade}  — 无实时数据')
                continue

            pct_day = e['pct_day'] * 100
            pct_vs_sig = e['pct_vs_sig'] * 100
            vr = e['vol_ratio']
            vr_str = f'量比{vr:.2f}x' if vr else '量比N/A'
            price = e['price']

            # Verdict icon
            if e['verdict'] == 'BUY':
                v_icon = '🟢出手'
                v_detail = '双达标 📍'
            elif e['verdict'] == 'NEAR':
                v_icon = '🟡接近'
                v_detail = '接近出手线'
            elif e['verdict'] == 'WATCH':
                v_icon = '⏳观察'
                v_detail = '等待信号'
            elif e['verdict'] == 'WAIT':
                v_icon = '💤回调'
                v_detail = '今日回调中'
            else:
                v_icon = '❓'
                v_detail = ''

            # Main line
            print(f'  {icon} #{rank} [{v_icon}] {name}({ts_code})  '
                  f'now {price:<7.2f}  day {pct_day:+5.2f}%  '
                  f'{vr_str}  {score:.0f}{grade}  RS{rs:+.1f}%')

            # Detail line: entry plan
            if e['verdict'] == 'BUY':
                stop = price * (1 + rule['stop_loss'])
                t1 = price * (1 + rule['target_1'])
                t2 = price * (1 + rule['target_2'])
                print(f'       └─ {v_detail} | 入场:{price:.2f} 止损:{stop:.2f}({rule["stop_loss"]*100:+.0f}%) '
                      f'T1:{t1:.2f}({rule["target_1"]*100:+.0f}%) T2:{t2:.2f}({rule["target_2"]*100:+.0f}%)')
            else:
                reason_str = '; '.join(reasons[:2]) if reasons else ''
                print(f'       └─ {v_detail} | {reason_str}')

        if buy_count > 0:
            print(f'  {icon} 🟢 本池有 {buy_count} 只达标可出手！')
        print()

    print(f'{"─" * width}')
    # Summary footer — focus on Top 3 recommendations only
    if total_actionable > 0:
        print(f'  🟢 今日推荐: 上方 Top 3 中标记 [🟢出手] 的即为今日可操作标的')
        print(f'  📊 全池达标数(仅供参考): 共 {total_actionable} 只满足出手条件')
    else:
        print(f'  💤 多策略池今日暂无达标信号，继续观察 Top 3 动态')
    print()
    print(f'  📋 各池买入逻辑:')
    print(f'     🚀 突破池: 涨≥+2% + 量比≥1.5x → 追涨(强突破才追) | 仓位: 1/3仓')
    print(f'     🎯 趋势池: 涨≥+1.5% + 量比≥1.3x → 顺势(趋势延续) | 仓位: 1/2仓')
    print(f'     🔄 回踩池: 涨≥+1% + 量比≥1.2x → 低吸(止跌反弹)   | 仓位: 1/3仓')
    print(f'     ⚠️  同一天最多出手2只，总仓位不超过80%')
    print(f'{"─" * width}')


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify(sig: dict, quote: Optional[dict],
             min_gain: float, require_green: bool, session: str) -> dict:
    """
    Decide whether to act on a signal-pool entry RIGHT NOW.

    Decision ladder (matches chip_screener_v3 BREAKOUT_CONFIG):
        BUY          : today_pct_chg >= 1.0%  AND  vol_ratio >= 1.3x  AND  price >= sig_price
        BUY_WEAK     : today_pct_chg >= 1.0%  AND  vol_ratio in [1.0, 1.3)
                       price is there but volume is marginal — small position
        WAIT_VOLUME  : today_pct_chg >= 1.0%  AND  vol_ratio < 1.0
                       price moved without volume; likely fades, do NOT chase
        WAIT_PRICE   : price >= sig_price  AND  today_pct_chg < 1.0%
                       (still waiting for the breakout candle)
        HOLD_FIRE    : price < signal_price (signal likely broken)
        NO_DATA      : quote missing or invalid

    KEY FIX (2026-04-23): chip_screener_v3.BREAKOUT_CONFIG['min_pct_chg_breakout']
    means TODAY'S day return (pct_chg vs yesterday close), NOT cumulative vs
    signal_close. A stock can sit +2% above its signal price from days ago
    while being -0.5% today — that is NOT a breakout candle. We now gate on
    Tencent's field[32] (today's pct_chg) for the breakout bar check, and
    only use pct_vs_sig as a sanity check (must be >= 0).
    """
    out = {
        'sig': sig,
        'quote': quote,
        'verdict': VERDICT_NO_DATA,
        'pct_vs_sig': None,
        'pct_today': None,
        'vol_ratio': None,
        'is_new_high': False,
        'reason': '',
    }
    if quote is None or quote['price'] <= 0:
        out['reason'] = 'no realtime quote'
        return out

    sig_price = sig['signal_close']
    price = quote['price']
    pct_vs_sig = (price / sig_price - 1.0) if sig_price else 0.0
    pct_today = quote['pct_chg'] / 100.0  # Tencent returns percent, convert to decimal
    out['pct_vs_sig'] = pct_vs_sig
    out['pct_today'] = pct_today
    out['vol_ratio'] = quote.get('vol_ratio')

    # Intraday new high = price equals today's high and is above open.
    if quote['high'] > 0 and quote['open'] > 0:
        out['is_new_high'] = (
            price >= quote['high'] - 1e-6 and price > quote['open']
        )

    # ---- Ladder ----
    # Gate 1: price must be at or above signal_close. Signal is broken otherwise.
    if price < sig_price:
        out['verdict'] = VERDICT_HOLD_FIRE
        out['reason'] = f'价格跌破信号价 (vs_sig {pct_vs_sig*100:+.2f}%)'
        return out

    # Gate 2: TODAY's candle must itself be a breakout yang line.
    # This is what chip_screener_v3 actually checks — not the cumulative
    # move since signal day.
    price_pass_screener = pct_today >= SCREENER_MIN_GAIN
    vr = quote.get('vol_ratio')

    if not price_pass_screener:
        # Price is above signal but today's bar isn't strong enough yet.
        out['verdict'] = VERDICT_WAIT_PRICE
        if pct_today < min_gain:
            out['reason'] = (f'今日涨幅 {pct_today*100:+.2f}% '
                             f'未达本窗口门槛 {min_gain*100:.1f}% '
                             f'(vs信号 {pct_vs_sig*100:+.2f}%)')
        else:
            out['reason'] = (f'今日涨幅 {pct_today*100:+.2f}% '
                             f'未达出手线 {SCREENER_MIN_GAIN*100:.1f}% '
                             f'(vs信号 {pct_vs_sig*100:+.2f}%)')
        return out

    # From here on: today's candle has cleared +1.0%. Volume is the deciding factor.
    if vr is None:
        # Volume data missing — can't confirm. Be conservative.
        out['verdict'] = VERDICT_BUY_WEAK
        out['reason'] = f'今日涨幅 {pct_today*100:+.2f}% 达标，但量比缺失'
        return out

    if vr >= SCREENER_MIN_VOL_RATIO:
        out['verdict'] = VERDICT_BUY
        out['reason'] = (f'今日涨幅 {pct_today*100:+.2f}% + '
                         f'量比 {vr:.2f}x 双达标 📍 '
                         f'(vs信号 {pct_vs_sig*100:+.2f}%)')
        return out

    if vr >= 1.0:
        out['verdict'] = VERDICT_BUY_WEAK
        out['reason'] = (f'今日涨幅 {pct_today*100:+.2f}% 达标，'
                         f'但量比 {vr:.2f}x 在 [1.0, 1.3) 区间 — 量能偏弱')
        return out

    # price up but volume is actually shrinking
    out['verdict'] = VERDICT_WAIT_VOLUME
    out['reason'] = (f'今日涨幅 {pct_today*100:+.2f}% 达标，'
                     f'但量比 {vr:.2f}x < 1.0 — 缩量拉升，小心假突破')
    return out


def classify_archive(entry: dict, quote: Optional[dict]) -> dict:
    """
    Classify a historical breakout for follow-through display.
    We care about: price vs breakout_close (holding the breakout? breaking down?)
    """
    bo_close = entry.get('breakout_close')
    bo_date = entry.get('breakout_date', '')
    out = {
        'sig': entry,
        'quote': quote,
        'status': 'HOLDING',      # HOLDING / STOPPED / BOUGHT_TODAY / NO_DATA
        'pct_vs_bo': None,
        'is_today': False,
        'is_new_high': False,
        'note': '',
    }

    today_str = datetime.now().strftime('%Y%m%d')
    out['is_today'] = (bo_date == today_str)

    if quote is None or quote['price'] <= 0:
        out['status'] = 'NO_DATA'
        out['note'] = 'no quote returned'
        return out
    if not bo_close:
        out['status'] = 'NO_DATA'
        out['note'] = 'no breakout_close in archive'
        return out

    price = quote['price']
    pct_vs_bo = (price / bo_close - 1.0)
    out['pct_vs_bo'] = pct_vs_bo

    if quote['high'] > 0 and quote['open'] > 0:
        out['is_new_high'] = price >= quote['high'] - 1e-6

    # Stop-loss rule from main screener: -7% from breakout_close.
    if pct_vs_bo <= -0.07:
        out['status'] = 'STOPPED'
        out['note'] = 'below -7% stop'
    elif out['is_today']:
        out['status'] = 'BOUGHT_TODAY'
    else:
        out['status'] = 'HOLDING'
    return out


STATUS_ICON = {
    'BREAKOUT': '🔫',
    'PENDING':  '⏳',
    'FAILED':   '❌',
    'NO_DATA':  '❓',
}

ARCHIVE_ICON = {
    'BOUGHT_TODAY': '🔫',   # today's confirmed breakout, still hot
    'HOLDING':      '📈',   # past breakout, still alive
    'STOPPED':      '🛑',   # hit stop
    'NO_DATA':      '❓',
}


# --------------------------------------------------------------------------- #
# Display helpers
# --------------------------------------------------------------------------- #
def project_full_day_volume(current_vol: float, elapsed_s: int) -> Optional[float]:
    """Linear-pace extrapolation of today's full-day volume. None if N/A."""
    if elapsed_s <= 0 or current_vol <= 0:
        return None
    ratio = TRADING_SECONDS_FULL / max(elapsed_s, 1)
    return current_vol * ratio


def fmt_vol(lots: float) -> str:
    """Format volume (in lots) as a human-friendly 万手 string."""
    if lots <= 0:
        return '-'
    wan = lots / 10000
    return f'{wan:.1f}万手' if wan >= 1 else f'{lots:.0f}手'


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def print_archive_section(results: List[dict], elapsed_s: int) -> None:
    """Print follow-through section for archived breakouts.
    Separates STOPPED (hit -7% stop) from active holdings.
    """
    if not results:
        return
    width = 100

    # Split into active vs stopped
    active = [r for r in results if r.get('status') != 'STOPPED']
    stopped = [r for r in results if r.get('status') == 'STOPPED']

    # ── Active holdings ──
    if active:
        today_count = sum(1 for r in active if r.get('is_today'))
        print()
        print('🔫' * 50)
        print(f'  🔫🔫🔫 【已确认突破 — 跟踪中】 ({len(active)} 只，其中今日新突破 {today_count} 只） 🔫🔫🔫')
        print('🔫' * 50)

        active.sort(key=lambda r: (not r.get('is_today'), -(r.get('pct_vs_bo') or -999)))
        for r in active:
            _print_archive_line(r)
        print('-' * width)

    # ── Stopped (hit -7% stop-loss) — separate section ──
    if stopped:
        print()
        print('🛑' * 50)
        print(f'  🛑🛑🛑 【已止损 — 出局】 ({len(stopped)} 只，跌破突破价-7%止损线） 🛑🛑🛑')
        print('🛑' * 50)

        stopped.sort(key=lambda r: (r.get('pct_vs_bo') or 0))
        for r in stopped:
            _print_archive_line(r)
        print(f'  💡 止损纪律: 突破失败很正常，关键是截断亏损。已止损的票不再跟踪。')
        print('-' * width)


def _print_archive_line(r: dict) -> None:
    """Print a single archive entry line."""
    e = r['sig']
    q = r['quote']
    icon = ARCHIVE_ICON[r['status']]
    name = f"{e.get('name', '?')}({e.get('ts_code', '?')})"
    bo_px = e.get('breakout_close', '-')
    bo_date = e.get('breakout_date', '-')
    score = e.get('score', '-')
    dyn = e.get('dyn_signal', '')
    bo_vr = e.get('breakout_vol_ratio')

    tag_today = ' 🆕今日新突破' if r.get('is_today') else ''
    tag_high = ' ⬆日内新高' if r.get('is_new_high') else ''

    if q is None:
        print(f'  {icon} {name:<24}  突破 {bo_px}@{bo_date}  '
              f'score {score}  {dyn}{tag_today}  — {r["note"]}')
        return

    pct_bo = (r['pct_vs_bo'] or 0) * 100
    pct_day = q['pct_chg']
    vol_str = fmt_vol(q['volume'])
    vr_str = f'突破日量比{bo_vr:.1f}x' if bo_vr else ''

    line = (f'  {icon} {name:<24}  突破 {bo_px}@{bo_date}  '
            f'now {q["price"]:<7}  vs_bo {pct_bo:+6.2f}%  '
            f'day {pct_day:+5.2f}%  vol {vol_str}  '
            f'score {score}  {dyn}  {vr_str}{tag_today}{tag_high}')
    print(line)


def print_pending_section(results: List[dict], session: str, min_gain: float,
                          require_green: bool, elapsed_s: int) -> None:
    """Print pending-pool section with verdict-driven decision guidance."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    width = 108
    print('=' * width)
    print(f' 信号池扫描  |  {now}  |  session: {SESSION_RULES[session]["label"]}')
    print(f' 📍 出手规则 (chip_screener_v3 同款): 涨幅 ≥ {SCREENER_MIN_GAIN*100:.1f}%  AND  '
          f'量比 ≥ {SCREENER_MIN_VOL_RATIO:.1f}x')
    print(f'    量比数据源: 腾讯实时 (当日成交量 / 过去5日同时段均量)')
    if session in ('morning', 'midday'):
        print(f' ℹ️  腾讯量比是"同时段量比"(今日至今量 ÷ 过去5日同时段均量)，'
              f'盘中已自带进度归一，可直接对比 {SCREENER_MIN_VOL_RATIO:.1f}x 门槛')
        print(f'    当前显示涨幅门槛放宽至 {min_gain*100:.1f}% 便于早期发现苗头，'
              f'最终出手仍需 +{SCREENER_MIN_GAIN*100:.1f}% 并量比 ≥ {SCREENER_MIN_VOL_RATIO:.1f}x')
    print('=' * width)

    # Priority: BUY > BUY_WEAK > WAIT_VOLUME > WAIT_PRICE > HOLD_FIRE > NO_DATA
    priority = {
        VERDICT_BUY: 0,
        VERDICT_BUY_WEAK: 1,
        VERDICT_WAIT_VOLUME: 2,
        VERDICT_WAIT_PRICE: 3,
        VERDICT_HOLD_FIRE: 4,
        VERDICT_NO_DATA: 5,
    }

    def sort_key(r):
        v = r['verdict']
        # Within same verdict, prefer: new-high first, then higher score, then higher gain
        return (
            priority.get(v, 9),
            0 if r.get('is_new_high') else 1,
            -(r['sig'].get('score') or 0),
            -(r.get('pct_vs_sig') or -999),
        )
    results.sort(key=sort_key)

    counters = {v: 0 for v in VERDICT_ICON}
    new_high_count = 0

    for r in results:
        sig = r['sig']
        q = r['quote']
        v = r['verdict']
        icon = VERDICT_ICON[v]
        verdict_text = VERDICT_TEXT[v]
        counters[v] += 1

        name = f"{sig['name']}({sig['ts_code']})"
        sig_px = sig['signal_close']
        sig_date = sig['signal_date']
        score = sig.get('score', '-')
        dyn = sig.get('dyn_signal', '')

        tag = ''
        if r.get('is_new_high') and v in (VERDICT_BUY, VERDICT_BUY_WEAK, VERDICT_WAIT_VOLUME):
            tag = ' ⬆日内新高'
            new_high_count += 1

        if q is None:
            print(f'  {icon} [{verdict_text}] {name:<22}  sig {sig_px}@{sig_date}  '
                  f'score {score}  {dyn}  — {r.get("reason", "")}')
            continue

        pct_sig = (r['pct_vs_sig'] or 0) * 100
        pct_day = q['pct_chg']
        vr = r.get('vol_ratio')
        vr_str = f'量比 {vr:.2f}x' if vr is not None else '量比 N/A'

        # Volume behavior from signal pool (ground volume data)
        qs = sig.get('quiet_streak')
        rr = sig.get('recovery_ratio')
        vol_info = ''
        if qs is not None and rr is not None:
            vol_info = f'  地量{qs}天→恢复{rr:.0%}'
        else:
            vol_info = '  地量N/A'

        # First line: the decision + key metrics
        line1 = (f'  {icon} [{verdict_text:<4}] {name:<22}  '
                 f'sig {sig_px}@{sig_date}  now {q["price"]:<7}  '
                 f'vs_sig {pct_sig:+6.2f}%  day {pct_day:+5.2f}%  '
                 f'{vr_str}  score {score}{vol_info}{tag}')
        print(line1)
        # Second line: the reasoning (why this verdict)
        reason = r.get('reason') or ''
        chip_conc = sig.get('chip_conc', '')
        winner = sig.get('winner', '')
        chip_info = f'集中度{chip_conc} 获利比{winner}' if chip_conc else ''
        detail_parts = [p for p in [reason, dyn, chip_info] if p]
        print(f'       └─ {"  |  ".join(detail_parts)}')

    print('-' * width)
    # Summary by verdict
    buy_total = counters[VERDICT_BUY]
    buy_weak = counters[VERDICT_BUY_WEAK]
    wait_vol = counters[VERDICT_WAIT_VOLUME]
    wait_px = counters[VERDICT_WAIT_PRICE]
    hold = counters[VERDICT_HOLD_FIRE]
    nod = counters[VERDICT_NO_DATA]
    print(f' Summary:  🟢出手:{buy_total}  🟡试探:{buy_weak}  '
          f'🔶等放量:{wait_vol}  ⏳等突破:{wait_px}  ⛔淘汰:{hold}  ❓无数据:{nod}  '
          f'|  ⬆日内新高: {new_high_count}')
    print('=' * width)

    # Actionable footer
    print()
    if buy_total > 0:
        print(f' 🟢 有 {buy_total} 只【出手】: 涨幅 + 量比双达标，符合 chip_screener_v3 的入场标准。')
        print(f'    建议按主脚本模板执行: 入场价=现价, 止损 -7%, 第一止盈 +10% 卖 1/3。')
    if buy_weak > 0:
        print(f' 🟡 有 {buy_weak} 只【可试探】: 价到量弱 (量比 1.0~1.3x)，')
        print(f'    小仓位试探即可，不要满仓；等收盘量比校验后再决定加仓。')
    if wait_vol > 0:
        print(f' 🔶 有 {wait_vol} 只【缩量拉升】: 警惕假突破，不追！等放量再说。')
    if buy_total == 0 and buy_weak == 0:
        if wait_vol > 0:
            print(' ⚠️  今日池内只有缩量冲高，容易假突破。按兵不动是最好的操作。')
        elif wait_px > 0:
            print(' 💤 价格还没到出手门槛。继续观察，或等下一个交易日。')
        else:
            print(' 💤 信号池全部回落，本批观察结束。')

    if session in ('morning', 'midday'):
        print(' 💡 16:00 后跑 chip_screener_v3.py 做最终确认 (Tushare EOD 数据最权威)')

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Fast breakout check against the chip signal pool + archive '
                    '(Tencent realtime, no Tushare).'
    )
    parser.add_argument('--session', choices=['auto', 'morning', 'midday', 'close'],
                        default='auto',
                        help='time-of-day preset; auto picks by wall clock')
    parser.add_argument('--min-gain', type=float, default=None,
                        help='override session default; e.g. 0.01 = 1%%')
    parser.add_argument('--require-green', action='store_true',
                        help='force "close > open" even in close session')
    parser.add_argument('--pool', default=SIGNAL_POOL_FILE,
                        help=f'signal pool JSON (default: {SIGNAL_POOL_FILE})')
    parser.add_argument('--archive', default=BREAKOUT_ARCHIVE_FILE,
                        help=f'breakout archive JSON (default: {BREAKOUT_ARCHIVE_FILE})')
    parser.add_argument('--archive-days', type=int, default=ARCHIVE_SHOW_DAYS,
                        help=f'how many recent days of archive to show '
                             f'(default: {ARCHIVE_SHOW_DAYS})')
    parser.add_argument('--no-archive', action='store_true',
                        help='skip the archive / follow-through section')
    args = parser.parse_args()

    session = auto_session() if args.session == 'auto' else args.session
    rule = SESSION_RULES[session]
    min_gain = args.min_gain if args.min_gain is not None else rule['min_gain']
    require_green = rule['require_green'] or args.require_green

    pool = load_signal_pool(args.pool)
    archive = [] if args.no_archive else load_recent_archive(args.archive, args.archive_days)

    if not pool and not archive:
        print('[INFO] Signal pool empty and no recent archive — nothing to check.')
        return

    # Deduplicate: if a stock appears in both pool and archive (rare, but defensively
    # handled — e.g. same ts_code got re-signaled after a prior breakout), prefer
    # the archive record so we show follow-through, not a fresh "waiting" view.
    archive_codes = {e['ts_code'] for e in archive}
    pool = [s for s in pool if s['ts_code'] not in archive_codes]

    all_codes = [s['ts_code'] for s in pool] + [e['ts_code'] for e in archive]
    t0 = time.time()
    quotes = fetch_tencent_quotes(all_codes)
    elapsed_net = time.time() - t0
    elapsed_trade = elapsed_trading_seconds()

    # --- Archive section first (most actionable: already confirmed breakouts) ---
    if archive:
        archive_results = [classify_archive(e, quotes.get(e['ts_code'])) for e in archive]
        print_archive_section(archive_results, elapsed_trade)

    # --- Pending pool section ---
    if pool:
        pending_results = [classify(s, quotes.get(s['ts_code']),
                                    min_gain, require_green, session) for s in pool]
        print_pending_section(pending_results, session, min_gain,
                              require_green, elapsed_trade)

    # --- Multi-strategy pool section ---
    strat_pool = _load_strategy_pool()
    if strat_pool:
        # Fetch quotes for strategy pool stocks not already fetched
        strat_codes = [s['ts_code'] for s in strat_pool if s['ts_code'] not in quotes]
        if strat_codes:
            strat_quotes = fetch_tencent_quotes(strat_codes)
            quotes.update(strat_quotes)
        _print_strategy_pool_section(strat_pool, quotes)

    print(f' Fetched {len(quotes)}/{len(all_codes) + len(strat_pool if strat_pool else [])} quotes in {elapsed_net:.2f}s.')


if __name__ == '__main__':
    main()
