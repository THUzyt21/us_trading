"""
universe.py - 美股全市场股票池管理
数据源：NASDAQ Trader 官方每日发布的股票清单（免费、权威、每日更新）
  - https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt       (NASDAQ)
  - https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt        (NYSE/AMEX/ARCA)

功能：
  1) 下载全美股上市清单，缓存到 cache/universe/
  2) 按 sector / industry / 市值 / 成交量过滤（需配合 yfinance info 数据）
  3) 预定义"科技股"和"资源股"的 sector/industry 集合
"""
from __future__ import annotations

import io
import logging
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_CACHE = PROJECT_ROOT / "cache" / "universe"
UNIVERSE_CACHE.mkdir(parents=True, exist_ok=True)

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# ============== 行业映射（yfinance 的 sector/industry 分类体系）==============

# 科技股相关的 yfinance sector
TECH_SECTORS = {
    "Technology",
    "Communication Services",  # GOOGL/META/NFLX 等在这里
}

# 科技股相关 industry（跨 sector 的半导体/互联网/云/软件等）
TECH_INDUSTRIES = {
    "Semiconductors",
    "Semiconductor Equipment & Materials",
    "Software—Application",
    "Software—Infrastructure",
    "Information Technology Services",
    "Computer Hardware",
    "Consumer Electronics",
    "Electronic Components",
    "Electronic Gaming & Multimedia",
    "Internet Content & Information",
    "Internet Retail",
    "Scientific & Technical Instruments",
    "Solar",
    "Communication Equipment",
    "Telecom Services",
}

# 资源股相关 sector（能源 + 基础材料）
RESOURCE_SECTORS = {
    "Basic Materials",
    "Energy",
}

# 资源股相关 industry（金属/矿业/油气）
RESOURCE_INDUSTRIES = {
    "Gold",
    "Silver",
    "Copper",
    "Other Precious Metals & Mining",
    "Other Industrial Metals & Mining",
    "Aluminum",
    "Steel",
    "Coking Coal",
    "Uranium",
    "Oil & Gas Integrated",
    "Oil & Gas E&P",
    "Oil & Gas Midstream",
    "Oil & Gas Refining & Marketing",
    "Oil & Gas Equipment & Services",
    "Oil & Gas Drilling",
    "Thermal Coal",
    "Agricultural Inputs",      # 化肥（资源属性）
    "Specialty Chemicals",
    "Chemicals",
    "Lumber & Wood Production",
    "Paper & Paper Products",
}


# ========================== 数据结构 ==========================

@dataclass
class UniverseRow:
    symbol: str
    name: str
    exchange: str     # Q=NASDAQ / N=NYSE / A=AMEX / P=ARCA
    etf: bool
    test_issue: bool


# ========================== 下载全美股清单 ==========================

def _is_fresh(path: Path, max_hours: float) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) / 3600 < max_hours


def _download(url: str, dest: Path, timeout: int = 30) -> None:
    logger.info(f"[universe] downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    dest.write_bytes(data)
    logger.info(f"[universe] saved to {dest} ({len(data)} bytes)")


def _parse_nasdaq_listed(text: str) -> pd.DataFrame:
    """
    nasdaqlisted.txt 字段：
      Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    """
    df = pd.read_csv(io.StringIO(text), sep="|")
    # 最后一行是 "File Creation Time"，过滤掉
    df = df[df["Symbol"].notna() & ~df["Symbol"].astype(str).str.startswith("File")]
    out = pd.DataFrame({
        "symbol": df["Symbol"].astype(str).str.strip(),
        "name": df["Security Name"].astype(str).str.strip(),
        "exchange": "Q",  # NASDAQ
        "etf": df["ETF"].astype(str).str.upper().eq("Y"),
        "test_issue": df["Test Issue"].astype(str).str.upper().eq("Y"),
    })
    return out


def _parse_other_listed(text: str) -> pd.DataFrame:
    """
    otherlisted.txt 字段：
      ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    Exchange: A=AMEX / N=NYSE / P=NYSE Arca / Z=Cboe BZX
    """
    df = pd.read_csv(io.StringIO(text), sep="|")
    df = df[df["ACT Symbol"].notna() & ~df["ACT Symbol"].astype(str).str.startswith("File")]
    out = pd.DataFrame({
        "symbol": df["ACT Symbol"].astype(str).str.strip(),
        "name": df["Security Name"].astype(str).str.strip(),
        "exchange": df["Exchange"].astype(str).str.strip(),
        "etf": df["ETF"].astype(str).str.upper().eq("Y"),
        "test_issue": df["Test Issue"].astype(str).str.upper().eq("Y"),
    })
    return out


def load_full_universe(
    max_hours: float = 24.0,
    include_etf: bool = False,
    force: bool = False,
) -> pd.DataFrame:
    """
    加载全美股上市清单（NASDAQ + NYSE/AMEX/ARCA）。
    缓存 24 小时。返回列：symbol, name, exchange, etf, test_issue
    """
    nasdaq_file = UNIVERSE_CACHE / "nasdaqlisted.txt"
    other_file = UNIVERSE_CACHE / "otherlisted.txt"

    if force or not _is_fresh(nasdaq_file, max_hours):
        _download(NASDAQ_LISTED_URL, nasdaq_file)
    if force or not _is_fresh(other_file, max_hours):
        _download(OTHER_LISTED_URL, other_file)

    ndq = _parse_nasdaq_listed(nasdaq_file.read_text(encoding="utf-8", errors="replace"))
    oth = _parse_other_listed(other_file.read_text(encoding="utf-8", errors="replace"))
    full = pd.concat([ndq, oth], ignore_index=True)

    # 过滤 test issue
    full = full[~full["test_issue"]].copy()
    # 过滤 ETF（除非明确要）
    if not include_etf:
        full = full[~full["etf"]].copy()

    # 过滤掉明显不是普通股的符号：
    # 1) 带 $ / . / = / + / ^ 的 → 优先股、多级股
    # 2) 长度 >= 5 且以 W/U/R 结尾 → Warrant / Units / Rights
    #    （注意：长度 4 的 W/U 结尾普通股存在，不过滤，如 IWM、TWLO）
    #    实际 NASDAQ 规则是：Warrant/Units/Rights 都是在原 ticker 基础上追加 W/U/R
    #    所以本 NASDAQ 股池里，超过原票 ≥4 字母后追加后缀的会变成 ≥5 字母
    bad_char = r"[\.\$=\+\^]"
    full = full[~full["symbol"].str.contains(bad_char, regex=True, na=False)].copy()
    bad_suffix = full["symbol"].str.match(r"^.{3,}[WUR]$", na=False) & (full["symbol"].str.len() >= 5)
    full = full[~bad_suffix].copy()
    # 过滤证券名称中明显包含 Warrant/Unit/Right/Preferred 的
    name_bad = full["name"].str.contains(
        r"\b(Warrant|Right|Unit|Units|Preferred|Depositary|Debenture|Note|Trust Units)\b",
        case=False, regex=True, na=False,
    )
    full = full[~name_bad].copy()

    # 去重（偶见 NASDAQ/NYSE 冲突，保留 NASDAQ）
    full = full.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)

    logger.info(f"[universe] full universe loaded: {len(full)} tickers "
                f"(NASDAQ={len(ndq)}, Other={len(oth)})")
    return full


# ========================== 筛选 ==========================

def filter_by_sectors(
    candidates: Iterable[str],
    infos: dict,
    sectors: Optional[set] = None,
    industries: Optional[set] = None,
    min_market_cap: float = 2e9,
    min_avg_volume: float = 5e5,
) -> list[str]:
    """
    根据已拉取的 info dict 对 candidates 做二次筛选。
    sectors / industries 任一命中即保留。
    """
    sectors = sectors or set()
    industries = industries or set()
    out = []
    for sym in candidates:
        info = infos.get(sym) or {}
        if not info:
            continue
        sec = info.get("sector")
        ind = info.get("industry")
        mc = info.get("marketCap") or 0
        vol = info.get("averageVolume") or info.get("averageVolume10days") or 0

        hit_sector = bool(sectors) and sec in sectors
        hit_industry = bool(industries) and ind in industries
        if not (hit_sector or hit_industry):
            continue
        if mc < min_market_cap:
            continue
        if vol < min_avg_volume:
            continue
        out.append(sym)
    return out


def get_tech_resource_universe(
    include_tech: bool = True,
    include_resource: bool = True,
) -> tuple[set, set]:
    """返回 (sectors, industries) 两个集合，用于过滤"""
    sectors, industries = set(), set()
    if include_tech:
        sectors |= TECH_SECTORS
        industries |= TECH_INDUSTRIES
    if include_resource:
        sectors |= RESOURCE_SECTORS
        industries |= RESOURCE_INDUSTRIES
    return sectors, industries


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    df = load_full_universe()
    print(f"Total: {len(df)}")
    print(df.head(10))
    print(df["exchange"].value_counts())
