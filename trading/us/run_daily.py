# -*- coding: utf-8 -*-
"""
run_daily.py — 美股每日流程入口
================================
执行:
    python3 us/run_daily.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from us_stock_screener import run_us_screen

if __name__ == '__main__':
    run_us_screen()
