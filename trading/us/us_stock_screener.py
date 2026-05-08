# -*- coding: utf-8 -*-
"""
美股选股器 us_stock_screener.py
=========================================
双池并行扫描：

池子A: 右侧突破池 (Right-side Breakout) — 真趋势跟随
  RS>70% + 距52周高<15% + 量比>1.5x + 均线多头
  正常仓位打，目标+10%~+20%，止损-5%

池子B: 左侧修复池 (Left-side Recovery) — 超跌反弹
  距52周高15-40% + 5日涨>3% + 量能温和 + MA10拐头
  轻仓试探(1/3~1/2仓)，目标+8%~+15%，止损-7%

股票池：S&P 500 + 全美股 ~2700只 + 矿业专属板块
市值过滤：$3亿 ~ $2000亿 USD
数据源：yfinance（免费无限速）

依赖：pip install yfinance pandas numpy
"""

import io
import os
import sys
import time
import pickle
import logging
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import sys, os; sys.path.insert(0, os.path.dirname(__file__))
from config import VCP_CONFIG, MOMENTUM_CONFIG, POOL_A_CONFIG, POOL_B_CONFIG, US_MINING_POOL

warnings.filterwarnings('ignore')

# Suppress yfinance "possibly delisted" noise
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# ============================================================
# Configuration (imported from config.py)
# ============================================================
_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
CACHE_FILE = os.path.join(_BASE_DIR, 'cache', 'us_history.pkl')
DELISTED_FILE = os.path.join(_BASE_DIR, 'cache', 'us_delisted.pkl')

# ============================================================
# Cache
# ============================================================
def load_us_cache():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'rb') as f:
                data = pickle.load(f)
            print(f"  [CACHE] Loaded {len(data)} US stocks from local cache")
            return data
        except Exception:
            pass
    return {}

def save_us_cache(cache_data):
    with open(CACHE_FILE, 'wb') as f:
        pickle.dump(cache_data, f)
    print(f"  [CACHE] Saved {len(cache_data)} US stocks to {CACHE_FILE}")

def load_delisted():
    """Load the delisted ticker blacklist (auto-maintained)."""
    if os.path.exists(DELISTED_FILE):
        try:
            with open(DELISTED_FILE, 'rb') as f:
                data = pickle.load(f)
            # Expire after 30 days — re-check in case ticker comes back
            ts = data.get('_timestamp', 0)
            if time.time() - ts > 30 * 86400:
                return set()
            return data.get('tickers', set())
        except Exception:
            pass
    return set()

def save_delisted(delisted_set):
    """Save the delisted ticker blacklist."""
    with open(DELISTED_FILE, 'wb') as f:
        pickle.dump({'tickers': delisted_set, '_timestamp': time.time()}, f)

# ============================================================
# Stock pool: S&P 500 + All US tickers + Custom Mining sector
# ============================================================
def get_all_us_tickers():
    """
    Build a comprehensive US stock pool:
    1. S&P 500 (GitHub CSV) — with sector/industry info
    2. All US tickers (~2700, GitHub) — broad coverage including Russell 2000
    3. Custom mining & metals sector — always included
    Returns [(ticker, name, sector, industry), ...]
    """
    import urllib.request, csv, io

    seen = set()
    result = []

    def _add(ticker, name, sector, industry):
        t = ticker.upper().strip()
        if t and t not in seen and len(t) <= 5 and t.isalpha():
            seen.add(t)
            result.append((t, name, sector, industry))

    # ---- Source 1: S&P 500 (with sector info) ----
    sp500_urls = [
        'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv',
        'https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv',
    ]
    sp500_count = 0
    for url in sp500_urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=15)
            text = resp.read().decode('utf-8')
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                ticker = row.get('Symbol', '').replace('.', '-')
                name = row.get('Security', row.get('Name', ticker))
                sector = row.get('GICS Sector', row.get('Sector', 'N/A'))
                industry = row.get('GICS Sub-Industry', row.get('Industry', 'N/A'))
                _add(ticker, name, sector, industry)
                sp500_count += 1
            if sp500_count > 0:
                print(f"  [POOL] S&P 500: {sp500_count} stocks (GitHub)")
                break
        except Exception:
            continue

    # ---- Source 2: All US tickers (covers Russell 2000 + more) ----
    all_ticker_urls = [
        'https://raw.githubusercontent.com/shilewenuw/get_all_tickers/master/get_all_tickers/tickers.csv',
    ]
    for url in all_ticker_urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=15)
            text = resp.read().decode('utf-8')
            before = len(result)
            for line in text.strip().split('\n'):
                ticker = line.strip()
                if ticker and ticker != 'Symbol':
                    _add(ticker, ticker, 'N/A', 'N/A')  # sector filled later via yfinance
            print(f"  [POOL] All US tickers: +{len(result)-before} new (total {len(result)})")
            break
        except Exception as e:
            print(f"  [WARN] All-tickers source failed: {e}")

    # ---- Source 3: Custom Mining & Metals (ALWAYS included) ----
    mining_count = 0
    for ticker, name, sector, industry in _mining_pool():
        if ticker not in seen:
            _add(ticker, name, sector, industry)
            mining_count += 1
    print(f"  [POOL] Mining & Metals custom: +{mining_count} (total {len(result)})")

    # ---- Fallback: built-in pool if nothing online worked ----
    if len(result) < 100:
        print("  [WARN] Online sources failed, using built-in pool")
        for t, n, s, i in _builtin_pool():
            _add(t, n, s, i)

    print(f"  [POOL] ✅ Total unique tickers: {len(result)}")
    return result


def _mining_pool():
    """Custom mining & metals sector — imported from config.py"""
    return US_MINING_POOL


def _builtin_pool():
    """Built-in pool (~250 stocks covering all sectors + mining)"""
    sectors = {
        'Technology': [
            'AAPL','MSFT','GOOGL','META','NVDA','AMD','INTC','QCOM','AVGO','CRM',
            'ADBE','NOW','ORCL','CSCO','IBM','TXN','AMAT','LRCX','KLAC','MCHP',
            'SNPS','CDNS','FTNT','PANW','CRWD','ZS','DDOG','NET','MDB','TEAM',
        ],
        'Health Care': [
            'JNJ','UNH','PFE','ABBV','MRK','LLY','AMGN','GILD','BMY','VRTX',
            'REGN','ISRG','DXCM','IDXX','ZTS','BDX','SYK','MDT','EW',
            'HCA','CI','ELV','HUM','CNC','MOH','ALGN','HOLX','PODD',
        ],
        'Financials': [
            'JPM','BAC','WFC','GS','MS','BLK','SCHW','AXP','V','MA',
            'SPGI','MCO','ICE','CME','CB','AON','MMC','TRV','PGR','ALL',
            'MET','PRU','AIG','AFL','BK','STT','NTRS','FITB','HBAN','RF',
        ],
        'Consumer Discretionary': [
            'AMZN','TSLA','HD','LOW','TJX','NKE','SBUX','MCD','CMG','YUM',
            'DPZ','BKNG','ABNB','MAR','HLT','RCL','LVS','WYNN','F','GM',
            'APTV','BWA','LEA','GRMN','POOL','WSM','RH','DECK','LULU','ULTA',
        ],
        'Consumer Staples': [
            'PG','KO','PEP','WMT','COST','MDLZ','CL','KMB','GIS',
            'HSY','MKC','SJM','CAG','CPB','TSN','HRL','CLX','CHD','EL',
        ],
        'Industrials': [
            'CAT','DE','BA','RTX','HON','GE','LMT','NOC','GD','TDG',
            'ITW','EMR','ROK','PH','ETN','AME','FAST','SWK','IR','XYL',
            'WM','RSG','VRSK','CTAS','PAYX','CPRT','ODFL','JBHT','CSX','UNP',
        ],
        'Energy': [
            'XOM','CVX','COP','EOG','SLB','MPC','VLO','PSX','OXY','DVN',
            'FANG','HES','HAL','BKR','TRGP','WMB','KMI','OKE','CTRA','EQT',
        ],
        'Materials': [
            'LIN','APD','SHW','ECL','DD','NEM','FCX','NUE','STLD','CF',
            'MOS','ALB','FMC','CE','EMN','PPG','VMC','MLM','AVY','IFF',
        ],
        'Real Estate': [
            'PLD','AMT','CCI','EQIX','PSA','SPG','O','WELL','DLR','AVB',
            'EQR','VTR','ARE','MAA','UDR','ESS','HST','KIM','REG',
        ],
        'Utilities': [
            'NEE','DUK','SO','D','AEP','SRE','EXC','XEL','WEC','ES',
            'ED','AEE','CMS','DTE','FE','PPL','EVRG','ATO','NI','PNW',
        ],
        'Communication Services': [
            'DIS','NFLX','T','VZ','TMUS','CMCSA','CHTR','EA','TTWO','MTCH',
            'LYV','PARA','WBD','FOX','FOXA','IPG','OMC',
        ],
    }
    result = []
    for sector, tickers in sectors.items():
        for t in tickers:
            result.append((t, t, sector, sector))
    # Always include mining
    result.extend(_mining_pool())
    print(f"  [POOL] Built-in pool: {len(result)} stocks (incl. mining)")
    return result


# ============================================================
# History data (yfinance batch download)
# ============================================================
def _get_last_us_trading_day():
    """
    Determine the most recent US trading day (approximate).
    US markets close at 16:00 ET. yfinance data usually available by ~17:00 ET.
    We use a simple heuristic: if it's before 17:00 ET on a weekday, use yesterday.
    Weekends/holidays are handled by rolling back to Friday.
    """
    from datetime import timezone
    now_utc = datetime.now(timezone.utc)
    # ET = UTC-4 (EDT) or UTC-5 (EST). Use UTC-4 as conservative estimate.
    et_hour = (now_utc.hour - 4) % 24
    et_date = now_utc - timedelta(hours=4)
    today_et = et_date.date()

    # If before 17:00 ET, data for today isn't ready yet -> use previous day
    if et_hour < 17:
        today_et = today_et - timedelta(days=1)

    # Roll back weekends: Saturday->Friday, Sunday->Friday
    weekday = today_et.weekday()  # Mon=0 ... Sun=6
    if weekday == 5:  # Saturday
        today_et -= timedelta(days=1)
    elif weekday == 6:  # Sunday
        today_et -= timedelta(days=2)

    return today_et.strftime('%Y-%m-%d')


def fetch_history_batch(tickers, cache_data, period_days=1100, update_delisted=True):
    """Batch download/update history via yfinance.
    
    Cache strategy (mirrors chip_pool_screener.py architecture):
    - Compare cache's last date against the latest US trading day (not calendar today)
    - If cache already has the latest trading day's data -> from_cache (skip entirely)
    - If cache exists but is stale -> incremental update (only fetch missing days)
    - If no cache at all -> full download (1100 days = ~3 years)
    
    Args:
        update_delisted: If False, skip delisted blacklist updates (for backtest re-downloads).
    """
    import yfinance as yf

    today_str = datetime.now().strftime('%Y-%m-%d')
    latest_trade_day = _get_last_us_trading_day()
    print(f"  [CACHE] Latest US trading day (estimated): {latest_trade_day}")

    need_full = []
    need_incr = {}
    from_cache = 0

    # Minimum rows threshold: if cache has fewer rows than this,
    # treat as "need full download" even if last date is current.
    # This ensures we get 3 years of data when period_days is increased.
    min_rows_threshold = int(period_days * 0.55)  # ~605 rows for 1100 days

    for ticker in tickers:
        if ticker not in cache_data or cache_data[ticker] is None or \
           (hasattr(cache_data[ticker], 'empty') and cache_data[ticker].empty):
            need_full.append(ticker)
        else:
            df = cache_data[ticker]
            last = df.index.max()
            last_str = last.strftime('%Y-%m-%d') if hasattr(last, 'strftime') else str(last)[:10]
            # Check if cache has enough history rows
            if len(df) < min_rows_threshold:
                need_full.append(ticker)  # Too short, re-download full history
            elif last_str >= latest_trade_day:
                # Cache already has the latest trading day's data -> skip
                from_cache += 1
            else:
                need_incr[ticker] = last_str

    print(f"  [CACHE] from_cache={from_cache} | incremental={len(need_incr)} | full={len(need_full)}")

    newly_delisted = set()

    def _batch_download(batch_tickers, start, end, mark_delisted=False):
        """Download a batch and split into per-ticker DataFrames.
        mark_delisted: only True for full downloads (400 days). 
        Incremental updates may return empty simply because market hasn't opened.
        Includes rate-limit retry logic (up to 3 attempts with exponential backoff).
        """
        for attempt in range(3):
            try:
                raw = yf.download(batch_tickers, start=start, end=end,
                                  auto_adjust=True, progress=False, threads=True)
                if raw is None or raw.empty:
                    # Check if this is a rate limit (yfinance sometimes returns empty on rate limit)
                    if attempt < 2:
                        time.sleep(5 * (attempt + 1))  # 5s, 10s backoff
                        continue
                    if mark_delisted:
                        newly_delisted.update(batch_tickers)
                    return {}
                # yfinance returns MultiIndex columns for multi-ticker
                if isinstance(raw.columns, pd.MultiIndex):
                    results = {}
                    for ticker in batch_tickers:
                        try:
                            df = raw.xs(ticker, axis=1, level=1)[['Open','High','Low','Close','Volume']].copy()
                            df = df.dropna(subset=['Close'])
                            if not df.empty:
                                results[ticker] = df
                            elif mark_delisted:
                                newly_delisted.add(ticker)
                        except Exception:
                            if mark_delisted:
                                newly_delisted.add(ticker)
                    return results
                else:
                    # Single ticker
                    df = raw[['Open','High','Low','Close','Volume']].copy().dropna(subset=['Close'])
                    if df.empty and mark_delisted:
                        newly_delisted.update(batch_tickers)
                    return {batch_tickers[0]: df} if not df.empty else {}
            except Exception as e:
                err_str = str(e).lower()
                if 'rate' in err_str or 'limit' in err_str or 'too many' in err_str:
                    wait = 15 * (attempt + 1)  # 15s, 30s, 45s
                    print(f"  [RATE LIMIT] Waiting {wait}s before retry ({attempt+1}/3)...")
                    time.sleep(wait)
                    continue
                print(f"  [WARN] Batch download failed: {e}")
                return {}
        return {}

    # Full download — mark_delisted=True (400 days of no data = truly dead)
    if need_full:
        print(f"  [FULL] Downloading {len(need_full)} stocks...")
        start = (datetime.now() - timedelta(days=period_days)).strftime('%Y-%m-%d')
        for i in range(0, len(need_full), 50):
            batch = need_full[i:i+50]
            data = _batch_download(batch, start, today_str, mark_delisted=True)
            cache_data.update(data)
            print(f"    ... {min(i+50, len(need_full))}/{len(need_full)} done ({len(data)} ok)")
            if i + 50 < len(need_full):
                time.sleep(1.0)  # Rate-limit protection: 1s between batches

    # Incremental update — mark_delisted=False (empty = market not open yet, NOT delisted)
    if need_incr:
        print(f"  [INCR] Updating {len(need_incr)} stocks...")
        tickers_incr = list(need_incr.keys())
        earliest = min(need_incr.values())
        start_incr = (datetime.strptime(earliest, '%Y-%m-%d')).strftime('%Y-%m-%d')
        for i in range(0, len(tickers_incr), 50):
            batch = tickers_incr[i:i+50]
            data = _batch_download(batch, start_incr, today_str, mark_delisted=False)
            for ticker, new_df in data.items():
                old_df = cache_data.get(ticker, pd.DataFrame())
                if not old_df.empty:
                    merged = pd.concat([old_df, new_df])
                    merged = merged[~merged.index.duplicated(keep='last')].sort_index().tail(max(800, period_days))
                    cache_data[ticker] = merged
                else:
                    cache_data[ticker] = new_df

    # Update delisted blacklist (only when called from screener, not backtest re-downloads)
    if newly_delisted and update_delisted:
        old_delisted = load_delisted()
        merged_delisted = old_delisted | newly_delisted
        save_delisted(merged_delisted)
        print(f"  [DELISTED] {len(newly_delisted)} new delisted tickers detected "
              f"(total blacklist: {len(merged_delisted)})")
    elif newly_delisted and not update_delisted:
        print(f"  [DELISTED] {len(newly_delisted)} tickers returned empty (skipped blacklist update)")

    return cache_data


# ============================================================
# Trend Template (Minervini Stage 2 filter)
# ============================================================
def check_trend_template(hist, cfg):
    """
    Minervini Trend Template:
    1. Price > MA50 > MA150 > MA200
    2. MA200 trending up for at least 1 month
    3. Price >= 52w low * 1.25
    4. Price >= 52w high * 0.75
    Returns (passed: bool, details: dict)
    """
    if len(hist) < cfg['min_days']:
        return False, {}

    close = float(hist['Close'].iloc[-1])
    ma50  = float(hist['Close'].tail(cfg['ma_short']).mean())
    ma150 = float(hist['Close'].tail(cfg['ma_mid']).mean())
    ma200 = float(hist['Close'].tail(cfg['ma_long']).mean())

    # MA200 one month ago
    if len(hist) >= cfg['ma_long'] + 22:
        ma200_1m = float(hist['Close'].iloc[-(cfg['ma_long']+22):-(22)].mean())
    else:
        ma200_1m = ma200

    high_52w = float(hist['High'].tail(252).max())
    low_52w  = float(hist['Low'].tail(252).min())

    # Conditions
    c1 = close > ma50                          # price above MA50
    c2 = ma50 > ma150                          # MA50 > MA150
    c3 = ma150 > ma200                         # MA150 > MA200
    c4 = ma200 > ma200_1m                      # MA200 trending up
    c5 = close >= low_52w * (1 + cfg['min_above_low52'])   # 25%+ above 52w low
    c6 = close >= high_52w * (1 - cfg['max_below_high52']) # within 25% of 52w high

    passed = all([c1, c2, c3, c4, c5, c6])

    details = {
        'close': close, 'ma50': round(ma50, 2), 'ma150': round(ma150, 2),
        'ma200': round(ma200, 2), 'high_52w': round(high_52w, 2),
        'low_52w': round(low_52w, 2),
        'pct_from_high': round((high_52w - close) / high_52w * 100, 1),
        'pct_from_low': round((close - low_52w) / low_52w * 100, 1),
        'trend_checks': [c1, c2, c3, c4, c5, c6],
    }
    return passed, details


# ============================================================
# VCP Detection (Volatility Contraction Pattern)
# ============================================================
def detect_vcp(hist, cfg):
    """
    Detect VCP pattern:
    1. Find successive contractions (lower highs, higher lows)
    2. Each contraction range should be smaller than previous
    3. Volume should dry up in the tight area

    Returns (is_vcp: bool, vcp_info: dict)
    """
    lookback = min(cfg['vcp_lookback'], len(hist))
    if lookback < 20:
        return False, {}

    recent = hist.tail(lookback)
    closes = recent['Close'].values.astype(float)
    highs  = recent['High'].values.astype(float)
    lows   = recent['Low'].values.astype(float)
    vols   = recent['Volume'].values.astype(float)

    # Split into segments and find contractions
    # Use rolling 5-day high/low ranges
    window = 5
    ranges = []
    for i in range(0, lookback - window + 1, window):
        seg_high = highs[i:i+window].max()
        seg_low  = lows[i:i+window].min()
        seg_range = (seg_high - seg_low) / seg_low if seg_low > 0 else 999
        ranges.append(seg_range)

    if len(ranges) < 3:
        return False, {}

    # Count contractions (each range smaller than previous)
    contractions = 0
    for i in range(1, len(ranges)):
        if ranges[i] < ranges[i-1] * cfg['contraction_ratio']:
            contractions += 1

    # Last range should be tight
    last_range = ranges[-1]
    is_tight = last_range < cfg['max_last_range']

    # Volume dry-up: recent 10d avg vol vs 50d avg vol
    if len(hist) >= 50:
        vol_10d = float(hist['Volume'].tail(10).mean())
        vol_50d = float(hist['Volume'].tail(50).mean())
        vol_dry = vol_10d / vol_50d if vol_50d > 0 else 1.0
    else:
        vol_dry = 1.0

    is_vol_dry = vol_dry < cfg['vol_dry_ratio']

    is_vcp = (contractions >= cfg['min_contractions'] and is_tight)

    vcp_info = {
        'contractions':  contractions,
        'last_range_pct': round(last_range * 100, 1),
        'vol_dry_ratio': round(vol_dry, 2),
        'is_tight':      is_tight,
        'is_vol_dry':    is_vol_dry,
        'ranges':        [round(r*100, 1) for r in ranges[-5:]],
    }
    return is_vcp, vcp_info


# ============================================================
# Breakout Signal Evaluation
# ============================================================
def evaluate_breakout(hist, vcp_info, trend_info, cfg):
    """
    Evaluate how close the stock is to a breakout:
    - Pivot point = highest high in last N days
    - Breakout = close above pivot with volume surge
    - CRITICAL: detect earnings blowup / panic selling FIRST
    """
    close = float(hist['Close'].iloc[-1])
    open_p = float(hist['Open'].iloc[-1])
    pivot = float(hist['High'].tail(cfg['pivot_window']).max())

    # Today's change
    if len(hist) >= 2:
        prev_close = float(hist['Close'].iloc[-2])
        pct_chg = (close - prev_close) / prev_close * 100
    else:
        pct_chg = 0.0

    # Volume ratio (today vs 20d avg)
    vol_today = float(hist['Volume'].iloc[-1])
    vol_20d = float(hist['Volume'].tail(21).iloc[:-1].mean()) if len(hist) >= 21 else vol_today
    vol_ratio = vol_today / vol_20d if vol_20d > 0 else 0

    # Distance to pivot
    dist_to_pivot = (pivot - close) / pivot * 100 if pivot > 0 else 999

    # MA50 (real-time, using latest data including today)
    ma50_now = float(hist['Close'].tail(50).mean()) if len(hist) >= 50 else close

    signals  = []
    warnings = []
    score    = 0
    is_blown_up = False   # earnings blowup / panic flag

    # ============================================================
    # CIRCUIT BREAKER #1: Earnings blowup / panic selling detection
    # A single-day crash >= 5% with volume surge >= 2x = DEAD ON ARRIVAL
    # This MUST be checked BEFORE any positive scoring.
    # ============================================================
    if pct_chg <= -5.0 and vol_ratio >= 2.0:
        is_blown_up = True
        warnings.append(f'💀 暴雷断头铡刀! {pct_chg:.1f}%+天量{vol_ratio:.1f}x')
        score -= 20  # nuclear penalty, no amount of VCP/trend can save this
    elif pct_chg <= -5.0:
        is_blown_up = True
        warnings.append(f'💀 单日暴跌{pct_chg:.1f}%! 疑似暴雷')
        score -= 15
    elif pct_chg <= -3.0 and vol_ratio >= 2.5:
        is_blown_up = True
        warnings.append(f'⛔ 放量大跌{pct_chg:.1f}%+量比{vol_ratio:.1f}x 机构出逃')
        score -= 10

    # ============================================================
    # CIRCUIT BREAKER #2: Price crashed below MA50
    # If today's close is below MA50, the trend template is BROKEN.
    # ============================================================
    if close < ma50_now:
        pct_below_ma50 = (ma50_now - close) / ma50_now * 100
        warnings.append(f'⛔ 跌破MA50(${ma50_now:.2f}, 偏离-{pct_below_ma50:.1f}%)')
        score -= 5
        is_blown_up = True  # trend is broken

    # ============================================================
    # CIRCUIT BREAKER #3: Recent multi-day crash (past 3 days)
    # Even if today is flat, if the stock crashed hard in the last 3 days
    # ============================================================
    if len(hist) >= 4:
        close_3d_ago = float(hist['Close'].iloc[-4])
        chg_3d = (close - close_3d_ago) / close_3d_ago * 100
        if chg_3d <= -8.0:
            warnings.append(f'⛔ 近3日累计暴跌{chg_3d:.1f}%')
            score -= 8
            is_blown_up = True

    # ============================================================
    # CIRCUIT BREAKER #4: Abnormal down-volume (selling climax)
    # Volume > 3x average on a down day = institutional liquidation
    # ============================================================
    if pct_chg < 0 and vol_ratio >= 3.0:
        warnings.append(f'⛔ 下跌天量({vol_ratio:.1f}x) 机构清仓式出逃')
        score -= 6
        is_blown_up = True

    # --- If blown up, skip all positive scoring, go straight to output ---
    if is_blown_up:
        signal = '💀 暴雷/崩盘'
        action = '⚠️ 该股近期出现暴跌/暴雷信号，绝对不能碰！远离！'
        stop = round(close * 0.93, 2)
        return {
            'signal':     signal,
            'score':      score,
            'action':     action,
            'signals':    signals,
            'warnings':   warnings,
            'pct_chg':    round(pct_chg, 2),
            'vol_ratio':  round(vol_ratio, 1) if vol_ratio > 0.1 else None,
            'pivot':      round(pivot, 2),
            'dist_pivot': round(dist_to_pivot, 1),
            'stop':       stop,
            'target_1':   0,
            'target_2':   0,
        }

    # --- 1. Trend strength ---
    checks = trend_info.get('trend_checks', [])
    trend_score = sum(checks)
    if trend_score == 6:
        signals.append('完美趋势模板(6/6)')
        score += 3
    elif trend_score >= 5:
        signals.append(f'趋势模板({trend_score}/6)')
        score += 2

    # --- 2. VCP quality ---
    n_contr = vcp_info.get('contractions', 0)
    if n_contr >= 3:
        signals.append(f'VCP {n_contr}次收缩')
        score += 3
    elif n_contr >= 2:
        signals.append(f'VCP {n_contr}次收缩')
        score += 2

    if vcp_info.get('is_tight'):
        signals.append(f"窄幅整理({vcp_info['last_range_pct']}%)")
        score += 2

    if vcp_info.get('is_vol_dry'):
        signals.append(f"量能萎缩({vcp_info['vol_dry_ratio']}x)")
        score += 2
    else:
        warnings.append(f"量能未充分萎缩({vcp_info.get('vol_dry_ratio', 'N/A')}x)")

    # --- 3. Breakout proximity ---
    if close >= pivot:
        signals.append(f'🔥 突破枢轴点 ${pivot:.2f}')
        score += 4
        if vol_ratio >= 1.5:
            signals.append(f'放量突破({vol_ratio:.1f}x)')
            score += 3
        elif vol_ratio >= 1.0:
            signals.append(f'温和放量({vol_ratio:.1f}x)')
            score += 1
        else:
            warnings.append(f'突破但量能不足({vol_ratio:.1f}x)')
    elif dist_to_pivot <= cfg['breakout_margin'] * 100:
        signals.append(f'接近枢轴点(距{dist_to_pivot:.1f}%)')
        score += 2
    else:
        warnings.append(f'距枢轴点{dist_to_pivot:.1f}%')

    # --- 4. K-line (enhanced penalty for big drops) ---
    is_yang = (close > open_p) and (pct_chg > 0)
    if is_yang and pct_chg >= 2.0:
        signals.append(f'长阳+{pct_chg:.1f}%')
        score += 2
    elif is_yang:
        signals.append(f'阳线+{pct_chg:.1f}%')
        score += 1
    elif pct_chg <= -3.0:
        warnings.append(f'大阴线{pct_chg:.1f}%')
        score -= 3  # was -1, now properly penalized
    elif pct_chg <= -2.0:
        warnings.append(f'阴线{pct_chg:.1f}%')
        score -= 2
    elif pct_chg < 0:
        # mild red candle, small penalty
        score -= 0

    # --- 5. Down-volume warning (not blowup level, but still suspicious) ---
    if pct_chg < -1.0 and vol_ratio >= 1.5:
        warnings.append(f'下跌放量({pct_chg:.1f}%+{vol_ratio:.1f}x) 注意资金流向')
        score -= 2

    # --- Signal level ---
    has_breakout = any('突破枢轴' in s for s in signals)
    has_vol_surge = any('放量突破' in s for s in signals)
    has_approach = any('接近枢轴' in s for s in signals)

    if has_breakout and has_vol_surge:
        signal = '🟢 突破买入'
        action = '放量突破枢轴点！可立即建仓，止损设在枢轴点下方3-5%'
    elif has_breakout:
        signal = '🟡 突破待确认'
        action = '已突破枢轴但量能不足，等次日放量确认'
    elif has_approach and vcp_info.get('is_tight') and vcp_info.get('is_vol_dry'):
        signal = '🟡 蓄势待发'
        action = 'VCP形态完美+接近枢轴，设好突破提醒，随时准备出手'
    elif has_approach:
        signal = '🟡 接近突破'
        action = '接近枢轴点，关注量能变化'
    else:
        signal = '🔴 形态构建中'
        action = 'VCP形态在构建，耐心等待接近枢轴点'

    stop = round(pivot * 0.95, 2) if pivot > 0 else round(close * 0.93, 2)
    target_1 = round(close * 1.10, 2)
    target_2 = round(close * 1.20, 2)

    return {
        'signal':     signal,
        'score':      score,
        'action':     action,
        'signals':    signals,
        'warnings':   warnings,
        'pct_chg':    round(pct_chg, 2),
        'vol_ratio':  round(vol_ratio, 1) if vol_ratio > 0.1 else None,
        'pivot':      round(pivot, 2),
        'dist_pivot': round(dist_to_pivot, 1),
        'stop':       stop,
        'target_1':   target_1,
        'target_2':   target_2,
    }


# ============================================================
# Momentum Breakout Detection (趋势跟随 — 找已经突破的票)
# ============================================================

def calc_relative_strength(hist, all_cache, lookback=63):
    """
    Calculate Relative Strength (RS) percentile rank.
    Compare this stock's N-day return against all stocks in cache.
    Returns RS percentile (0-100), higher = stronger.
    """
    if len(hist) < lookback + 1:
        return 50  # default if not enough data

    stock_ret = (float(hist['Close'].iloc[-1]) / float(hist['Close'].iloc[-lookback]) - 1) * 100

    all_returns = []
    for ticker, df in all_cache.items():
        if df is None or not hasattr(df, 'empty') or df.empty or len(df) < lookback + 1:
            continue
        try:
            ret = (float(df['Close'].iloc[-1]) / float(df['Close'].iloc[-lookback]) - 1) * 100
            all_returns.append(ret)
        except Exception:
            continue

    if not all_returns:
        return 50

    # Percentile rank
    rank = sum(1 for r in all_returns if r < stock_ret) / len(all_returns) * 100
    return round(rank, 1)


def detect_momentum_breakout(hist, trend_info, all_cache, mcfg):
    """
    Detect momentum breakout — stocks that have ALREADY broken out.
    
    Criteria:
    1. Price at or near 20-day / 52-week high (already broke out)
    2. Volume surge confirming the breakout (institutional demand)
    3. Recent positive momentum (3d/5d gains)
    4. MA alignment: MA10 > MA21 > MA50 (short-term trend accelerating)
    5. Relative Strength in top 30%
    
    Returns (is_momentum: bool, momentum_info: dict)
    """
    if len(hist) < 50:
        return False, {}

    close = float(hist['Close'].iloc[-1])
    open_p = float(hist['Open'].iloc[-1])
    high_today = float(hist['High'].iloc[-1])

    # --- Price position ---
    high_20d = float(hist['High'].tail(20).max())
    high_50d = float(hist['High'].tail(50).max())
    high_52w = float(hist['High'].tail(252).max()) if len(hist) >= 252 else float(hist['High'].max())
    low_20d  = float(hist['Low'].tail(20).min())

    # Is it at/near new highs?
    at_20d_high = high_today >= high_20d * 0.99  # within 1% of 20d high
    at_50d_high = high_today >= high_50d * 0.99
    near_52w_high = close >= high_52w * (1 - mcfg['near_high_pct'])

    # --- Volume analysis ---
    vol_today = float(hist['Volume'].iloc[-1])
    vol_20d = float(hist['Volume'].tail(21).iloc[:-1].mean()) if len(hist) >= 21 else vol_today
    vol_ratio = vol_today / vol_20d if vol_20d > 0 else 0

    # --- Recent momentum ---
    if len(hist) >= 4:
        close_3d_ago = float(hist['Close'].iloc[-4])
        chg_3d = (close - close_3d_ago) / close_3d_ago * 100
    else:
        chg_3d = 0

    if len(hist) >= 6:
        close_5d_ago = float(hist['Close'].iloc[-6])
        chg_5d = (close - close_5d_ago) / close_5d_ago * 100
    else:
        chg_5d = 0

    # Today's change
    if len(hist) >= 2:
        prev_close = float(hist['Close'].iloc[-2])
        pct_chg = (close - prev_close) / prev_close * 100
    else:
        pct_chg = 0

    # --- MA alignment (short-term acceleration) ---
    ma10 = float(hist['Close'].tail(mcfg['ma_fast']).mean())
    ma21 = float(hist['Close'].tail(mcfg['ma_mid']).mean())
    ma50 = float(hist['Close'].tail(mcfg['ma_slow']).mean())

    ma_aligned = (close > ma10 > ma21 > ma50)
    ma10_slope = 0
    if len(hist) >= 15:
        ma10_5d_ago = float(hist['Close'].iloc[-15:-5].tail(10).mean())
        ma10_slope = (ma10 - ma10_5d_ago) / ma10_5d_ago * 100

    # --- Relative Strength ---
    rs_rank = calc_relative_strength(hist, all_cache, mcfg['rs_lookback'])

    # --- Gap-up filter (too big gap = earnings, risky) ---
    gap_up = (open_p - prev_close) / prev_close if len(hist) >= 2 and prev_close > 0 else 0

    # --- Consolidation base detection ---
    # Look for a period of low volatility before the breakout
    if len(hist) >= 30:
        base_range = (float(hist['High'].tail(30).iloc[:-5].max()) - 
                      float(hist['Low'].tail(30).iloc[:-5].min()))
        base_pct = base_range / float(hist['Close'].tail(30).iloc[-6]) * 100 if float(hist['Close'].tail(30).iloc[-6]) > 0 else 999
    else:
        base_pct = 999

    # --- Pullback from recent high (should be minimal for momentum) ---
    pullback = (high_20d - close) / high_20d if high_20d > 0 else 0

    # ============================================================
    # SCORING — Momentum Breakout
    # ============================================================
    signals = []
    warnings = []
    score = 0

    # 1. New high breakout (most important)
    if near_52w_high:
        signals.append(f'🔥 52周新高区域(距高{((high_52w-close)/high_52w*100):.1f}%)')
        score += 5
    elif at_50d_high:
        signals.append(f'突破50日新高')
        score += 4
    elif at_20d_high:
        signals.append(f'突破20日新高')
        score += 3
    else:
        # Not at any new high — not a momentum breakout
        return False, {}

    # 2. Volume confirmation
    if vol_ratio >= mcfg['strong_vol_ratio']:
        signals.append(f'🔥 机构级放量({vol_ratio:.1f}x)')
        score += 4
    elif vol_ratio >= mcfg['min_vol_ratio']:
        signals.append(f'放量确认({vol_ratio:.1f}x)')
        score += 2
    else:
        warnings.append(f'量能偏弱({vol_ratio:.1f}x)')
        score -= 1

    # 3. Recent momentum
    if chg_5d >= mcfg['min_5d_chg']:
        signals.append(f'5日动量+{chg_5d:.1f}%')
        score += 2
    if chg_3d >= mcfg['min_3d_chg']:
        signals.append(f'3日连涨+{chg_3d:.1f}%')
        score += 2
    elif chg_3d < 0:
        warnings.append(f'近3日回落{chg_3d:.1f}%')
        score -= 1

    # 4. MA alignment
    if ma_aligned:
        signals.append('均线多头加速(MA10>21>50)')
        score += 3
    elif close > ma21 > ma50:
        signals.append('均线多头(MA21>50)')
        score += 1
    else:
        warnings.append('短期均线未完全多头')
        score -= 1

    # 5. MA10 slope (acceleration)
    if ma10_slope > 1.0:
        signals.append(f'MA10加速上翘({ma10_slope:.1f}%)')
        score += 1

    # 6. Relative Strength
    if rs_rank >= 90:
        signals.append(f'🔥 RS顶级({rs_rank}%)')
        score += 3
    elif rs_rank >= mcfg['min_rs_rank']:
        signals.append(f'RS强势({rs_rank}%)')
        score += 2
    else:
        warnings.append(f'RS偏弱({rs_rank}%)')
        score -= 2

    # 7. Today's candle
    is_yang = close > open_p
    if is_yang and pct_chg >= 3.0:
        signals.append(f'🔥 大阳线+{pct_chg:.1f}%')
        score += 3
    elif is_yang and pct_chg >= 1.0:
        signals.append(f'阳线+{pct_chg:.1f}%')
        score += 1
    elif pct_chg <= -2.0:
        warnings.append(f'冲高回落{pct_chg:.1f}%')
        score -= 2

    # 8. Gap-up risk
    if gap_up > mcfg['max_gap_up']:
        warnings.append(f'⛔ 跳空过大({gap_up*100:.1f}%)，疑似事件驱动')
        score -= 3

    # 9. Pullback check
    if pullback > mcfg['max_pullback_from_high']:
        warnings.append(f'距20日高点回撤{pullback*100:.1f}%')
        score -= 1

    # --- Circuit breakers (same as VCP) ---
    if pct_chg <= -5.0 and vol_ratio >= 2.0:
        return False, {}  # blown up
    if close < ma50:
        return False, {}  # trend broken

    # Minimum score threshold
    is_momentum = score >= 6

    # --- Signal level ---
    if score >= 15:
        signal = '🟢 强势突破'
        action = '放量创新高+动量强劲，可立即建仓！止损设在MA21下方'
    elif score >= 10:
        signal = '🟢 确认突破'
        action = '突破确认，可建仓。止损设在突破日低点或MA21下方'
    elif score >= 6:
        signal = '🟡 突破初期'
        action = '刚开始突破，可轻仓试探。关注次日能否站稳'
    else:
        signal = '🔴 动量不足'
        action = '虽在高位但动量不够，观望为主'

    # Stop & targets
    stop = round(max(ma21, low_20d) * 0.98, 2)  # MA21 or 20d low, whichever higher
    target_1 = round(close * 1.10, 2)
    target_2 = round(close * 1.20, 2)

    momentum_info = {
        'signal':       signal,
        'score':        score,
        'action':       action,
        'signals':      signals,
        'warnings':     warnings,
        'pct_chg':      round(pct_chg, 2),
        'chg_3d':       round(chg_3d, 1),
        'chg_5d':       round(chg_5d, 1),
        'vol_ratio':    round(vol_ratio, 1),
        'rs_rank':      rs_rank,
        'ma10':         round(ma10, 2),
        'ma21':         round(ma21, 2),
        'ma50':         round(ma50, 2),
        'ma10_slope':   round(ma10_slope, 2),
        'high_20d':     round(high_20d, 2),
        'high_52w':     round(high_52w, 2),
        'pct_from_52h': round((high_52w - close) / high_52w * 100, 1),
        'pullback':     round(pullback * 100, 1),
        'base_pct':     round(base_pct, 1) if base_pct < 900 else None,
        'stop':         stop,
        'target_1':     target_1,
        'target_2':     target_2,
    }

    return is_momentum, momentum_info


# ============================================================
# Main
# ============================================================
def run_us_screen():
    import yfinance as yf

    cfg = VCP_CONFIG
    print("=" * 70)
    print("  美股选股器 — 动量突破 + VCP 双策略扫描")
    print(f"  S&P500 + 全美股 + 矿业专属")
    print(f"  市值范围: ${cfg['min_market_cap']/1e6:.0f}M ~ ${cfg['max_market_cap']/1e9:.0f}B USD")
    print("=" * 70)

    # ---- Step 1: Pool (S&P 500 + All US + Mining) ----
    print("\n[STEP 1] Loading stock pool (S&P 500 + All US + Mining)...")
    pool = get_all_us_tickers()
    info_map = {t[0]: {'name': t[1], 'sector': t[2], 'industry': t[3]} for t in pool}

    # Filter out known delisted tickers
    delisted = load_delisted()
    all_tickers = [t[0] for t in pool]
    if delisted:
        tickers = [t for t in all_tickers if t not in delisted]
        print(f"  [DELISTED] Skipped {len(all_tickers) - len(tickers)} known delisted tickers "
              f"(blacklist: {len(delisted)})")
    else:
        tickers = all_tickers

    # ---- Step 2: History ----
    print("\n[STEP 2] Loading history data (yfinance, free & no rate limit)...")
    cache_data = load_us_cache()
    cache_data = fetch_history_batch(tickers, cache_data, period_days=1100)
    save_us_cache(cache_data)

    # ---- Step 3: Liquidity + Deep-V + Trend pre-screening ----
    # Two paths: VCP (strict trend template) and Momentum (relaxed, price > MA50)
    print(f"\n[STEP 3] Pre-screening on {len(tickers)} stocks (liquidity → deep-V → trend)...")
    trend_candidates = []      # [(ticker, hist, trend_info)] — strict trend template for VCP
    momentum_pool = []         # [(ticker, hist, trend_info)] — relaxed for momentum breakout
    no_data = 0
    killed_liquidity = 0
    killed_deepv = 0

    for idx, ticker in enumerate(tickers):
        if (idx + 1) % 500 == 0:
            print(f"  ... {idx+1}/{len(tickers)} scanned | vcp_pass: {len(trend_candidates)} "
                  f"| mom_pass: {len(momentum_pool)} "
                  f"| liq_kill: {killed_liquidity} | deepv_kill: {killed_deepv}")

        hist = cache_data.get(ticker)
        if hist is None or not hasattr(hist, 'empty') or hist.empty or len(hist) < 50:
            no_data += 1
            continue

        hist = hist.sort_index()
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            if col in hist.columns:
                hist[col] = pd.to_numeric(hist[col], errors='coerce')
        hist = hist.dropna(subset=['Close'])
        if len(hist) < 50:
            no_data += 1
            continue

        # --- Liquidity floor: 50d avg vol > 500K shares & avg dollar vol > $10M ---
        if len(hist) >= 50:
            avg_vol_50d = float(hist['Volume'].tail(50).mean())
            avg_close_50d = float(hist['Close'].tail(50).mean())
            avg_dollar_vol = avg_vol_50d * avg_close_50d
        else:
            avg_vol_50d = float(hist['Volume'].mean())
            avg_close_50d = float(hist['Close'].mean())
            avg_dollar_vol = avg_vol_50d * avg_close_50d

        if avg_vol_50d < cfg.get('min_avg_vol_50d', 500_000) or \
           avg_dollar_vol < cfg.get('min_avg_dollar_vol', 10_000_000):
            killed_liquidity += 1
            continue

        # --- Deep-V rejection ---
        high_52w = float(hist['High'].tail(252).max()) if len(hist) >= 252 else float(hist['High'].max())
        low_52w  = float(hist['Low'].tail(252).min()) if len(hist) >= 252 else float(hist['Low'].min())
        max_dd = (high_52w - low_52w) / high_52w if high_52w > 0 else 0
        if max_dd > cfg.get('max_drawdown_52w', 0.65):
            killed_deepv += 1
            continue

        # --- Basic trend info (used by both paths) ---
        close = float(hist['Close'].iloc[-1])
        ma50 = float(hist['Close'].tail(50).mean()) if len(hist) >= 50 else close

        # Build a basic trend_info dict for momentum path
        ma150 = float(hist['Close'].tail(150).mean()) if len(hist) >= 150 else ma50
        ma200 = float(hist['Close'].tail(200).mean()) if len(hist) >= 200 else ma150
        basic_trend_info = {
            'close': close, 'ma50': round(ma50, 2),
            'ma150': round(ma150, 2), 'ma200': round(ma200, 2),
            'high_52w': round(high_52w, 2), 'low_52w': round(low_52w, 2),
            'pct_from_high': round((high_52w - close) / high_52w * 100, 1) if high_52w > 0 else 0,
            'pct_from_low': round((close - low_52w) / low_52w * 100, 1) if low_52w > 0 else 0,
            'trend_checks': [],
        }

        # --- Path A: Strict trend template for VCP ---
        if len(hist) >= cfg['min_days']:
            trend_ok, trend_info = check_trend_template(hist, cfg)
            if trend_ok:
                trend_candidates.append((ticker, hist, trend_info))

        # --- Path B: Relaxed filter for Momentum Breakout ---
        # Only need: price > MA50 (basic uptrend) and enough data (50 days)
        if close > ma50:
            momentum_pool.append((ticker, hist, basic_trend_info))

    print(f"  [FILTER] Liquidity killed: {killed_liquidity} | Deep-V killed: {killed_deepv} "
          f"| No data: {no_data}")
    print(f"  [TREND] {len(trend_candidates)} stocks passed strict trend template (for VCP)")
    print(f"  [MOMENTUM] {len(momentum_pool)} stocks passed relaxed filter (price > MA50)")

    # ---- Step 4: Fetch market cap & info ----
    # Need info for both VCP and momentum candidates
    all_candidate_tickers = set(t[0] for t in trend_candidates) | set(t[0] for t in momentum_pool)
    print(f"\n[STEP 4] Fetching info for {len(all_candidate_tickers)} candidate stocks...")
    info_cache_file = './cache/us_info_cache.pkl'
    info_cache = {}
    if os.path.exists(info_cache_file):
        try:
            with open(info_cache_file, 'rb') as f:
                info_cache = pickle.load(f)
            cache_ts = info_cache.get('_timestamp', 0)
            if time.time() - cache_ts > 7 * 86400:
                info_cache = {}
                print("  [INFO] Cache expired (>7 days), refreshing...")
            else:
                cached_count = len([k for k in info_cache if k != '_timestamp'])
                print(f"  [INFO] Using cached info ({cached_count} stocks, "
                      f"{(time.time()-cache_ts)/3600:.0f}h old)")
        except Exception:
            info_cache = {}

    trend_tickers = list(all_candidate_tickers)
    need_info = [t for t in trend_tickers if t not in info_cache]
    if need_info:
        print(f"  [INFO] Fetching info for {len(need_info)} new stocks "
              f"(cached: {len(trend_tickers)-len(need_info)})...")
        for i, ticker in enumerate(need_info):
            try:
                t_obj = yf.Ticker(ticker)
                info = t_obj.info
                info_cache[ticker] = {
                    'market_cap':  info.get('marketCap', 0) or 0,
                    'sector':      info.get('sector', info_map.get(ticker, {}).get('sector', 'N/A')),
                    'industry':    info.get('industry', info_map.get(ticker, {}).get('industry', 'N/A')),
                    'short_name':  info.get('shortName', ticker),
                    'shares_out':  info.get('sharesOutstanding', 0) or 0,
                }
                if (i + 1) % 50 == 0:
                    print(f"    ... {i+1}/{len(need_info)} info done")
                time.sleep(0.05)
            except Exception:
                info_cache[ticker] = {
                    'market_cap': 0,
                    'sector': info_map.get(ticker, {}).get('sector', 'N/A'),
                    'industry': info_map.get(ticker, {}).get('industry', 'N/A'),
                    'short_name': ticker, 'shares_out': 0,
                }
        info_cache['_timestamp'] = time.time()
        with open(info_cache_file, 'wb') as f:
            pickle.dump(info_cache, f)

    # ---- Step 5: VCP (DISABLED — currently not useful in this market) ----
    # VCP requires MA50>MA150>MA200 which rarely holds in post-crash recovery.
    # To re-enable, uncomment the VCP block below.
    results = []
    vcp_pass = 0
    # print(f"\n[STEP 5] VCP screening on {len(trend_candidates)} trend candidates...")
    # [VCP code disabled]

    # ---- Step 5.5: Dual-Pool Momentum Scan ----
    # Pool A: Right-side breakout (真趋势突破, normal position)
    # Pool B: Left-side recovery (超跌修复, light position)
    mcfg = MOMENTUM_CONFIG
    pa_cfg = POOL_A_CONFIG
    pb_cfg = POOL_B_CONFIG
    print(f"\n[STEP 5.5] Dual-Pool scan on {len(momentum_pool)} momentum candidates...")
    print(f"  Pool A (右侧突破): RS>{pa_cfg['min_rs_rank']}% + 距52高<{pa_cfg['max_pct_from_52h']}% + 量比>{pa_cfg['min_vol_ratio']}x + 均线多头")
    print(f"  Pool B (左侧修复): 距52高{pb_cfg['min_pct_from_52h']}-{pb_cfg['max_pct_from_52h']}% + 5日涨>{pb_cfg['min_5d_chg']}% + MA10拐头")
    pool_a_results = []  # right-side breakout
    pool_b_results = []  # left-side recovery
    momentum_pass = 0

    for ticker, hist, trend_info in momentum_pool:
        # Market cap filter
        ic = info_cache.get(ticker, {})
        mcap = ic.get('market_cap', 0)
        if mcap < cfg['min_market_cap'] or mcap > cfg['max_market_cap']:
            continue

        is_mom, mom_info = detect_momentum_breakout(hist, trend_info, cache_data, mcfg)
        if not is_mom:
            continue
        momentum_pass += 1

        # Build result dict
        result = {
            'ticker':       ticker,
            'name':         ic.get('short_name', ticker),
            'sector':       ic.get('sector', 'N/A'),
            'industry':     ic.get('industry', 'N/A'),
            'close':        trend_info['close'],
            'pct_chg':      mom_info['pct_chg'],
            'chg_3d':       mom_info['chg_3d'],
            'chg_5d':       mom_info['chg_5d'],
            'mcap_b':       round(mcap / 1e9, 1),
            'ma10':         mom_info['ma10'],
            'ma21':         mom_info['ma21'],
            'ma50':         mom_info['ma50'],
            'vol_ratio':    mom_info['vol_ratio'],
            'rs_rank':      mom_info['rs_rank'],
            'high_20d':     mom_info['high_20d'],
            'high_52w':     mom_info['high_52w'],
            'pct_from_52h': mom_info['pct_from_52h'],
            'signal':       mom_info['signal'],
            'score':        mom_info['score'],
            'action':       mom_info['action'],
            'sig_detail':   mom_info['signals'],
            'sig_warn':     mom_info['warnings'],
            'stop':         mom_info['stop'],
            'target_1':     mom_info['target_1'],
            'target_2':     mom_info['target_2'],
            'ma10_slope':   mom_info.get('ma10_slope', 0),
        }

        # ---- Classify into Pool A or Pool B ----
        pct_from_52h = mom_info['pct_from_52h']  # how far from 52w high (%)
        rs = mom_info['rs_rank']
        vr = mom_info['vol_ratio']
        ma_aligned = (mom_info['ma10'] > mom_info['ma21'] > mom_info['ma50'])
        ma10_slope = mom_info.get('ma10_slope', 0)

        # Pool A: Right-side breakout
        is_pool_a = (
            rs >= pa_cfg['min_rs_rank'] and
            pct_from_52h <= pa_cfg['max_pct_from_52h'] and
            vr >= pa_cfg['min_vol_ratio'] and
            (not pa_cfg['require_ma_aligned'] or ma_aligned) and
            mom_info['score'] >= pa_cfg['min_score']
        )

        # Pool B: Left-side recovery
        is_pool_b = (
            pct_from_52h >= pb_cfg['min_pct_from_52h'] and
            pct_from_52h <= pb_cfg['max_pct_from_52h'] and
            mom_info['chg_5d'] >= pb_cfg['min_5d_chg'] and
            vr >= pb_cfg['min_vol_ratio'] and
            vr <= pb_cfg['max_vol_ratio'] and
            (not pb_cfg['require_ma10_upturn'] or ma10_slope > 0) and
            mom_info['score'] >= pb_cfg['min_score']
        )

        if is_pool_a:
            # Override targets/stops for Pool A
            result['pool'] = 'A'
            result['target_1'] = round(result['close'] * (1 + pa_cfg['target_pct_1']), 2)
            result['target_2'] = round(result['close'] * (1 + pa_cfg['target_pct_2']), 2)
            result['stop'] = round(result['close'] * (1 - pa_cfg['stop_pct']), 2)
            result['action'] = '🅰️ 右侧突破！正常仓位打，止损-5%，目标+10%~+20%'
            pool_a_results.append(result)
        elif is_pool_b:
            # Override targets/stops for Pool B
            result['pool'] = 'B'
            result['target_1'] = round(result['close'] * (1 + pb_cfg['target_pct_1']), 2)
            result['target_2'] = round(result['close'] * (1 + pb_cfg['target_pct_2']), 2)
            result['stop'] = round(result['close'] * (1 - pb_cfg['stop_pct']), 2)
            result['action'] = '🅱️ 左侧修复！轻仓试探(1/3~1/2仓)，止损-7%，目标+8%~+15%'
            pool_b_results.append(result)
        # else: neither pool — skip (noise)

    print(f"  [RESULT] Pool A (右侧突破): {len(pool_a_results)} 只")
    print(f"  [RESULT] Pool B (左侧修复): {len(pool_b_results)} 只")
    print(f"  [RESULT] 未分类(噪音): {momentum_pass - len(pool_a_results) - len(pool_b_results)} 只")

    # ---- Step 5.6: Pool C — Chip-Peak Strategy (筹码峰战法) ----
    # Reuse A-share chip analysis pipeline via us_chip_backtest adapter
    print(f"\n[STEP 5.6] Pool C 筹码峰战法扫描 (对齐A股 PF 2.58)...")
    pool_c_results = []
    pool_c_signal_pool = []  # signals waiting for breakout confirmation
    try:
        from us_chip_backtest import (
            _us_to_ashare_df, analyze_one_stock_chip,
            count_green_dims_c, POOL_C_BT_CONFIG, CHIP_CFG,
        )
        from chip_screener_v3 import V3_CONFIG

        # Load signal pool from cache (for breakout confirmation tracking)
        signal_pool_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        'cache', 'us_chip_signal_pool.json')
        import json
        old_pool = []
        if os.path.exists(signal_pool_file):
            try:
                with open(signal_pool_file, 'r') as f:
                    old_pool = json.load(f)
                print(f"  [POOL] Loaded {len(old_pool)} pending signals from cache")
            except Exception:
                old_pool = []

        # Scan all stocks with enough history for chip analysis
        chip_candidates = []
        chip_scanned = 0
        for ticker in tickers:
            hist = cache_data.get(ticker)
            if hist is None or not hasattr(hist, 'empty') or hist.empty:
                continue
            if len(hist) < POOL_C_BT_CONFIG['min_history_days']:
                continue

            hist = hist.sort_index()
            for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
                if col in hist.columns:
                    hist[col] = pd.to_numeric(hist[col], errors='coerce')
            hist = hist.dropna(subset=['Close'])
            if len(hist) < POOL_C_BT_CONFIG['min_history_days']:
                continue

            close = float(hist['Close'].iloc[-1])
            if close <= 0:
                continue

            # Quick pre-filter: price > MA50 (basic uptrend)
            ma50 = float(hist['Close'].tail(50).mean()) if len(hist) >= 50 else close
            if close <= ma50:
                continue

            # Quick liquidity check
            avg_vol = float(hist['Volume'].tail(50).mean()) if len(hist) >= 50 else 0
            if avg_vol * close < 5_000_000:  # $5M daily dollar volume
                continue

            # Fast vectorized Layer 1 pre-screen (avoid expensive chip calc)
            vols = hist['Volume'].values.astype(float)
            baseline = np.mean(vols[-60:]) if len(vols) >= 60 else np.mean(vols)
            if baseline <= 0:
                continue
            quiet_thresh = baseline * CHIP_CFG.get('vol_quiet_threshold', 0.60)
            scan_region = vols[-35:-5] if len(vols) >= 35 else vols[:-5]
            streak = 0; max_streak = 0
            for v in scan_region:
                if not np.isnan(v) and v < quiet_thresh:
                    streak += 1; max_streak = max(max_streak, streak)
                else:
                    streak = 0
            if max_streak < CHIP_CFG.get('vol_quiet_min_streak', 2):
                continue

            chip_scanned += 1
            result = analyze_one_stock_chip(hist, close, CHIP_CFG)
            if result is None:
                continue

            if result['score'] < POOL_C_BT_CONFIG['min_score_entry']:
                continue
            if result['score'] > POOL_C_BT_CONFIG.get('max_score_entry', 999):
                continue
            if not result['scores'].get('dyn_pass', False):
                continue

            # Chip quality filters (对齐回测)
            chip = result['chip_today']
            if chip.get('peak_count', 99) > POOL_C_BT_CONFIG.get('max_peaks_entry', 2):
                continue
            if chip.get('concentration_90', 1.0) > POOL_C_BT_CONFIG.get('max_conc90_entry', 0.28):
                continue
            main_peak = chip.get('main_peak_price', 0)
            if main_peak > 0:
                pk_dev = (close - main_peak) / main_peak
                if pk_dev < POOL_C_BT_CONFIG.get('min_peak_deviation', -0.05):
                    continue
                if pk_dev > POOL_C_BT_CONFIG.get('max_peak_deviation', 0.12):
                    continue

            dyn = result['dynamics']
            rd = result['readiness']
            ic = info_cache.get(ticker, {})

            cand = {
                'ticker':         ticker,
                'name':           ic.get('short_name', ticker),
                'sector':         ic.get('sector', 'N/A'),
                'industry':       ic.get('industry', 'N/A'),
                'close':          close,
                'score':          result['score'],
                'signal':         dyn['signal'],
                'signal_cn':      dyn['signal_cn'],
                'conc_change':    round(dyn['conc_change'] * 100, 1),
                'center_drift':   round(dyn['center_drift_pct'] * 100, 1),
                'winner_ratio':   round(chip['winner_ratio'] * 100, 1),
                'main_peak':      round(chip['main_peak_price'], 2),
                'peak_deviation': round(rd.get('peak_deviation', 0) * 100, 1) if rd.get('peak_deviation') is not None else 0,
                'concentration':  round(chip.get('concentration_90', 0), 3),
                'peak_count':     chip.get('peak_count', 0),
                'pool':           'C',
                'target_10':      round(close * 1.10, 2),
                'stop_7':         round(close * 0.93, 2),
            }
            cand['green_count'] = count_green_dims_c(cand | {
                'scores': result['scores'],
                'conc_change': dyn['conc_change'],
                'center_drift': dyn['center_drift_pct'],
                'winner_ratio': chip['winner_ratio'],
                'peak_deviation': rd.get('peak_deviation'),
            })

            # Pick ≥5 green (≥6 for signal pool, 5 for observation)
            if cand['green_count'] >= 5:
                chip_candidates.append(cand)

            if chip_scanned % 200 == 0:
                print(f"  ... {chip_scanned} scanned | {len(chip_candidates)} candidates", flush=True)

        # Sort by green_count desc, score desc
        chip_candidates.sort(key=lambda x: (x['green_count'], x['score']), reverse=True)

        # Check breakout confirmation for old pool signals
        breakout_confirmed = []
        still_waiting = []
        today_dt = datetime.now().strftime('%Y-%m-%d')
        for sig in old_pool:
            sig_age = (datetime.now() - datetime.strptime(sig['signal_date'], '%Y-%m-%d')).days
            if sig_age > 4:
                continue  # expired
            tk = sig['ticker']
            hist = cache_data.get(tk)
            if hist is None or hist.empty:
                still_waiting.append(sig)
                continue
            hist = hist.sort_index()
            last = hist.iloc[-1]
            c_close = float(last['Close'])
            c_open = float(last['Open'])
            c_vol = float(last['Volume'])
            if c_close <= 0 or c_open <= 0:
                still_waiting.append(sig)
                continue
            # Check breakout: +1% yang + 1.3x vol
            pct_chg = (c_close - c_open) / c_open * 100
            if len(hist) >= 2:
                prev_close = float(hist.iloc[-2]['Close'])
                if prev_close > 0:
                    pct_chg = (c_close - prev_close) / prev_close * 100
            vol_5d = float(hist['Volume'].tail(6).iloc[:-1].mean()) if len(hist) >= 6 else c_vol
            vol_ratio = c_vol / vol_5d if vol_5d > 0 else 0

            if pct_chg >= 1.0 and vol_ratio >= 1.3:
                sig['breakout_date'] = today_dt
                sig['breakout_price'] = round(c_close, 2)
                sig['breakout_pct'] = round(pct_chg, 1)
                sig['breakout_vol_ratio'] = round(vol_ratio, 1)
                breakout_confirmed.append(sig)
            else:
                still_waiting.append(sig)

        # Add new candidates to signal pool (only ≥6 green enter pool)
        existing_tickers = set(s['ticker'] for s in still_waiting + breakout_confirmed)
        for c in chip_candidates[:10]:  # top 10 new signals
            if c['green_count'] < POOL_C_BT_CONFIG['min_green_count']:
                continue  # only ≥6 green enter signal pool
            if c['ticker'] not in existing_tickers:
                still_waiting.append({
                    'ticker': c['ticker'],
                    'name': c['name'],
                    'signal_date': today_dt,
                    'signal_price': c['close'],
                    'score': c['score'],
                    'green_count': c['green_count'],
                    'signal_cn': c.get('signal_cn', ''),
                    'sector': c['sector'],
                })

        # Save updated signal pool
        os.makedirs(os.path.dirname(signal_pool_file), exist_ok=True)
        with open(signal_pool_file, 'w') as f:
            json.dump(still_waiting, f, indent=2, default=str)

        pool_c_results = chip_candidates
        pool_c_signal_pool = still_waiting

        print(f"  [RESULT] Pool C (筹码峰): {len(chip_candidates)} 只候选 (scanned {chip_scanned})")
        print(f"  [RESULT] 🔫 突破确认: {len(breakout_confirmed)} 只 | 👀 等待中: {len(still_waiting)} 只")

    except ImportError as e:
        print(f"  [WARN] Pool C disabled: {e}")
        pool_c_results = []
        breakout_confirmed = []

    # ---- Step 6: Output (also save to txt) ----
    # Tee: capture all output to both terminal and buffer for txt export
    today_str = datetime.now().strftime('%Y-%m-%d')
    _output_buf = io.StringIO()
    _orig_stdout = sys.stdout

    class _TeeWriter:
        """Write to both terminal and buffer simultaneously."""
        def __init__(self, *writers):
            self.writers = writers
        def write(self, s):
            for w in self.writers:
                w.write(s)
        def flush(self):
            for w in self.writers:
                w.flush()

    sys.stdout = _TeeWriter(_orig_stdout, _output_buf)

    print(f"\n{'='*70}")
    print(f"  美股双池选股结果 — {today_str}")
    print(f"{'='*70}")
    print(f"\n{'='*70}")
    print(f"  筛选漏斗: {len(tickers)} 总池")
    print(f"    → 流动性过滤掉 {killed_liquidity} 只 (50d均量<50万股 或 日均额<$1000万)")
    print(f"    → 深V过滤掉 {killed_deepv} 只 (52周最大回撤>{cfg.get('max_drawdown_52w',0.65)*100:.0f}%)")
    print(f"    → 动量路径: {len(momentum_pool)} 候选(price>MA50) → {momentum_pass} 动量信号")
    print(f"    → 🅰️ 右侧突破池: {len(pool_a_results)} 只 (真趋势, 正常仓位)")
    print(f"    → 🅱️ 左侧修复池: {len(pool_b_results)} 只 (超跌反弹, 轻仓)")
    print(f"{'='*70}")

    # ============================================================
    # Helper: print pool detail
    # ============================================================
    def _print_pool_detail(pool_results, pool_label, pool_emoji, target_desc, stop_desc,
                           group_by_sector=True):
        """Print detailed results for a pool."""
        if not pool_results:
            print(f"\n  {pool_emoji} 【{pool_label}】未找到符合条件的标的")
            return

        df = pd.DataFrame(pool_results).sort_values('score', ascending=False)

        print(f"\n{'='*70}")
        print(f"  {pool_emoji} 【{pool_label}】{len(df)} 只 {pool_emoji}")
        print(f"  {target_desc}")
        print(f"{'='*70}")

        cols = ['ticker','name','close','pct_chg','chg_3d','chg_5d','mcap_b',
                'vol_ratio','rs_rank','pct_from_52h','score']
        col_names = {
            'ticker':'代码','name':'名称','close':'价格','pct_chg':'涨跌%',
            'chg_3d':'3日%','chg_5d':'5日%','mcap_b':'市值$B',
            'vol_ratio':'量比','rs_rank':'RS%','pct_from_52h':'距52高%','score':'总分',
        }

        if group_by_sector:
            # Sector summary table
            for sector in sorted(df['sector'].unique()):
                sec_df = df[df['sector'] == sector]
                print(f"\n  === {sector} ({len(sec_df)}只) ===")
                print(sec_df[cols].rename(columns=col_names).to_string(index=False))
        else:
            # Flat table sorted by score descending (no sector grouping)
            print(f"\n  === 按总分降序排列 ===")
            print(df[cols].rename(columns=col_names).to_string(index=False))

        # Signal detail (sorted by priority then score)
        print(f"\n  {'='*70}")
        print(f"  === 【{pool_label}】逐票分析 ===")
        print(f"  {'='*70}")

        priority_m = {'🟢 强势突破': 0, '🟢 确认突破': 1, '🟡 突破初期': 2, '🔴 动量不足': 3}
        df['_ord'] = df['signal'].map(
            lambda s: next((v for k, v in priority_m.items() if k in s), 99)
        )
        df = df.sort_values(['_ord', 'score'], ascending=[True, False])

        counts = {'green': 0, 'yellow': 0, 'red': 0}
        for _, r in df.iterrows():
            if '🟢' in r['signal']:
                counts['green'] += 1
            elif '🟡' in r['signal']:
                counts['yellow'] += 1
            else:
                counts['red'] += 1

            print(f"\n  {r['signal']}  {r['name']}({r['ticker']})  "
                  f"现价:${r['close']:.2f}  市值:${r['mcap_b']}B  总分:{r['score']}")
            print(f"    涨跌:{r['pct_chg']}%  3日:{r['chg_3d']}%  5日:{r['chg_5d']}%  "
                  f"量比:{r['vol_ratio']}x  RS:{r['rs_rank']}%")
            print(f"    MA10:${r['ma10']}  MA21:${r['ma21']}  MA50:${r['ma50']}  "
                  f"距52周高:{r['pct_from_52h']}%")
            print(f"    行业: {r['industry']}")
            if r['sig_detail']:
                print(f"    ✅ 利好: {' | '.join(r['sig_detail'])}")
            if r['sig_warn']:
                print(f"    ⚠️  注意: {' | '.join(r['sig_warn'])}")
            print(f"    📋 操作: {r['action']}")
            print(f"    🎯 目标: 一目标 ${r['target_1']}({target_desc.split('目标')[0].strip()}) | 二目标 ${r['target_2']}")
            print(f"    🛑 止损: ${r['stop']}({stop_desc})")

        print(f"\n  {'─'*70}")
        print(f"  {pool_label}汇总: 🟢强势/确认 {counts['green']}只 | "
              f"🟡初期 {counts['yellow']}只 | 🔴不足 {counts['red']}只")
        print(f"  {'─'*70}")

    # ============================================================
    # Pool A: Right-side Breakout (右侧突破 — 真趋势, 正常仓位)
    # ============================================================
    _print_pool_detail(
        pool_a_results,
        '🅰️ 右侧突破池 — 真趋势跟随',
        '🚀',
        '目标+10%~+20% | 正常仓位 | 高胜率',
        '入场价下方5%'
    )

    # ============================================================
    # Pool B: Left-side Recovery (左侧修复 — 超跌反弹, 轻仓)
    # ============================================================
    _print_pool_detail(
        pool_b_results,
        '🅱️ 左侧修复池 — 超跌反弹',
        '🔄',
        '目标+8%~+15% | 轻仓(1/3~1/2) | 止损更紧',
        '入场价下方7%，跑得快',
        group_by_sector=False
    )

    # ============================================================
    # Pool C: Chip-Peak Strategy (筹码峰战法 — 缩量再起飞)
    # ============================================================
    if pool_c_results or (hasattr(locals(), 'breakout_confirmed') and breakout_confirmed):
        print(f"\n{'='*70}")
        print(f"  🅲 筹码峰战法池 — 缩量收敛→突破起飞 (PF 2.58)")
        print(f"{'='*70}")

        # Show breakout confirmed first (直接买！)
        try:
            if breakout_confirmed:
                print(f"\n  🔫🔫🔫 【今日突破确认 — 直接买入】 🔫🔫🔫")
                print(f"  " + "─" * 60)
                for i, sig in enumerate(breakout_confirmed, 1):
                    print(f"  {i}. 🔫 {sig['ticker']} ({sig.get('name', '')})  {sig.get('sector', '')}")
                    print(f"     信号日: {sig['signal_date']}  信号价: ${sig['signal_price']:.2f}")
                    print(f"     突破日: {sig['breakout_date']}  突破价: ${sig['breakout_price']:.2f}")
                    print(f"     今日涨幅: +{sig['breakout_pct']:.1f}%  量比: {sig['breakout_vol_ratio']:.1f}x")
                    entry = sig['breakout_price']
                    print(f"     🎯 入场: ${entry:.2f}  止盈+10%: ${entry*1.10:.2f}  止损-7%: ${entry*0.93:.2f}")
                print()
        except NameError:
            pass

        # Show signal pool (waiting for breakout)
        if pool_c_signal_pool:
            print(f"  👀 【信号池 — 等待突破确认】({len(pool_c_signal_pool)} 只)")
            print(f"  " + "─" * 60)
            for i, sig in enumerate(pool_c_signal_pool[:10], 1):
                age = (datetime.now() - datetime.strptime(sig['signal_date'], '%Y-%m-%d')).days
                remain = max(0, 4 - age)
                print(f"  {i}. {sig['ticker']:<6} ({sig.get('name', '')[:15]}) "
                      f"score={sig['score']} {sig.get('green_count', '?')}🟢 "
                      f"信号日:{sig['signal_date']} 剩余{remain}天")
            print()

        # Show new candidates (today's screening)
        if pool_c_results:
            # Split into tradeable (≥6 green) and observation (5 green)
            tradeable = [c for c in pool_c_results if c['green_count'] >= POOL_C_BT_CONFIG['min_green_count']]
            observation = [c for c in pool_c_results if c['green_count'] < POOL_C_BT_CONFIG['min_green_count']]

            if tradeable:
                print(f"  📡 【今日新信号 ≥6🟢】({len(tradeable)} 只)")
                print(f"  " + "─" * 60)
                for i, c in enumerate(tradeable[:10], 1):
                    print(f"  {i}. {c['ticker']:<6} ({c['name'][:15]})  {c['sector']}")
                    print(f"     ${c['close']:.2f} | score={c['score']} {c['green_count']}🟢 | "
                          f"{c['signal_cn']} | 集中度变化:{c['conc_change']:+.1f}% | "
                          f"重心漂移:{c['center_drift']:+.1f}%")
                    print(f"     获利盘:{c['winner_ratio']:.1f}% | 主峰:${c['main_peak']:.2f} | "
                          f"峰偏离:{c['peak_deviation']:+.1f}% | 峰数:{c['peak_count']}")
                    print(f"     🎯 止盈+10%: ${c['target_10']:.2f}  止损-7%: ${c['stop_7']:.2f}")
                print()

            if observation:
                print(f"  👁️  【观察中 5🟢 — 接近触发】({len(observation)} 只)")
                print(f"  " + "─" * 60)
                for i, c in enumerate(observation[:5], 1):
                    print(f"  {i}. {c['ticker']:<6} ({c['name'][:15]})  score={c['score']} {c['green_count']}🟢 | {c['signal_cn']}")
                print()

        print(f"  ⚠️  Pool C 纪律提醒:")
        print(f"  ┌─────────────────────────────────────────────────────┐")
        print(f"  │  1. 只买🔫突破确认的 (不要盲目追信号池)              │")
        print(f"  │  2. +10% 卖1/3 → 移成本止损 → MA10追踪剩余          │")
        print(f"  │  3. -7% 硬止损 (突破失败=判断错误, 快速认错)          │")
        print(f"  │  4. 最长持仓35天, 横盘20天出场                       │")
        print(f"  └─────────────────────────────────────────────────────┘")

    # ============================================================
    # Final Summary
    # ============================================================
    print(f"\n{'='*70}")
    print(f"  📊 总结")
    print(f"{'='*70}")
    print(f"  🚀 池子A (右侧突破): {len(pool_a_results)} 只 — RS强+距高近+放量+均线多头")
    print(f"     → 正常仓位, 目标+10%~+20%, 止损-5%")
    print(f"  🔄 池子B (左侧修复): {len(pool_b_results)} 只 — 超跌反弹+5日涨+MA10拐头")
    print(f"     → 轻仓(1/3~1/2), 目标+8%~+15%, 止损-7%")
    print(f"  🧬 池子C (筹码峰): {len([c for c in pool_c_results if c['green_count']>=6])} 只可交易 + {len([c for c in pool_c_results if c['green_count']<6])} 只观察 (PF 2.17)")
    print(f"     → 正常仓位, +10%卖1/3→MA10追踪, 止损-7%")
    try:
        if breakout_confirmed:
            print(f"     🔫 今日突破确认: {len(breakout_confirmed)} 只 — 直接买入！")
    except NameError:
        pass
    noise = momentum_pass - len(pool_a_results) - len(pool_b_results)
    print(f"  🗑️  未分类噪音: {noise} 只 — 不符合任一池子条件, 已过滤")
    print(f"{'='*70}")

    # ---- Save results to txt ----
    sys.stdout = _orig_stdout  # restore stdout
    results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'results')
    os.makedirs(results_dir, exist_ok=True)
    txt_path = os.path.join(results_dir, f'us_screen_{today_str}.txt')
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(_output_buf.getvalue())
    _output_buf.close()
    print(f"\n  💾 结果已保存: {txt_path}")


if __name__ == '__main__':
    run_us_screen()
