#!/usr/bin/env python3
"""
run_daily.py - 美股鹰眼系统每日主入口
用法：
    python3 run_daily.py                      # 默认：全市场扫描（科技+资源），支持断点续传
    python3 run_daily.py --mode watchlist     # 只跑 config/watchlist.yaml 里的手选清单
    python3 run_daily.py --mode scan          # 全市场扫描（科技+资源股，默认）
    python3 run_daily.py --scope tech         # 只扫科技股 (tech / resource / both)
    python3 run_daily.py --top 50             # 最终报告只保留 Top 50
    python3 run_daily.py --workers 16         # 并发度
    python3 run_daily.py --force              # 强制刷新所有数据缓存
    python3 run_daily.py --fresh              # 清空断点续传记录，从头开始
    python3 run_daily.py --date 2026-05-06    # 指定报告日期
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.fetcher import (  # noqa: E402
    fetch_batch, fetch_batch_parallel, fetch_info_light_batch,
    RateLimitCircuitBreaker,
)
from src.signals import compute_scorecard, macro_snapshot  # noqa: E402
from src.reporter import generate_report  # noqa: E402
from src.universe import (  # noqa: E402
    load_full_universe,
    filter_by_sectors,
    get_tech_resource_universe,
)
from src.checkpoint import Checkpoint  # noqa: E402


def setup_logging():
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config():
    with open(PROJECT_ROOT / "config" / "watchlist.yaml", "r", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    with open(PROJECT_ROOT / "config" / "params.yaml", "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)
    return wl, params


def resolve_watchlist(
    mode: str,
    scope: str,
    wl_cfg: dict,
    params: dict,
    workers: int,
    log: logging.Logger,
    checkpoint: Checkpoint = None,
    circuit_breaker: RateLimitCircuitBreaker = None,
) -> list[str]:
    """
    根据 mode 决定候选池：
      - watchlist: 用 yaml 里的手选清单
      - scan:     全市场扫描，按 scope (tech/resource/both) 过滤 sector/industry
    返回最终的候选 symbol 列表（已去重、已剔除持仓）。
    """
    if mode == "watchlist":
        return list(wl_cfg.get("watchlist", []))

    # ========== 全市场扫描 ==========
    include_tech = scope in ("tech", "both")
    include_resource = scope in ("resource", "both")

    log.info(f"[scan] 加载全美股上市清单 (scope={scope}) ...")
    full = load_full_universe(max_hours=24.0)
    all_syms = full["symbol"].tolist()
    log.info(f"[scan] 全市场共 {len(all_syms)} 只普通股，开始轻量 info 拉取预筛...")

    # Stage 1: 并发拉轻量 info（只要 sector/industry/marketCap/avgVolume）
    # 注意：info-light 并发不能高，Yahoo 对 /quoteSummary 端点限流严
    light_workers = min(workers, 6)
    info_map = fetch_info_light_batch(
        all_syms,
        max_workers=light_workers,
        cache_hours=168.0,  # 7天，sector/marketCap 变化极慢
        checkpoint=checkpoint,
        circuit_breaker=circuit_breaker,
        batch_flush=100,
    )

    # Stage 2: 按行业 + 市值 + 成交量过滤
    sectors, industries = get_tech_resource_universe(
        include_tech=include_tech, include_resource=include_resource
    )
    min_mc = params.get("exclude_rules", {}).get("min_market_cap_usd", 2e9)
    min_vol = params.get("exclude_rules", {}).get("min_avg_volume", 5e5)

    filtered = filter_by_sectors(
        all_syms, info_map,
        sectors=sectors, industries=industries,
        min_market_cap=min_mc, min_avg_volume=min_vol,
    )

    # 输出当前 checkpoint 统计，便于用户判断"建库进度"
    if checkpoint:
        st = checkpoint.stats("info_light")
        total_cov = st["done"] + st["empty"]
        coverage = 100.0 * total_cov / max(len(all_syms), 1)
        log.info(f"[scan] 📊 universe 建库进度: {total_cov}/{len(all_syms)} "
                 f"({coverage:.1f}%) done={st['done']}, empty={st['empty']}, "
                 f"failed={st['failed']}")

    log.info(f"[scan] 按行业/市值/成交量过滤后: {len(filtered)} 只 "
             f"(min_cap=${min_mc/1e9:.1f}B, min_vol={min_vol/1e4:.0f}万股)")

    if circuit_breaker and circuit_breaker.tripped:
        log.warning("[scan] ⚠️ 本次 info-light 阶段被 Yahoo 限流熔断，"
                    "已保存进度。等 15-30 分钟后重跑即可从断点继续建库。")

    return filtered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["scan", "watchlist"], default="scan",
                        help="scan=全市场扫描（默认） / watchlist=手选清单")
    parser.add_argument("--scope", choices=["tech", "resource", "both"], default="both",
                        help="扫描范围：tech=科技 / resource=资源 / both=两者（默认）")
    parser.add_argument("--top", type=int, default=80,
                        help="报告中候选池保留的最大条数（按总分，默认80）")
    parser.add_argument("--pool-top", type=int, default=3,
                        help="每个策略池（突破/趋势/回踩/反转）展示 Top N（默认3）")
    parser.add_argument("--workers", type=int, default=6,
                        help="并发度（默认6，推荐4-8，太高易被限流）")
    parser.add_argument("--force", action="store_true", help="强制刷新所有数据缓存")
    parser.add_argument("--fresh", action="store_true",
                        help="重置 checkpoint（断点续传记录），从头开始扫描")
    parser.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD")
    args = parser.parse_args()

    setup_logging()
    log = logging.getLogger("run_daily")

    wl, params = load_config()
    macro_syms = wl.get("macro", [])
    holdings = wl.get("holdings", [])

    cache_hours = 0 if args.force else params["data"].get("cache_hours", 4)
    history_days = params["data"].get("history_days", 400)

    log.info("=" * 60)
    log.info(f"美股鹰眼系统启动 | mode={args.mode} scope={args.scope} "
             f"workers={args.workers} top={args.top}")
    log.info("=" * 60)

    # 初始化 checkpoint + 熔断器（仅 scan 模式使用）
    checkpoint = None
    circuit_breaker = None
    if args.mode == "scan":
        task_name = f"universe_scan_{args.scope}"
        checkpoint = Checkpoint(task_name, max_age_hours=168.0)  # 7天内断点续传有效
        if args.fresh:
            log.info(f"[checkpoint] --fresh: 重置 {task_name}")
            checkpoint.reset()
        # 熔断器：窗口 30s 内连续 8 次限流 → 熔断
        circuit_breaker = RateLimitCircuitBreaker(threshold=8, window_sec=30)

    # Step 0. 解析候选池
    watchlist = resolve_watchlist(
        args.mode, args.scope, wl, params, args.workers, log,
        checkpoint=checkpoint, circuit_breaker=circuit_breaker,
    )
    # 剔除已在持仓里的，避免重复
    watchlist = [s for s in watchlist if s not in set(holdings)]
    log.info(f"大盘 {len(macro_syms)} + 持仓 {len(holdings)} + 候选 {len(watchlist)}")

    # 如果 info-light 阶段已熔断，候选池可能不完整，但我们仍尽力完成剩余流程
    # （大盘+持仓会正常拉到；候选池走缓存的部分也能出报告）

    # Step 1. 批量拉数据（大盘 + 持仓 + 候选），并发 + bulk
    all_syms = list(dict.fromkeys(macro_syms + holdings + watchlist))
    log.info(f"开始批量拉取 {len(all_syms)} 个标的历史数据...")
    data = fetch_batch_parallel(
        all_syms,
        days=history_days,
        cache_hours=cache_hours,
        max_workers=args.workers,
        with_earnings=True,
        checkpoint=checkpoint,
        circuit_breaker=circuit_breaker,
    )

    # Step 2. 大盘风向标
    macro_data = {s: data[s] for s in macro_syms if s in data}
    macro = macro_snapshot(macro_data)
    log.info(f"大盘快照完成: {len(macro)} 个指标")

    # Step 3. SPY 历史（给 RS 计算用）
    spy_hist = None
    if "SPY" in data and data["SPY"].get("history") is not None:
        spy_hist = data["SPY"]["history"]

    # Step 4. 持仓打分
    holdings_cards = []
    for sym in holdings:
        b = data.get(sym, {})
        if b.get("history") is None:
            log.warning(f"持仓 {sym} 无数据，跳过")
            continue
        sc = compute_scorecard(sym, b["history"], b.get("info", {}), spy_hist, b.get("earnings"))
        holdings_cards.append(sc)
        log.info(f"持仓 {sym}: {sc.total_score:.1f} ({sc.grade}) - {sc.verdict}")

    # Step 5. 候选池打分
    watchlist_cards = []
    for sym in watchlist:
        b = data.get(sym, {})
        if b.get("history") is None:
            continue
        sc = compute_scorecard(sym, b["history"], b.get("info", {}), spy_hist, b.get("earnings"))
        watchlist_cards.append(sc)

    a_count = sum(1 for c in watchlist_cards if c.grade == "A")
    b_count = sum(1 for c in watchlist_cards if c.grade == "B")
    log.info(f"候选池打分完成: A级={a_count} / B级={b_count} / 总{len(watchlist_cards)}")

    # Step 6. Top N 截断（避免 markdown 过大；A/B 级永远保留）
    top_n = max(args.top, a_count + b_count)
    sorted_all = sorted(watchlist_cards, key=lambda x: x.total_score, reverse=True)
    watchlist_cards_report = sorted_all[:top_n]
    if len(sorted_all) > top_n:
        log.info(f"报告仅保留 Top {top_n}（共打分 {len(sorted_all)} 只）")

    # Step 7. 生成报告
    report_date = args.date or datetime.now().strftime("%Y-%m-%d")
    md_path = generate_report(
        macro, holdings_cards, watchlist_cards_report, report_date,
        pool_top=args.pool_top,
    )
    log.info(f"✓ 报告已生成: {md_path}")
    log.info(f"✓ JSON 数据: {md_path.with_suffix('.json')}")

    # Step 8. 给 stdout 一个简短摘要
    print("\n" + "=" * 60)
    print(f"📊 美股鹰眼速报 {report_date}  [mode={args.mode} scope={args.scope}]")
    print("=" * 60)
    spy = macro.get("SPY", {})
    qqq = macro.get("QQQ", {})
    vix = macro.get("VIX", {})
    print(f"SPY: {spy.get('close','—')} ({spy.get('chg_1d','—')}%)  "
          f"QQQ: {qqq.get('close','—')} ({qqq.get('chg_1d','—')}%)  "
          f"VIX: {vix.get('close','—')} ({vix.get('chg_1d','—')}%)")
    print()
    print("持仓：")
    for c in holdings_cards:
        print(f"  {c.symbol:6s} {c.total_score:5.1f}分 ({c.grade}) {c.verdict}  "
              f"现价 {c.price:.2f} ({c.change_pct:+.2f}%)")
    print()
    print(f"候选池（共扫描 {len(watchlist_cards)} 只）  A级狙击: {a_count} 只 | B级观察: {b_count} 只")
    top_show = sorted_all[:10]
    if top_show:
        print("Top 10:")
        for c in top_show:
            print(f"  {c.symbol:6s} {c.total_score:5.1f}分 ({c.grade}) {c.verdict}")

    # 熔断提醒
    if circuit_breaker and circuit_breaker.tripped:
        print()
        print("⚠️  本次扫描被 Yahoo 限流熔断，universe 建库尚未完成。")
        print("   已保存断点，请等 15-30 分钟后重跑 `python3 run_daily.py` 继续建库。")

    if checkpoint:
        st_info = checkpoint.stats("info_light")
        st_hist = checkpoint.stats("history")
        print()
        print(f"断点进度: universe.info={st_info['done']+st_info['empty']} 完成, "
              f"history={st_hist['done']} 已缓存")

    print("=" * 60)
    print(f"完整报告: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
