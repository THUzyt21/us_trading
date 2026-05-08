"""
fetcher.py - 美股数据采集模块
功能：用 yfinance 拉取行情、基本面、财报日期；本地 parquet 缓存减少重复调用
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 特殊 ticker 映射（yfinance 要用的符号和用户习惯不一样）
TICKER_ALIAS = {
    "VIX": "^VIX",
    "DXY": "DX-Y.NYB",
    "SPX": "^GSPC",
    "NDX": "^NDX",
}

# ================ 反限流基础设施 ================
# curl_cffi 伪装成真实浏览器（Chrome TLS/JA3 指纹），Yahoo 对这个更宽容
_SESSION = None
_SESSION_LOCK = threading.Lock()


def _get_session():
    """线程安全地获取一个共享的 curl_cffi Session。"""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                try:
                    from curl_cffi import requests as cffi_requests
                    _SESSION = cffi_requests.Session(impersonate="chrome")
                    logger.debug("[session] curl_cffi session initialized (chrome impersonate)")
                except Exception as e:
                    logger.warning(f"[session] fallback to default: {e}")
                    _SESSION = False  # 标记已尝试
    return _SESSION if _SESSION else None


def _retry_on_rate_limit(func, max_retries: int = 3, base_delay: float = 4.0):
    """
    调用 func()，遇到限流自动退避重试。
    429 / Too Many Requests / Rate limited 识别。
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            msg = str(e).lower()
            is_rl = ("too many requests" in msg or "rate limit" in msg
                     or "429" in msg or "throttled" in msg or "empty bulk" in msg)
            last_exc = e
            if not is_rl or attempt == max_retries:
                raise
            # 指数退避 + 随机抖动
            delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
            time.sleep(delay)
    if last_exc:
        raise last_exc


class RateLimitCircuitBreaker:
    """
    限流熔断器：连续 N 次失败视为 Yahoo IP 限流，触发熔断，暂停所有请求。
    熔断后，调用方应保存 checkpoint 并结束当次任务，等冷却期过再重启。
    """

    def __init__(self, threshold: int = 8, window_sec: int = 30):
        self.threshold = threshold  # 窗口内连续失败多少次触发熔断
        self.window_sec = window_sec
        self._fails: list[float] = []
        self._tripped = False
        self._lock = threading.Lock()

    def record_success(self):
        with self._lock:
            self._fails.clear()

    def record_failure(self, is_rate_limit: bool = True):
        if not is_rate_limit:
            return
        with self._lock:
            now = time.time()
            self._fails.append(now)
            # 只保留窗口内的
            self._fails = [t for t in self._fails if now - t < self.window_sec]
            if len(self._fails) >= self.threshold:
                self._tripped = True

    @property
    def tripped(self) -> bool:
        return self._tripped

    def reset(self):
        with self._lock:
            self._fails.clear()
            self._tripped = False


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e).lower()
    return ("too many requests" in msg or "rate limit" in msg
            or "429" in msg or "throttled" in msg or "empty bulk" in msg)


def _resolve_ticker(symbol: str) -> str:
    return TICKER_ALIAS.get(symbol.upper(), symbol.upper())


def _cache_path(symbol: str, kind: str) -> Path:
    safe = symbol.replace("^", "_").replace("=", "_").replace("/", "_")
    return CACHE_DIR / f"{safe}__{kind}.parquet"


def _is_fresh(path: Path, max_hours: float) -> bool:
    if not path.exists():
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    return age_hours < max_hours


def fetch_history(
    symbol: str,
    days: int = 400,
    cache_hours: float = 4.0,
    force: bool = False,
) -> Optional[pd.DataFrame]:
    """
    拉取日线历史。返回包含 Open/High/Low/Close/Volume 的 DataFrame。
    默认缓存 4 小时，避免重复请求。
    """
    ticker = _resolve_ticker(symbol)
    cache_file = _cache_path(symbol, "history")

    if not force and _is_fresh(cache_file, cache_hours):
        try:
            df = pd.read_parquet(cache_file)
            logger.debug(f"[cache] {symbol} history loaded from cache ({len(df)} rows)")
            return df
        except Exception as e:
            logger.warning(f"[cache-read-failed] {symbol}: {e}")

    def _do():
        end = datetime.now()
        start = end - timedelta(days=int(days * 1.5))
        sess = _get_session()
        tk = yf.Ticker(ticker, session=sess) if sess else yf.Ticker(ticker)
        return tk.history(start=start, end=end, auto_adjust=False, actions=False)

    try:
        df = _retry_on_rate_limit(_do, max_retries=3, base_delay=4.0)
        if df is None or df.empty:
            logger.debug(f"[empty] {symbol} returned no data")
            return None
        df = df.tail(days).copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.to_parquet(cache_file)
        logger.debug(f"[fetched] {symbol} history: {len(df)} rows")
        return df
    except Exception as e:
        logger.debug(f"[fetch-failed] {symbol}: {e}")
        return None


def fetch_info(symbol: str, cache_hours: float = 24.0, force: bool = False) -> dict:
    """
    拉取基本面信息：市值、PE、Forward PE、行业、分红率等。
    info 数据变化慢，缓存 24h。
    """
    ticker = _resolve_ticker(symbol)
    cache_file = _cache_path(symbol, "info")

    if not force and _is_fresh(cache_file, cache_hours):
        try:
            df = pd.read_parquet(cache_file)
            return df.iloc[0].to_dict()
        except Exception:
            pass

    def _do():
        sess = _get_session()
        tk = yf.Ticker(ticker, session=sess) if sess else yf.Ticker(ticker)
        return tk.info or {}

    try:
        info = _retry_on_rate_limit(_do, max_retries=3, base_delay=4.0)
        if not info:
            return {}
        # 只保留我们关心的字段，防止缓存太大
        keep = [
            "symbol", "shortName", "longName", "sector", "industry",
            "marketCap", "trailingPE", "forwardPE", "pegRatio",
            "dividendYield", "beta",
            "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
            "averageVolume", "averageVolume10days",
            "revenueGrowth", "earningsGrowth",
            "profitMargins", "operatingMargins",
            "recommendationMean", "targetMeanPrice",
        ]
        filtered = {k: info.get(k) for k in keep}
        filtered["symbol"] = symbol
        pd.DataFrame([filtered]).to_parquet(cache_file)
        return filtered
    except Exception as e:
        logger.error(f"[info-failed] {symbol}: {e}")
        return {}


def fetch_earnings_date(symbol: str, cache_hours: float = 12.0) -> Optional[datetime]:
    """
    获取下次财报日（用于财报前2日回避）。取不到返回 None。
    """
    ticker = _resolve_ticker(symbol)
    cache_file = _cache_path(symbol, "earnings")
    if _is_fresh(cache_file, cache_hours):
        try:
            df = pd.read_parquet(cache_file)
            if df.empty:
                return None
            val = df.iloc[0]["earnings_date"]
            return pd.to_datetime(val).to_pydatetime() if pd.notna(val) else None
        except Exception:
            pass

    def _do():
        sess = _get_session()
        tk = yf.Ticker(ticker, session=sess) if sess else yf.Ticker(ticker)
        return tk.calendar

    try:
        cal = _retry_on_rate_limit(_do, max_retries=2, base_delay=4.0)
        earnings_date = None
        if isinstance(cal, dict):
            val = cal.get("Earnings Date")
            if isinstance(val, (list, tuple)) and val:
                earnings_date = pd.to_datetime(val[0])
            elif val is not None:
                earnings_date = pd.to_datetime(val)
        elif isinstance(cal, pd.DataFrame) and not cal.empty:
            if "Earnings Date" in cal.index:
                earnings_date = pd.to_datetime(cal.loc["Earnings Date"].iloc[0])

        pd.DataFrame([{"earnings_date": earnings_date}]).to_parquet(cache_file)
        return earnings_date.to_pydatetime() if earnings_date is not None else None
    except Exception as e:
        logger.debug(f"[earnings-miss] {symbol}: {e}")
        pd.DataFrame([{"earnings_date": None}]).to_parquet(cache_file)
        return None


def fetch_batch(symbols: list[str], days: int = 400, cache_hours: float = 4.0) -> dict:
    """
    批量拉取一组标的的历史 + info。返回 {symbol: {"history": df, "info": dict, "earnings": dt_or_none}}
    """
    results = {}
    for i, sym in enumerate(symbols, 1):
        logger.info(f"[{i}/{len(symbols)}] fetching {sym}...")
        hist = fetch_history(sym, days=days, cache_hours=cache_hours)
        info = fetch_info(sym)
        earn = fetch_earnings_date(sym)
        results[sym] = {"history": hist, "info": info, "earnings": earn}
        # 轻微节流，避免被 yfinance 限流
        time.sleep(0.15)
    return results


# ============================================================
#         并发批量拉取（大规模全市场扫描用）
# ============================================================

def fetch_info_light(
    symbol: str,
    cache_hours: float = 168.0,  # 7天，sector/marketCap 变化极慢
    force: bool = False,
) -> dict:
    """
    轻量 info 拉取：只关心 sector/industry/marketCap/averageVolume，
    用于全市场预筛阶段。缓存 48h（info 变化慢）。
    """
    cache_file = _cache_path(symbol, "info_light")
    if not force and _is_fresh(cache_file, cache_hours):
        try:
            df = pd.read_parquet(cache_file)
            return df.iloc[0].to_dict()
        except Exception:
            pass

    ticker = _resolve_ticker(symbol)

    def _do():
        sess = _get_session()
        tk = yf.Ticker(ticker, session=sess) if sess else yf.Ticker(ticker)
        return tk.info or {}

    try:
        info = _retry_on_rate_limit(_do, max_retries=3, base_delay=4.0)
        keep = ["sector", "industry", "marketCap",
                "averageVolume", "averageVolume10days",
                "shortName", "longName", "quoteType"]
        filtered = {k: info.get(k) for k in keep}
        filtered["symbol"] = symbol
        # 只有拿到非空 sector 或 marketCap 才写缓存；空数据不缓存保留下次重试机会
        if info.get("sector") or info.get("marketCap") or info.get("quoteType"):
            pd.DataFrame([filtered]).to_parquet(cache_file)
        return filtered
    except Exception as e:
        logger.debug(f"[info-light-failed] {symbol}: {e}")
        return {}


def _parallel_map(
    func: Callable,
    items: list,
    max_workers: int = 16,
    desc: str = "task",
    log_every: int = 100,
) -> dict:
    """
    通用并发执行器。func(item) -> result。
    返回 {item: result}。对失败项 result=None。
    """
    results = {}
    done = 0
    total = len(items)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut2item = {ex.submit(func, it): it for it in items}
        for fut in as_completed(fut2item):
            item = fut2item[fut]
            try:
                results[item] = fut.result()
            except Exception as e:
                logger.debug(f"[{desc}-err] {item}: {e}")
                results[item] = None
            done += 1
            if done % log_every == 0 or done == total:
                logger.info(f"[{desc}] progress {done}/{total}")
    return results


def fetch_info_light_batch(
    symbols: list[str],
    max_workers: int = 16,
    cache_hours: float = 168.0,  # 7天
    checkpoint=None,  # Optional[Checkpoint]
    circuit_breaker=None,  # Optional[RateLimitCircuitBreaker]
    batch_flush: int = 100,
) -> dict:
    """
    并发拉取轻量 info，用于 universe 预筛。

    集成断点续传：
      - 若传入 checkpoint，每处理 batch_flush 只就 flush 一次
      - 若传入 circuit_breaker，连续失败过多时立即退出（保留进度）

    返回 {symbol: info_dict}。被跳过（已完成/冷却中/熔断后未执行）的不在返回字典里。
    """
    # 先从缓存读已有的
    results: dict = {}
    cached_syms = []
    todo = []
    for sym in symbols:
        # 如果有 checkpoint 记录为已完成，直接从缓存读
        if checkpoint and checkpoint.is_done("info_light", sym):
            cache_file = _cache_path(sym, "info_light")
            if _is_fresh(cache_file, cache_hours):
                try:
                    df = pd.read_parquet(cache_file)
                    results[sym] = df.iloc[0].to_dict()
                    cached_syms.append(sym)
                    continue
                except Exception:
                    pass
            else:
                # checkpoint 说完成但缓存失效了 → 要重拉
                pass
        # 冷却中跳过
        if checkpoint and checkpoint.is_in_cooldown("info_light", sym):
            continue
        todo.append(sym)

    logger.info(f"[info-light] 缓存/checkpoint 命中 {len(cached_syms)}, "
                f"本次需拉取 {len(todo)}, 跳过（冷却）{len(symbols) - len(cached_syms) - len(todo)} "
                f"(workers={max_workers})")

    if not todo:
        return results

    t0 = time.time()
    done_count = [0]  # closure-mutable counter
    lock = threading.Lock()

    def _one(sym):
        # 熔断后，新任务立即跳过
        if circuit_breaker and circuit_breaker.tripped:
            return ("skip", None)
        try:
            info = fetch_info_light(sym, cache_hours=cache_hours)
            if info and (info.get("sector") or info.get("marketCap") or info.get("quoteType")):
                if circuit_breaker:
                    circuit_breaker.record_success()
                return ("ok", info)
            else:
                # 空结果：可能是限流导致的空，也可能是真的无数据
                # 保守：视为失败（进冷却），下次重试
                return ("empty_maybe_rl", info or {})
        except Exception as e:
            if _is_rate_limit_error(e):
                if circuit_breaker:
                    circuit_breaker.record_failure(is_rate_limit=True)
                return ("rate_limit", None)
            return ("fail", None)

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut2sym = {ex.submit(_one, s): s for s in todo}
            for fut in as_completed(fut2sym):
                sym = fut2sym[fut]
                try:
                    status, payload = fut.result()
                except Exception as e:
                    status, payload = "fail", None

                if status == "ok":
                    results[sym] = payload
                    if checkpoint:
                        checkpoint.mark_done("info_light", sym)
                elif status == "empty_maybe_rl":
                    # 暂不标记完成，下次重试（但不进冷却，避免被误杀）
                    # 标记 failed 让它进冷却，避免本次反复空跑
                    if checkpoint:
                        checkpoint.mark_failed("info_light", sym, "empty_response")
                elif status == "rate_limit":
                    if checkpoint:
                        checkpoint.mark_failed("info_light", sym, "rate_limit")
                elif status == "skip":
                    pass  # 熔断后跳过
                else:
                    if checkpoint:
                        checkpoint.mark_failed("info_light", sym, "other")

                with lock:
                    done_count[0] += 1
                    n = done_count[0]

                # 分批 flush
                if checkpoint and n % batch_flush == 0:
                    checkpoint.flush()
                if n % 200 == 0 or n == len(todo):
                    logger.info(f"[info-light] progress {n}/{len(todo)} "
                                f"(ok={len(results)-len(cached_syms)})")

                # 熔断：取消剩余任务
                if circuit_breaker and circuit_breaker.tripped:
                    logger.warning(f"[info-light] ⚠️ 熔断触发（连续限流），"
                                   f"已完成 {n}/{len(todo)}, 保存进度后退出")
                    for f in fut2sym:
                        if not f.done():
                            f.cancel()
                    break
    finally:
        if checkpoint:
            checkpoint.flush(force=True)

    elapsed = time.time() - t0
    logger.info(f"[info-light] 本次拉取 {len(results)-len(cached_syms)}/{len(todo)} "
                f"成功, 耗时 {elapsed:.1f}s, 累计有效 {len(results)}")
    return results


def _download_history_bulk(
    symbols: list[str],
    days: int,
    cache_hours: float,
    chunk_size: int = 80,
    checkpoint=None,
    circuit_breaker=None,
) -> dict:
    """
    用 yf.download 分批批量拉历史（每批 chunk_size 只）。
    比单票 Ticker.history 快 10-50 倍，且更少触发限流。

    支持：
      - checkpoint：完成的 chunk 会记录进 history stage
      - circuit_breaker：连续限流时提前退出，保留进度

    返回 {symbol: df_or_none}。已写入本地 parquet 缓存。
    """
    results: dict[str, Optional[pd.DataFrame]] = {}
    # 先命中缓存
    to_fetch = []
    for sym in symbols:
        cache_file = _cache_path(sym, "history")
        if _is_fresh(cache_file, cache_hours):
            try:
                df = pd.read_parquet(cache_file)
                results[sym] = df
                continue
            except Exception:
                pass
        # checkpoint 说最近成功过但缓存失效了，允许重拉
        if checkpoint and checkpoint.is_in_cooldown("history", sym):
            continue
        to_fetch.append(sym)

    if not to_fetch:
        return results

    logger.info(f"[bulk-hist] 缓存命中 {len(results)}/{len(symbols)}, "
                f"需要下载 {len(to_fetch)} 只...")

    end = datetime.now()
    start = end - timedelta(days=int(days * 1.5))

    # 分批，每批 chunk_size 只
    chunks = [to_fetch[i:i + chunk_size] for i in range(0, len(to_fetch), chunk_size)]
    for idx, chunk in enumerate(chunks, 1):
        # 熔断后剩余 chunk 全部跳过
        if circuit_breaker and circuit_breaker.tripped:
            logger.warning(f"[bulk-hist] 熔断后跳过剩余 {len(chunks) - idx + 1} 个 chunks")
            for s in chunk:
                results[s] = None
            continue

        # 特殊 ticker 映射
        resolved = [_resolve_ticker(s) for s in chunk]
        rev_map = dict(zip(resolved, chunk))  # ^VIX -> VIX

        def _do():
            sess = _get_session()
            result = yf.download(
                tickers=" ".join(resolved),
                start=start, end=end,
                auto_adjust=False, actions=False,
                group_by="ticker", threads=True, progress=False,
                session=sess,
            )
            # yfinance 在 429 时吞掉异常返回空 DataFrame，我们主动检测
            if result is None or result.empty:
                raise RuntimeError("Too Many Requests - empty bulk response")
            return result

        try:
            big = _retry_on_rate_limit(_do, max_retries=4, base_delay=8.0)
            if circuit_breaker:
                circuit_breaker.record_success()
        except Exception as e:
            logger.warning(f"[bulk-hist] chunk {idx}/{len(chunks)} failed: {e}")
            if circuit_breaker and _is_rate_limit_error(e):
                circuit_breaker.record_failure(is_rate_limit=True)
            for s in chunk:
                results[s] = None
                if checkpoint:
                    checkpoint.mark_failed("history", s, "chunk_fail")
            if checkpoint:
                checkpoint.flush()
            continue

        # yf.download 多只返回 MultiIndex 列 (ticker, field)，单只返回普通列
        for resolved_t, orig_sym in rev_map.items():
            try:
                if isinstance(big.columns, pd.MultiIndex):
                    if resolved_t in big.columns.get_level_values(0):
                        sub = big[resolved_t].dropna(how="all")
                    else:
                        sub = None
                else:
                    sub = big.dropna(how="all")
                if sub is None or sub.empty:
                    results[orig_sym] = None
                    if checkpoint:
                        checkpoint.mark_empty("history", orig_sym)
                    continue
                sub = sub.tail(days).copy()
                sub.index = pd.to_datetime(sub.index).tz_localize(None)
                sub.to_parquet(_cache_path(orig_sym, "history"))
                results[orig_sym] = sub
                if checkpoint:
                    checkpoint.mark_done("history", orig_sym)
            except Exception as e:
                logger.debug(f"[bulk-hist-parse] {orig_sym}: {e}")
                results[orig_sym] = None
                if checkpoint:
                    checkpoint.mark_failed("history", orig_sym, f"parse:{e}"[:80])

        got = sum(1 for s in chunk if results.get(s) is not None)
        logger.info(f"[bulk-hist] chunk {idx}/{len(chunks)} done, "
                    f"{got}/{len(chunk)} got data")
        if checkpoint:
            checkpoint.flush()
        time.sleep(0.4 + random.uniform(0, 0.6))  # 批间节流
    return results


def fetch_batch_parallel(
    symbols: list[str],
    days: int = 400,
    cache_hours: float = 4.0,
    max_workers: int = 12,
    with_earnings: bool = True,
    use_bulk_history: bool = True,
    checkpoint=None,
    circuit_breaker=None,
) -> dict:
    """
    批量拉取 history + info (+ earnings)。
    返回 {symbol: {"history": df, "info": dict, "earnings": dt_or_none}}

    默认策略（use_bulk_history=True）：
      Stage 1: 用 yf.download 分批批量拉历史（每批80只，最快）
      Stage 2: 并发拉 info
      Stage 3: 并发拉 earnings（可选）
    """
    logger.info(f"[batch-parallel] fetching {len(symbols)} tickers "
                f"(workers={max_workers}, days={days}, bulk={use_bulk_history})...")
    t0 = time.time()

    # Stage 1: 历史
    if use_bulk_history:
        hist_map = _download_history_bulk(
            symbols, days=days, cache_hours=cache_hours,
            checkpoint=checkpoint, circuit_breaker=circuit_breaker,
        )
    else:
        hist_map = _parallel_map(
            lambda s: fetch_history(s, days=days, cache_hours=cache_hours),
            symbols, max_workers=max_workers, desc="history", log_every=100,
        )

    # 熔断后跳过 info 和 earnings 阶段
    if circuit_breaker and circuit_breaker.tripped:
        logger.warning("[batch-parallel] 熔断已触发，跳过 info/earnings 阶段")
        results = {}
        for s in symbols:
            results[s] = {
                "history": hist_map.get(s),
                "info": {},
                "earnings": None,
            }
        elapsed = time.time() - t0
        ok = sum(1 for v in results.values() if v.get("history") is not None)
        logger.info(f"[batch-parallel] partial done in {elapsed:.1f}s, "
                    f"with-history={ok}/{len(symbols)} (熔断后退出)")
        return results

    # Stage 2: info（并发）— 只对有历史的拉，节省请求
    syms_with_hist = [s for s in symbols if hist_map.get(s) is not None]
    info_map = _parallel_map(
        lambda s: fetch_info(s),
        syms_with_hist, max_workers=max_workers, desc="info", log_every=100,
    )

    # Stage 3: earnings（并发，可选）
    if with_earnings:
        earn_map = _parallel_map(
            lambda s: fetch_earnings_date(s),
            syms_with_hist, max_workers=max_workers, desc="earnings", log_every=200,
        )
    else:
        earn_map = {}

    # 组装
    results = {}
    for s in symbols:
        results[s] = {
            "history": hist_map.get(s),
            "info": info_map.get(s) or {},
            "earnings": earn_map.get(s),
        }

    elapsed = time.time() - t0
    ok = sum(1 for v in results.values() if v.get("history") is not None)
    logger.info(f"[batch-parallel] done in {elapsed:.1f}s, "
                f"with-history={ok}/{len(symbols)}")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    test = fetch_batch(["SPY", "ADBE", "NVDA"], days=300)
    for k, v in test.items():
        h = v["history"]
        info = v["info"]
        print(f"{k}: history rows={len(h) if h is not None else 0}, "
              f"market_cap={info.get('marketCap')}, fwd_pe={info.get('forwardPE')}")
