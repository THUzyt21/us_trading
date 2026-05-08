# -*- coding: utf-8 -*-
import os

# US Stock VCP Screener Configuration
# ============================================================
# Momentum Breakout Config (趋势跟随/动量突破 — 找已经开始突破的票)
# ============================================================
MOMENTUM_CONFIG = {
    # --- New high detection ---
    'new_high_window':     20,    # 20-day new high (breakout confirmation)
    'near_high_pct':       0.03,  # within 3% of 52w high = "near new high"

    # --- Volume surge on breakout ---
    'min_vol_ratio':       1.3,   # today vol >= 1.3x of 20d avg (confirming demand)
    'strong_vol_ratio':    2.0,   # >= 2.0x = institutional buying

    # --- Momentum (recent price action) ---
    'min_3d_chg':          1.5,   # 3-day cumulative gain >= 1.5% (upward momentum)
    'min_5d_chg':          2.0,   # 5-day cumulative gain >= 2.0%
    'max_pullback_from_high': 0.08,  # current price within 8% of recent 20d high

    # --- Relative Strength (RS) ---
    'rs_lookback':         63,    # ~3 months for RS calculation
    'min_rs_rank':         70,    # RS percentile >= 70 (top 30% performers)

    # --- Moving average alignment (trend confirmation) ---
    'ma_fast':             10,    # MA10 (short-term momentum)
    'ma_mid':              21,    # MA21 (swing trend)
    'ma_slow':             50,    # MA50 (intermediate trend)

    # --- Breakout quality filters ---
    'min_base_days':       10,    # at least 10 days of consolidation before breakout
    'max_gap_up':          0.15,  # gap-up > 15% = too risky (earnings gap, avoid)
}

# ============================================================
VCP_CONFIG = {
    # --- Market cap filter (USD) ---
    'min_market_cap':  300_000_000,       # $300M (cover small-cap mining stocks)
    'max_market_cap':  200_000_000_000,   # $200B

    # --- Trend Template (Minervini) ---
    'min_days':        150,   # need at least 150 days of history
    'ma_short':        50,    # MA50
    'ma_mid':          150,   # MA150
    'ma_long':         200,   # MA200
    'min_above_low52': 0.25,  # price >= 52w low * 1.25 (at least 25% above low)
    'max_below_high52': 0.25, # price >= 52w high * 0.75 (within 25% of high)

    # --- VCP detection ---
    'vcp_lookback':    50,    # look back 50 days for contractions
    'min_contractions': 2,    # at least 2 contractions
    'contraction_ratio': 0.6, # each contraction should be < 60% of previous
    'max_last_range':  0.08,  # last contraction range < 8% (tight)

    # --- Volume dry-up ---
    'vol_dry_ratio':   0.7,   # recent 10d avg vol < 70% of 50d avg vol

    # --- Pivot breakout ---
    'pivot_window':    10,    # pivot = highest high in last 10 days
    'breakout_margin': 0.02,  # within 2% of pivot = "approaching"

    # --- Liquidity floor (kill penny stocks & zombie tickers) ---
    'min_avg_vol_50d':    500_000,      # 50d avg volume > 500K shares
    'min_avg_dollar_vol': 10_000_000,   # 50d avg dollar volume > $10M

    # --- Deep-V / wide-base rejection ---
    # NOTE: In volatile markets (post-crash recovery), most stocks have
    # 52w drawdowns > 35%. Relaxed to 65% to avoid killing everything.
    # The trend template (MA alignment) already filters weak stocks.
    'max_drawdown_52w':   0.65,  # max drawdown from 52w high < 65%
}

# ============================================================
# Pool A: Right-side Breakout (右侧突破池)
# True trend breakout — high conviction, normal position size
# Target: +10% to +20%, stop: -5% to -7%
# ============================================================
POOL_A_CONFIG = {
    'min_rs_rank':         70,    # RS >= 70% (top 30% performers)
    'max_pct_from_52h':    15.0,  # within 15% of 52-week high
    'min_vol_ratio':       1.5,   # volume ratio >= 1.5x (confirmed demand)
    'require_ma_aligned':  True,  # MA10 > MA21 > MA50 (full bull alignment)
    'min_score':           8,     # minimum total score to qualify
    'target_pct_1':        0.10,  # first target +10%
    'target_pct_2':        0.20,  # second target +20%
    'stop_pct':            0.05,  # stop loss -5% below entry
}

# ============================================================
# Pool B: Left-side Recovery (左侧修复池)
# Bottom-fishing recovery — low conviction, light position (1/3 to 1/2)
# Target: +8% to +15%, stop: -7% to -8% (tighter, run fast if wrong)
# ============================================================
POOL_B_CONFIG = {
    'min_pct_from_52h':    15.0,  # at least 15% from 52w high (not already at top)
    'max_pct_from_52h':    40.0,  # no more than 40% from 52w high (not a falling knife)
    'min_5d_chg':          3.0,   # 5-day gain >= 3% (recovery momentum)
    'min_vol_ratio':       0.8,   # volume ratio >= 0.8x (mild increase, no need for explosion)
    'max_vol_ratio':       2.5,   # volume ratio <= 2.5x (too much = event-driven, risky)
    'require_ma10_upturn': True,  # MA10 must be turning up (slope > 0)
    'min_score':           8,     # raised from 4 to cut noise; ~22 stocks
    'target_pct_1':        0.08,  # first target +8%
    'target_pct_2':        0.15,  # second target +15%
    'stop_pct':            0.07,  # stop loss -7% (tighter, run fast)
}
# ============================================================
# US Mining & Metals Custom Pool (always included)
# ============================================================
US_MINING_POOL = [
    # --- Uranium ---
    ('UUUU', 'Energy Fuels',       'Mining & Metals', 'Uranium'),
    ('CCJ',  'Cameco',             'Mining & Metals', 'Uranium'),
    ('UEC',  'Uranium Energy',     'Mining & Metals', 'Uranium'),
    ('NXE',  'NexGen Energy',      'Mining & Metals', 'Uranium'),
    ('DNN',  'Denison Mines',      'Mining & Metals', 'Uranium'),
    ('LEU',  'Centrus Energy',     'Mining & Metals', 'Uranium'),
    ('URA',  'Global X Uranium',   'Mining & Metals', 'Uranium ETF'),
    # --- Copper & Aluminum ---
    ('FCX',  'Freeport-McMoRan',   'Mining & Metals', 'Copper'),
    ('SCCO', 'Southern Copper',    'Mining & Metals', 'Copper'),
    ('TECK', 'Teck Resources',     'Mining & Metals', 'Copper/Zinc'),
    ('AA',   'Alcoa',             'Mining & Metals', 'Aluminum'),
    ('CENX', 'Century Aluminum',   'Mining & Metals', 'Aluminum'),
    ('HBM',  'Hudbay Minerals',    'Mining & Metals', 'Copper/Gold'),
    ('ARIS', 'Aris Mining',        'Mining & Metals', 'Copper/Gold'),
    # --- Gold & Precious Metals ---
    ('NEM',  'Newmont',            'Mining & Metals', 'Gold'),
    ('GOLD', 'Barrick Gold',       'Mining & Metals', 'Gold'),
    ('AEM',  'Agnico Eagle',       'Mining & Metals', 'Gold'),
    ('WPM',  'Wheaton Precious',   'Mining & Metals', 'Precious Metals Streaming'),
    ('PAAS', 'Pan American Silver','Mining & Metals', 'Silver'),
    ('HL',   'Hecla Mining',       'Mining & Metals', 'Silver/Gold'),
    ('AG',   'First Majestic',     'Mining & Metals', 'Silver'),
    ('CDE',  'Coeur Mining',       'Mining & Metals', 'Gold/Silver'),
    ('KGC',  'Kinross Gold',       'Mining & Metals', 'Gold'),
    ('EGO',  'Eldorado Gold',      'Mining & Metals', 'Gold'),
    ('BTG',  'B2Gold',             'Mining & Metals', 'Gold'),
    ('GFI',  'Gold Fields',        'Mining & Metals', 'Gold'),
    ('AU',   'AngloGold Ashanti',  'Mining & Metals', 'Gold'),
    ('RGLD', 'Royal Gold',         'Mining & Metals', 'Gold Royalty'),
    ('FNV',  'Franco-Nevada',      'Mining & Metals', 'Gold Royalty'),
    ('SSRM', 'SSR Mining',         'Mining & Metals', 'Gold/Silver'),
    # --- Rare Earths, Lithium & Minor Metals ---
    ('MP',   'MP Materials',       'Mining & Metals', 'Rare Earths'),
    ('ALB',  'Albemarle',          'Mining & Metals', 'Lithium'),
    ('SQM',  'SQM',               'Mining & Metals', 'Lithium/Iodine'),
    ('LAC',  'Lithium Americas',   'Mining & Metals', 'Lithium'),
    ('AMR',  'Alpha Metallurgical','Mining & Metals', 'Metallurgical Coal'),
    ('LTHM', 'Livent',            'Mining & Metals', 'Lithium'),
    # --- Steel & Iron ---
    ('NUE',  'Nucor',             'Mining & Metals', 'Steel'),
    ('STLD', 'Steel Dynamics',     'Mining & Metals', 'Steel'),
    ('CLF',  'Cleveland-Cliffs',   'Mining & Metals', 'Iron/Steel'),
    ('X',    'US Steel',           'Mining & Metals', 'Steel'),
    ('RS',   'Reliance Steel',     'Mining & Metals', 'Steel Distribution'),
    # --- Diversified Mining ---
    ('BHP',  'BHP Group',          'Mining & Metals', 'Diversified Mining'),
    ('RIO',  'Rio Tinto',          'Mining & Metals', 'Diversified Mining'),
    ('VALE', 'Vale',               'Mining & Metals', 'Iron/Nickel'),
    # --- Coal ---
    ('BTU',  'Peabody Energy',     'Mining & Metals', 'Coal'),
    ('ARCH', 'Arch Resources',     'Mining & Metals', 'Coal'),
    ('CEIX', 'CONSOL Energy',      'Mining & Metals', 'Coal'),
    ('HCC',  'Warrior Met Coal',   'Mining & Metals', 'Metallurgical Coal'),
]
