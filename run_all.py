# -*- coding: utf-8 -*-
"""
run_all.py — 一键执行 A股 + 美股，生成每日汇总日报
=======================================================
用法:
    TUSHARE_TOKEN=xxx python3 run_all.py          # 完整跑 A股+美股
    TUSHARE_TOKEN=xxx python3 run_all.py --cn-only # 只跑 A股
    python3 run_all.py --us-only                   # 只跑 美股
    python3 run_all.py --report-only               # 只生成日报 (两边结果已存在)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TRADING_DIR  = os.path.join(PROJECT_ROOT, 'trading')
CN_DIR       = os.path.join(TRADING_DIR, 'cn')
US_DIR       = os.path.join(TRADING_DIR, 'us')
REPORTS_DIR  = os.path.join(PROJECT_ROOT, 'reports')


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _banner(title: str):
    print()
    print('█' * 70)
    print(f'  {title}')
    print('█' * 70)


def _run_subprocess(script_path: str, cwd: str, extra_env: dict = None) -> bool:
    """Run script in isolated subprocess, return success."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, script_path],
        cwd=cwd,
        env=env,
    )
    return result.returncode == 0


def _load_json(path: str) -> list | dict | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


# ──────────────────────────────────────────────
# Step 1: Run CN  (subprocess isolation)
# ──────────────────────────────────────────────

def run_cn():
    _banner('🇨🇳  A股  —  chip_screener_v3  →  quick_check')
    ok = _run_subprocess(
        script_path=os.path.join(CN_DIR, 'run_daily.py'),
        cwd=CN_DIR,
    )
    if not ok:
        print('  ❌  A股流程异常退出，请检查上方日志')
    return ok


# ──────────────────────────────────────────────
# Step 2: Run US  (subprocess isolation)
# ──────────────────────────────────────────────

def run_us():
    _banner('🇺🇸  美股  —  run_daily')
    ok = _run_subprocess(
        script_path=os.path.join(PROJECT_ROOT, 'run_daily.py'),
        cwd=PROJECT_ROOT,
    )
    if not ok:
        print('  ❌  美股流程异常退出，请检查上方日志')
    return ok


# ──────────────────────────────────────────────
# Step 3: Generate clean AI-friendly daily report
# ──────────────────────────────────────────────

def _cn_signal_pool_section(lines: list):
    """A股信号池 (chip_signal_pool.json) — 等待突破确认的票。"""
    pool_file = os.path.join(CN_DIR, 'cache', 'chip_signal_pool.json')
    pool = _load_json(pool_file)
    if not pool:
        lines.append('_（今日无信号池数据）_')
        return

    lines.append(f'> 共 {len(pool)} 只等待突破确认（信号发出后 4 天内需出现 +1% 阳线 + 1.3x 量比）')
    lines.append('')
    lines.append('| # | 代码 | 名称 | 板块 | 信号价 | 评分 | 动态信号 | 剩余天 |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for i, s in enumerate(pool[:10], 1):
        lines.append(f'| {i} | {s["ts_code"]} | {s["name"]} | '
                     f'{s.get("sector", "")} | {s["signal_close"]} | '
                     f'{s.get("score", "")} | {s.get("dyn_signal", "")} | '
                     f'{s.get("days_left", "?")} |')


def _cn_strategy_pool_section(lines: list):
    """A股多策略池 (strategy_signal_pool.json) — 每池 Top5。"""
    pool_file = os.path.join(CN_DIR, 'cache', 'strategy_signal_pool.json')
    pool = _load_json(pool_file)
    if not pool:
        lines.append('_（今日无多策略池数据）_')
        return

    # Group by pool
    groups = {}
    for s in pool:
        groups.setdefault(s.get('pool', 'unknown'), []).append(s)

    pool_meta = {
        'breakout': ('🚀 突破池', '追涨买入 — 距52W高≤5% + 放量突破', '止损-5%, 目标+10%/+20%'),
        'trend':    ('🎯 趋势池', 'Minervini模板 — 价>MA50>MA150>MA200', '止损-7%, 目标+15%/+30%'),
        'pullback': ('🔄 回踩池', '强势股回踩MA20/50 — 低吸', '止损-5%, 目标+10%/+15%'),
    }

    for pool_name in ['breakout', 'trend', 'pullback']:
        items = groups.get(pool_name, [])
        if not items:
            continue
        meta = pool_meta.get(pool_name, (pool_name, '', ''))
        items.sort(key=lambda x: x.get('score', 0), reverse=True)
        top = items[:5]

        lines.append(f'#### {meta[0]}')
        lines.append(f'_{meta[1]} | {meta[2]}_')
        lines.append('')
        lines.append('| # | 代码 | 名称 | 板块 | 评分 | 等级 | RS(6M) | 价格 | 理由 |')
        lines.append('|---|---|---|---|---|---|---|---|---|')
        for i, s in enumerate(top, 1):
            reasons = '; '.join(s.get('reasons', [])[:2])
            lines.append(f'| {i} | {s["ts_code"]} | {s["name"]} | '
                         f'{s.get("sector", "")} | {s["score"]:.0f} | {s["grade"]} | '
                         f'{s.get("rs_6m", 0):+.1f}% | {s["signal_close"]} | {reasons} |')
        lines.append('')


def _cn_market_env_section(lines: list):
    """A股市场环境 — 从 txt 中提取，或直接写固定格式。"""
    # Try to extract from chip_v3 txt output
    import glob
    cn_results = os.path.join(TRADING_DIR, 'results')
    txt_files = glob.glob(os.path.join(cn_results, 'chip_v3_*.txt'))
    if not txt_files:
        lines.append('_（无市场环境数据）_')
        return
    latest = max(txt_files, key=os.path.getmtime)
    with open(latest, encoding='utf-8') as f:
        content = f.read()

    # Extract [市场环境] block
    marker = '[市场环境]'
    idx = content.find(marker)
    if idx == -1:
        lines.append('_（未找到市场环境数据）_')
        return
    # Take lines from marker until next separator
    env_lines = []
    for line in content[idx:].split('\n'):
        if line.startswith('---') and env_lines:
            break
        if line.strip():
            env_lines.append(line.strip())
    lines.append('```')
    for el in env_lines[:6]:
        lines.append(el)
    lines.append('```')


def _cn_quick_check_section(lines: list, today: str):
    """A股 quick_check 实时突破检测结果。"""
    import glob
    cn_results = os.path.join(TRADING_DIR, 'results')
    qc_files = glob.glob(os.path.join(cn_results, f'quick_check_{today}*.txt'))
    if not qc_files:
        lines.append('_（今日未跑 quick_check 或无结果文件）_')
        return
    latest = max(qc_files, key=os.path.getmtime)
    with open(latest, encoding='utf-8') as f:
        content = f.read().strip()
    if not content:
        lines.append('_（quick_check 输出为空）_')
        return
    # Only keep the summary parts (skip verbose per-stock lines if too long)
    if len(content) > 3000:
        # Extract key sections: Summary lines + actionable footer
        kept = []
        for line in content.split('\n'):
            if any(kw in line for kw in ['Summary:', '🟢', '🟡', '🔶', '💤', '⚠️',
                                          '出手', '试探', '策略池', '═', '─',
                                          'Top', '达标', '推荐', '买入']):
                kept.append(line)
        lines.append('```')
        lines.append('\n'.join(kept[-40:]))  # Last 40 relevant lines
        lines.append('```')
    else:
        lines.append('```')
        lines.append(content)
        lines.append('```')


def generate_report(today: str):
    _banner('📝  生成每日汇总日报')

    os.makedirs(REPORTS_DIR, exist_ok=True)

    lines = []
    lines.append(f'# 📊 每日交易日报 — {today}')
    lines.append('')
    lines.append('> 自动生成 · AI 解读专用 · 精简决策信息')
    lines.append('')

    # ═══════════════════════════════════════════
    # A股 Section
    # ═══════════════════════════════════════════
    lines.append('---')
    lines.append('')
    lines.append('## 🇨🇳 A股')
    lines.append('')

    # Market environment
    lines.append('### 📡 市场环境')
    lines.append('')
    _cn_market_env_section(lines)
    lines.append('')

    # Signal pool (chip_signal_pool)
    lines.append('### 👀 信号池 — 等待突破确认')
    lines.append('')
    _cn_signal_pool_section(lines)
    lines.append('')

    # Multi-strategy pool (Top5 per pool)
    lines.append('### 🚀🎯🔄 多策略池 — 每池 Top 5')
    lines.append('')
    _cn_strategy_pool_section(lines)

    # Quick check realtime
    lines.append('### ⚡ quick_check 实时突破检测')
    lines.append('')
    _cn_quick_check_section(lines, today)
    lines.append('')

    # ═══════════════════════════════════════════
    # US Section — directly inline the structured report
    # ═══════════════════════════════════════════
    lines.append('---')
    lines.append('')
    lines.append('## 🇺🇸 美股')
    lines.append('')

    us_md_path = os.path.join(REPORTS_DIR, f'{today}.md')
    if os.path.exists(us_md_path):
        with open(us_md_path, encoding='utf-8') as f:
            us_content = f.read().strip()
        # Remove the top-level H1 title (avoid double heading)
        us_lines = us_content.split('\n')
        # Skip lines until after the first --- separator or first ## heading
        start = 0
        for i, line in enumerate(us_lines):
            if line.startswith('## '):
                start = i
                break
        lines.extend(us_lines[start:])
    else:
        lines.append('_（今日无美股鹰眼报告）_')
    lines.append('')

    # ═══════════════════════════════════════════
    # Footer
    # ═══════════════════════════════════════════
    lines.append('---')
    lines.append('')
    lines.append(f'_生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}_')
    lines.append('')

    md_path = os.path.join(REPORTS_DIR, f'daily_report_{today}.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f'  ✅ 日报已生成: {md_path}')
    return md_path


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='一键跑 A股+美股，生成每日日报')
    parser.add_argument('--cn-only',     action='store_true', help='只跑 A股')
    parser.add_argument('--us-only',     action='store_true', help='只跑 美股')
    parser.add_argument('--report-only', action='store_true', help='只生成日报')
    args = parser.parse_args()

    today = datetime.now().strftime('%Y-%m-%d')

    if args.report_only:
        generate_report(today)
        return

    if args.us_only:
        run_us()
        generate_report(today)
        return

    if args.cn_only:
        run_cn()
        generate_report(today)
        return

    # Default: run both
    run_cn()
    run_us()
    generate_report(today)

    print()
    print('🎉  全部完成！')
    print(f'📁  日报: reports/daily_report_{today}.md')


if __name__ == '__main__':
    main()
