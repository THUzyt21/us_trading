# -*- coding: utf-8 -*-
"""
run_daily.py — A股每日流程入口
================================
完整流程（收盘后跑）:
    TUSHARE_TOKEN=xxx python3 cn/run_daily.py

只跑 quick_check（盘中随时看信号）:
    python3 cn/run_daily.py --skip-screener
"""

import argparse
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _TeeWriter:
    """Write to both stdout and a StringIO buffer."""
    def __init__(self, original):
        self._original = original
        self._buffer = io.StringIO()

    def write(self, s):
        self._original.write(s)
        self._buffer.write(s)

    def flush(self):
        self._original.flush()

    def getvalue(self):
        return self._buffer.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-screener', action='store_true',
                        help='Skip chip_screener_v3, only run quick_check')
    args = parser.parse_args()

    if not args.skip_screener:
        print()
        print('=' * 70)
        print('  [Step 1/2] A股筹码选股器 — chip_screener_v3')
        print('=' * 70)
        from chip_screener_v3 import run_chip_screen_v3
        run_chip_screen_v3()

    print()
    print('=' * 70)
    print('  [Step 2/2] 实时突破检测 — quick_check')
    print('=' * 70)

    # Capture quick_check output to file
    tee = _TeeWriter(sys.stdout)
    sys.stdout = tee
    try:
        from quick_check import main as qc_main
        qc_main()
    finally:
        sys.stdout = tee._original

    # Save quick_check output
    results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'results')
    os.makedirs(results_dir, exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d')
    qc_path = os.path.join(results_dir, f'quick_check_{today}.txt')
    with open(qc_path, 'w', encoding='utf-8') as f:
        f.write(tee.getvalue())
    print(f'  📄 quick_check 结果已保存: {qc_path}')

    print()
    print('✅  Done.')


if __name__ == '__main__':
    main()
