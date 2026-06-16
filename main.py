"""Free-data daily US stock picker — no API keys needed.

Sources: Finviz screeners, Nasdaq pre-market, StockTwits trending, Reddit WSB.
Enrichment: yfinance (market cap, avg volume, sector, ATR).
Delivery: Telegram.
"""
from __future__ import annotations
import asyncio
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TOP_N = int(os.getenv("TOP_N", "33"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "5"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP_M", "500")) * 1e6
MIN_DOLLAR_VOL = float(os.getenv("MIN_DOLLAR_VOL_M", "5")) * 1e6
MAX_PER_SECTOR = int(os.getenv("MAX_PER_SECTOR", "3"))
ENRICH_POOL = int(os.getenv("ENRICH_POOL", "50"))
CACHE_DIR = os.getenv("CACHE_DIR", "cache")
WEEKLY_DIR = os.getenv("WEEKLY_DIR", "weekly")  # legacy v7 artifact dir (kept)
TRACKING_DIR = os.getenv("TRACKING_DIR", "tracking")  # v8 rolling cohorts, committed
DOCS_DIR = os.getenv("DOCS_DIR", "docs")  # v8 HTML dashboard for GitHub Pages
TRACKING_WINDOW = int(os.getenv("TRACKING_WINDOW", "10"))  # trading days per cohort
FALLBACK_MIN = int(os.getenv("FALLBACK_MIN", "5"))
ATR_STOP_MULT = float(os.getenv("ATR_STOP_MULT", "1.5"))
ATR_TARGET_MULT = float(os.getenv("ATR_TARGET_MULT", "3.0"))

SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NDX_WIKI = "https://en.wikipedia.org/wiki/Nasdaq-100"
UNIVERSE_CACHE_TTL = 7 * 24 * 3600  # 1 week

# Last-resort backup if Wikipedia is also unreachable.
BACKUP_UNIVERSE: list[str] = [
    # Tech (15)
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "ORCL", "AVGO", "ADBE",
    "CRM", "AMD", "CSCO", "INTC", "TXN", "QCOM", "IBM",
    # Financial (7)
    "JPM", "V", "MA", "BAC", "WFC", "GS", "SCHW",
    # Healthcare (8)
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "DHR",
    # Consumer (8)
    "AMZN", "TSLA", "WMT", "COST", "HD", "KO", "PEP", "MCD",
    # Industrial (4)
    "CAT", "HON", "UNP", "BA",
    # Energy (3)
    "XOM", "CVX", "COP",
    # Communications (3)
    "NFLX", "T", "TMUS",
    # Utilities (2)
    "NEE", "DUK",
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

FINVIZ_SCREENERS = [
    ("涨幅榜", "v=111&s=ta_topgainers"),
    ("Gap-Up", "v=111&f=sh_relvol_o2,ta_gap_u3"),
    ("异动量", "v=111&s=ta_unusualvolume"),
    ("52周新高", "v=111&s=ta_newhigh"),
]

REDDIT_SUBS = ["wallstreetbets", "stocks", "investing", "smallstreetbets", "options"]


def parse_num(s: str) -> float:
    s = (s or "").strip().replace(",", "").replace("$", "")
    if not s or s == "-":
        return 0.0
    mult = 1.0
    if s.endswith("B"):
        mult, s = 1e9, s[:-1]
    elif s.endswith("M"):
        mult, s = 1e6, s[:-1]
    elif s.endswith("K"):
        mult, s = 1e3, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def parse_pct(s: str) -> float:
    s = (s or "").strip().replace("%", "").replace("+", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _normalize_ticker(t: str) -> str:
    """Wikipedia uses BRK.B; yfinance expects BRK-B."""
    return t.replace(".", "-").strip()


async def fetch_finviz_screener(client: httpx.AsyncClient, name: str, query: str) -> list[dict]:
    url = f"https://finviz.com/screener.ashx?{query}"
    try:
        r = await client.get(url, headers={"User-Agent": UA})
        if r.status_code != 200:
            print(f"  {name}: HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "lxml")

        tbl = None
        for candidate in soup.find_all("table"):
            if candidate.find("a", class_="screener-link-primary"):
                tbl = candidate
                break
        if not tbl:
            return []

        rows: list[dict] = []
        for tr in tbl.find_all("tr"):
            ticker_a = tr.find("a", class_="screener-link-primary")
            if not ticker_a:
                continue
            tds = tr.find_all("td")
            if len(tds) < 10:
                continue
            ticker = ticker_a.text.strip()
            try:
                price = parse_num(tds[8].text)
                change = parse_pct(tds[9].text)
                volume = parse_num(tds[10].text) if len(tds) > 10 else 0
            except Exception:
                price, change, volume = 0, 0, 0
            if ticker and price >= 3:
                rows.append({
                    "ticker": ticker,
                    "price": price,
                    "change": change,
                    "volume": volume,
                    "source": name,
                })
        return rows[:40]
    except Exception as e:
        print(f"  {name} error: {e}")
        return []


async def fetch_stocktwits_trending(client: httpx.AsyncClient) -> set[str]:
    try:
        r = await client.get(
            "https://api.stocktwits.com/api/2/trending/symbols.json",
            headers={"User-Agent": UA},
        )
        if r.status_code != 200:
            return set()
        return {s.get("symbol", "") for s in r.json().get("symbols", [])}
    except Exception as e:
        print(f"  StockTwits error: {e}")
        return set()


async def fetch_reddit_mentions(client: httpx.AsyncClient, candidates: set[str]) -> dict[str, int]:
    import re
    from collections import Counter

    counter: Counter[str] = Counter()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()

    for sub in REDDIT_SUBS:
        for listing in ["hot", "new"]:
            try:
                r = await client.get(
                    f"https://www.reddit.com/r/{sub}/{listing}.json?limit=100",
                    headers={"User-Agent": UA},
                )
                if r.status_code != 200:
                    continue
                posts = [c.get("data", {}) for c in r.json().get("data", {}).get("children", [])]
                for p in posts:
                    if p.get("created_utc", 0) < cutoff:
                        continue
                    text = f"{p.get('title','')} {p.get('selftext','')}"
                    cashtags = set(re.findall(r"\$([A-Z]{1,5})\b", text))
                    bare = set(re.findall(r"\b([A-Z]{2,5})\b", text))
                    hits = cashtags & candidates
                    if not hits:
                        hits = bare & candidates
                    for t in hits:
                        counter[t] += 1
                await asyncio.sleep(1.0)
            except Exception as e:
                print(f"  Reddit {sub}/{listing} error: {e}")
    return dict(counter)


async def fetch_nasdaq_premarket(client: httpx.AsyncClient) -> list[dict]:
    try:
        r = await client.get(
            "https://api.nasdaq.com/api/marketmovers/PREMARKET",
            params={"limit": 50, "type": "GAINERS"},
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
                "Origin": "https://www.nasdaq.com",
                "Referer": "https://www.nasdaq.com/",
            },
        )
        if r.status_code != 200:
            return []
        data = r.json()
        rows = ((data.get("data") or {}).get("data") or {}).get("rows") or []
        return [
            {
                "ticker": row.get("symbol", ""),
                "price": parse_num(row.get("lastSalePrice", "")),
                "change": parse_pct(row.get("percentageChange", "")),
                "source": "Nasdaq盘前",
            }
            for row in rows
            if row.get("symbol") and parse_pct(row.get("percentageChange", "")) >= 2
        ]
    except Exception as e:
        print(f"  Nasdaq error: {e}")
        return []


async def _fetch_wiki_constituents(
    client: httpx.AsyncClient, url: str, name: str
) -> list[str]:
    """Pull tickers from the first wikitable on `url` with a Symbol/Ticker column."""
    try:
        r = await client.get(url, headers={"User-Agent": UA})
        if r.status_code != 200:
            print(f"  {name}: HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"  {name} error: {e}")
        return []

    for tbl in soup.find_all("table", class_="wikitable"):
        first_row = tbl.find("tr")
        if not first_row:
            continue
        headers = [th.get_text(strip=True).lower() for th in first_row.find_all("th")]
        sym_idx = next(
            (i for i, h in enumerate(headers) if h in ("symbol", "ticker")),
            None,
        )
        if sym_idx is None:
            continue

        tickers: list[str] = []
        for tr in tbl.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) <= sym_idx:
                continue
            raw = tds[sym_idx].get_text(strip=True)
            if not raw:
                continue
            t = _normalize_ticker(raw)
            if not (1 <= len(t) <= 6):
                continue
            if not all(c.isupper() or c == "-" for c in t):
                continue
            tickers.append(t)

        if len(tickers) >= 50:
            return tickers
    return []


async def load_dynamic_universe(
    client: httpx.AsyncClient,
) -> tuple[list[str], str]:
    """S&P 500 ∪ Nasdaq-100 from Wikipedia, cached 7d on disk.

    Returns (tickers, human_label). Falls back to BACKUP_UNIVERSE if both
    Wikipedia fetches fail or come back short.
    """
    cache_path = os.path.join(CACHE_DIR, "universe.json")
    os.makedirs(CACHE_DIR, exist_ok=True)

    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if time.time() - cached.get("ts", 0) < UNIVERSE_CACHE_TTL:
                tickers = cached.get("tickers", [])
                print(f"  Universe cache hit ({len(tickers)} tickers)")
                return tickers, "S&P500 + Nasdaq100"
        except Exception as e:
            print(f"  Universe cache load failed: {e}")

    sp500, ndx = await asyncio.gather(
        _fetch_wiki_constituents(client, SP500_WIKI, "S&P500"),
        _fetch_wiki_constituents(client, NDX_WIKI, "Nasdaq100"),
    )
    print(f"  S&P500: {len(sp500)}, Nasdaq100: {len(ndx)}")
    merged = sorted(set(sp500) | set(ndx))

    if len(merged) >= 400:
        if len(sp500) >= 400 and len(ndx) >= 80:
            try:
                with open(cache_path, "w") as f:
                    json.dump(
                        {
                            "ts": time.time(),
                            "fetched": datetime.now(timezone.utc).isoformat(),
                            "tickers": merged,
                        },
                        f,
                    )
            except Exception as e:
                print(f"  Universe cache save failed: {e}")
        return merged, "S&P500 + Nasdaq100"

    # Wikipedia blocked (e.g. GH Actions IP) — try repo-committed snapshot.
    repo_snapshot = "universe.json"
    if os.path.exists(repo_snapshot):
        try:
            with open(repo_snapshot) as f:
                static = json.load(f)
            tickers = static.get("tickers", [])
            if len(tickers) >= 400:
                print(
                    f"  Wikipedia unreachable; using committed universe.json "
                    f"({len(tickers)} tickers, fetched {static.get('fetched', '?')})"
                )
                return tickers, "S&P500 + Nasdaq100"
        except Exception as e:
            print(f"  Repo universe.json load failed: {e}")

    print(
        f"  Universe fetch failed ({len(merged)} tickers); "
        f"using {len(BACKUP_UNIVERSE)}-ticker backup"
    )
    return BACKUP_UNIVERSE, f"备用 {len(BACKUP_UNIVERSE)} 只大盘股"


def inject_fallback_universe(agg: dict[str, dict], tickers: list[str]) -> None:
    """Add any missing fallback-universe tickers to agg so enrichment + fundamental
    scoring can still run when free scrapers return nothing."""
    for t in tickers:
        if t not in agg:
            agg[t] = {
                "ticker": t,
                "sources": {"Default universe"},
                "price": 0,
                "change": 0,
                "volume": 0,
            }


def aggregate(all_rows: list[dict]) -> dict[str, dict]:
    agg: dict[str, dict] = {}
    for row in all_rows:
        t = row["ticker"]
        if t not in agg:
            agg[t] = {
                "ticker": t,
                "sources": set(),
                "price": 0,
                "change": 0,
                "volume": 0,
            }
        agg[t]["sources"].add(row["source"])
        if row.get("price"):
            agg[t]["price"] = max(agg[t]["price"], row["price"])
        if row.get("change"):
            agg[t]["change"] = (
                row["change"] if abs(row["change"]) > abs(agg[t]["change"]) else agg[t]["change"]
            )
        if row.get("volume"):
            agg[t]["volume"] = max(agg[t]["volume"], row["volume"])
    return agg


def _enrich_sync(ticker: str, max_retries: int = 3) -> dict | None:
    """Fetch market cap, avg volume, sector, last close, ATR(14), and fundamentals.

    Retries transient errors with exponential backoff (0.5s, 1s, 2s with jitter).
    Returns None if the ticker has insufficient price history (not retryable).
    """
    import yfinance as yf
    import pandas as pd

    for attempt in range(max_retries):
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
            # 1y bar — covers 52w high/low, 200d MA, and gives MACD/RSI room
            # to stabilize. Single HTTP call same as 1mo.
            hist = t.history(period="1y", auto_adjust=False)
            # Pre-market, Yahoo often appends today's not-yet-traded bar with
            # NaN OHLC. That NaN close poisoned everything downstream on
            # 2026-06-10 (last_close/ret_1d → nan, momentum zeroed, "$nan" in
            # Telegram). Keep only rows with a real close.
            hist = hist[hist["Close"].notna()]
            if hist.empty or len(hist) < 15:
                return None
            high, low, close, volume = (
                hist["High"], hist["Low"], hist["Close"], hist["Volume"]
            )
            prev = close.shift(1)
            tr = pd.concat(
                [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
            ).max(axis=1)
            atr_val = tr.rolling(14).mean().iloc[-1]

            # --- Short-term momentum (last 1mo) -------------------------------
            last_close = float(close.iloc[-1])
            ret_1d = (
                (last_close / float(close.iloc[-2]) - 1) * 100
                if len(close) >= 2 else 0.0
            )
            ret_5d = (
                (last_close / float(close.iloc[-6]) - 1) * 100
                if len(close) >= 6 else 0.0
            )
            vol_window = volume.tail(20)
            vol_avg = float(vol_window.mean()) if len(vol_window) >= 5 else 0.0
            vol_ratio = (
                float(volume.iloc[-1]) / vol_avg if vol_avg > 0 else 0.0
            )
            # near_high stays "last 20 bars" (month high) for back-compat
            month_high = float(high.tail(20).max())
            near_high = last_close / month_high if month_high > 0 else 0.0

            # --- Technical (52w lookback) -------------------------------------
            high_52w = float(high.max())
            dist_52w = last_close / high_52w if high_52w > 0 else 0.0

            ma200_pos = 0.0
            if len(close) >= 200:
                ma200 = float(close.rolling(200).mean().iloc[-1])
                if ma200 > 0:
                    ma200_pos = last_close / ma200

            # RSI(14) using Wilder smoothing approximation via SMA.
            rsi14 = 50.0
            if len(close) >= 15:
                delta = close.diff()
                gains = delta.clip(lower=0)
                losses = -delta.clip(upper=0)
                avg_gain = float(gains.rolling(14).mean().iloc[-1])
                avg_loss = float(losses.rolling(14).mean().iloc[-1])
                if avg_loss == 0:
                    rsi14 = 100.0 if avg_gain > 0 else 50.0
                else:
                    rs = avg_gain / avg_loss
                    rsi14 = 100.0 - (100.0 / (1.0 + rs))

            # MACD state: 0 unset, +2 fresh golden cross within 5d,
            # +1 ongoing bullish, -1 ongoing bearish, -2 fresh death cross.
            macd_state = 0
            if len(close) >= 35:
                ema12 = close.ewm(span=12, adjust=False).mean()
                ema26 = close.ewm(span=26, adjust=False).mean()
                macd_line = ema12 - ema26
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                diff = macd_line - signal_line
                today_pos = float(diff.iloc[-1]) > 0
                recent_other_sign = (
                    (diff.iloc[-6:-1] < 0).any() if today_pos
                    else (diff.iloc[-6:-1] > 0).any()
                )
                if today_pos:
                    macd_state = 2 if recent_other_sign else 1
                else:
                    macd_state = -2 if recent_other_sign else -1

            return {
                "ticker": ticker,
                "market_cap": float(info.get("marketCap") or 0),
                "avg_volume": float(
                    info.get("averageVolume10days") or info.get("averageVolume") or 0
                ),
                "sector": info.get("sector") or "Unknown",
                "last_close": last_close,
                "atr": float(atr_val) if atr_val == atr_val else 0.0,
                "trailing_pe": float(info.get("trailingPE") or 0),
                "forward_pe": float(info.get("forwardPE") or 0),
                "peg": float(info.get("pegRatio") or info.get("trailingPegRatio") or 0),
                "roe": float(info.get("returnOnEquity") or 0),
                "revenue_growth": float(info.get("revenueGrowth") or 0),
                "earnings_growth_q": float(info.get("earningsQuarterlyGrowth") or 0),
                "ret_1d": ret_1d,
                "ret_5d": ret_5d,
                "vol_ratio": vol_ratio,
                "near_high": near_high,
                "dist_52w": dist_52w,
                "ma200_pos": ma200_pos,
                "rsi14": float(rsi14),
                "macd_state": int(macd_state),
            }
        except Exception:
            if attempt == max_retries - 1:
                return None
            time.sleep(0.5 * (2 ** attempt) + random.random() * 0.3)
    return None


def _cache_path(kind: str, date_str: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{kind}_{date_str}.json")


def load_enrich_cache(date_str: str) -> dict[str, dict]:
    path = _cache_path("enrich", date_str)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  Cache load failed: {e}")
        return {}


def save_enrich_cache(date_str: str, data: dict[str, dict]) -> None:
    path = _cache_path("enrich", date_str)
    try:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  Cache save failed: {e}")


def save_run_log(date_str: str, picks: list[dict], filtered_count: int, enriched_count: int) -> None:
    path = _cache_path("run", date_str)
    summary = {
        "date": date_str,
        "ts": datetime.now(timezone.utc).isoformat(),
        "enriched_count": enriched_count,
        "filtered_count": filtered_count,
        "config": {
            "MIN_PRICE": MIN_PRICE,
            "MIN_MARKET_CAP": MIN_MARKET_CAP,
            "MIN_DOLLAR_VOL": MIN_DOLLAR_VOL,
            "MAX_PER_SECTOR": MAX_PER_SECTOR,
            "ENRICH_POOL": ENRICH_POOL,
        },
        "picks": [
            {
                "rank": i,
                "ticker": p["ticker"],
                "score": p.get("score"),
                "heat_score": p.get("heat_score"),
                "fund_score": p.get("fund_score"),
                "mom_score": p.get("mom_score"),
                "tech_score": p.get("tech_score"),
                "sector": p.get("sector"),
                "last_close": p.get("last_close"),
                "change": p.get("change"),
                "atr": p.get("atr"),
                "market_cap": p.get("market_cap"),
                "trailing_pe": p.get("trailing_pe"),
                "forward_pe": p.get("forward_pe"),
                "peg": p.get("peg"),
                "roe": p.get("roe"),
                "revenue_growth": p.get("revenue_growth"),
                "earnings_growth_q": p.get("earnings_growth_q"),
                "ret_1d": p.get("ret_1d"),
                "ret_5d": p.get("ret_5d"),
                "vol_ratio": p.get("vol_ratio"),
                "near_high": p.get("near_high"),
                "dist_52w": p.get("dist_52w"),
                "ma200_pos": p.get("ma200_pos"),
                "rsi14": p.get("rsi14"),
                "macd_state": p.get("macd_state"),
                "sources": sorted(p.get("sources", set())),
                "trending": p.get("trending", False),
                "reddit": p.get("reddit", 0),
            }
            for i, p in enumerate(picks, 1)
        ],
    }
    try:
        with open(path, "w") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  Run log save failed: {e}")


async def enrich_candidates(tickers: list[str], use_cache: bool = True) -> dict[str, dict]:
    date_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    cache = load_enrich_cache(date_str) if use_cache else {}
    missing = [t for t in tickers if t not in cache]
    hits = len(tickers) - len(missing)
    if hits:
        print(f"  Cache hits: {hits}/{len(tickers)}")

    newly: dict[str, dict] = {}
    if missing:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = await asyncio.gather(
                *[loop.run_in_executor(pool, _enrich_sync, t) for t in missing],
                return_exceptions=True,
            )
        newly = {r["ticker"]: r for r in results if isinstance(r, dict) and r}

    if newly:
        save_enrich_cache(date_str, {**cache, **newly})

    merged = {**cache, **newly}
    return {t: merged[t] for t in tickers if t in merged}


def score(
    agg: dict[str, dict],
    stocktwits_trending: set[str],
    reddit_mentions: dict[str, int],
) -> list[dict]:
    for t, c in agg.items():
        s = 0
        sources = c["sources"]
        s += len(sources) * 20

        if "Gap-Up" in sources:
            s += 15
        if "52周新高" in sources:
            s += 10
        if "异动量" in sources:
            s += 10
        if "Nasdaq盘前" in sources:
            s += 15

        s += min(abs(c["change"]), 30)

        if t in stocktwits_trending:
            s += 15
            c["trending"] = True
        else:
            c["trending"] = False

        r_count = reddit_mentions.get(t, 0)
        c["reddit"] = r_count
        if r_count > 0:
            s += min(math.log(r_count + 1) * 8, 20)

        c["heat_score"] = s
        c["score"] = s

    ranked = [c for c in agg.values() if c["ticker"]]
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def hard_filter(candidates: list[dict], enrich: dict[str, dict]) -> list[dict]:
    """Drop low-cap / illiquid / penny-stock names using yfinance enrichment."""
    kept = []
    for c in candidates:
        e = enrich.get(c["ticker"])
        price = e["last_close"] if e else c.get("price", 0)
        if price < MIN_PRICE:
            continue
        if e:
            if e["market_cap"] < MIN_MARKET_CAP:
                continue
            if e["avg_volume"] * price < MIN_DOLLAR_VOL:
                continue
            for k in (
                "last_close", "atr", "sector", "market_cap",
                "trailing_pe", "forward_pe", "peg",
                "roe", "revenue_growth", "earnings_growth_q",
                "ret_1d", "ret_5d", "vol_ratio", "near_high",
                "dist_52w", "ma200_pos", "rsi14", "macd_state",
            ):
                if k in e:
                    c[k] = e[k]
        kept.append(c)
    return kept


def fundamental_score(c: dict) -> float:
    """Bucket-based fundamental boost. Range ≈ −9 to +35."""
    s = 0.0
    pe = c.get("forward_pe") or c.get("trailing_pe") or 0
    if 0 < pe <= 25:
        s += 5
    elif 25 < pe <= 40:
        s += 0
    elif pe > 40 or pe < 0:
        s -= 4

    peg = c.get("peg") or 0
    if 0 < peg <= 1:
        s += 8
    elif 1 < peg <= 2:
        s += 3

    roe = c.get("roe") or 0
    if roe >= 0.25:
        s += 8
    elif roe >= 0.15:
        s += 4

    rg = c.get("revenue_growth") or 0
    if rg >= 0.20:
        s += 6
    elif rg >= 0.10:
        s += 3

    eg = c.get("earnings_growth_q") or 0
    if eg >= 0.20:
        s += 5
    elif eg >= 0.10:
        s += 2
    elif eg <= -0.20:
        s -= 5

    return s


def momentum_score(c: dict) -> float:
    """Bucket-based momentum boost from yfinance price/volume action.

    Rebalanced 2026-05-22 to avoid "tops bias": prior version stacked +40
    purely for "already at month high + just had a +5% day", which made the
    picker chase parabolic moves. Now:

      - `ret_1d ≥8%` actively penalized (noise/exhaustion)
      - `near_high ≥0.98` (literally at month peak) penalized
      - Sweet spot moved to `near_high 0.88-0.92` — i.e. an 8-12% pullback
        from the month high, still inside the uptrend

    The four signals are computed in `_enrich_sync` from the same 1-month
    bar yfinance already fetches for ATR; no extra network. Range ≈ −10 to
    +28.
    """
    s = 0.0

    r1 = c.get("ret_1d") or 0
    if r1 >= 8:
        s -= 3      # 单日 ≥8% 通常是新闻噪音或日内反转风险
    elif r1 >= 5:
        s += 2      # 大涨但不极端
    elif r1 >= 2:
        s += 6      # 温和上涨
    elif r1 >= 1:
        s += 3
    elif r1 <= -3:
        s -= 5

    r5 = c.get("ret_5d") or 0
    if r5 >= 10:
        s += 10
    elif r5 >= 5:
        s += 5
    elif r5 <= -5:
        s -= 3

    vr = c.get("vol_ratio") or 0
    if vr >= 3:
        s += 10
    elif vr >= 2:
        s += 5

    nh = c.get("near_high") or 0
    if nh >= 0.98:
        s -= 2      # 顶到月内最高，反转风险
    elif nh >= 0.93:
        s += 5
    elif nh >= 0.88:
        s += 7      # 健康回调甜区（距月高 7-12%）
    elif nh >= 0.82:
        s += 4      # 较深回调，仍在趋势内
    elif 0 < nh < 0.70:
        s -= 2      # 跌得太深

    return s


def technical_score(c: dict) -> float:
    """Longer-horizon technical signals from the 1y yfinance bar.

    Rebalanced 2026-05-22 alongside `momentum_score` to defuse "tops bias":
    creating a 52w high or sitting >30% above 200d MA is now neutral-to-
    negative rather than the previous max bonus. RSI penalties for >75/>80
    sharpened from −2 to −8/−15.

    A new composite `quality_dip` rewards the canonical "buy strength on
    pullback" setup: above 200d MA + recent pullback from month high +
    healthy (not stretched) RSI.

    Range ≈ −20 to +25.
    """
    s = 0.0

    d = c.get("dist_52w") or 0
    if d >= 0.98:
        s -= 2          # 创 52w 新高，反转风险
    elif d >= 0.92:
        s += 5
    elif d >= 0.85:
        s += 6          # 距 52w 高 8-15%，趋势内回调甜区
    elif d >= 0.70:
        s += 0          # 中性区
    elif 0 < d <= 0.50:
        s -= 3          # 深度熊势

    ma = c.get("ma200_pos") or 0
    if ma >= 1.30:
        s += 0          # 距 200d 均线 ≥30%，过度拉伸
    elif ma >= 1.10:
        s += 5
    elif ma >= 1.00:
        s += 3
    elif ma >= 0.95:
        s += 1          # 在 200d 附近，可能是入场点
    elif 0 < ma < 0.95:
        s -= 3

    rsi = c.get("rsi14") or 0
    if rsi > 0:
        if rsi > 80:
            s -= 15     # 极端超买
        elif rsi > 75:
            s -= 8      # 超买
        elif rsi > 65:
            s += 0      # 偏高位，不奖也不罚
        elif rsi >= 50:
            s += 5      # 健康上涨区
        elif rsi >= 40:
            s += 4      # 弱回调，潜在反转
        elif rsi >= 30:
            s += 2      # 深回调
        elif rsi >= 25:
            s += 5      # 超卖反弹机会
        else:
            s += 8      # 深度超卖

    m = c.get("macd_state") or 0
    if m == 2:
        s += 6
    elif m == 1:
        s += 3
    elif m == -1:
        s -= 1
    elif m == -2:
        s -= 4

    # Quality dip composite: 长期上涨 + 短期回调 + 不超买
    nh = c.get("near_high") or 0
    if ma >= 1.05 and 0.80 <= nh <= 0.92 and 35 <= rsi <= 65:
        s += 6

    return s


def apply_boosts(candidates: list[dict]) -> list[dict]:
    """Apply fundamental + momentum + technical boosts on top of heat_score."""
    for c in candidates:
        c["fund_score"] = fundamental_score(c)
        c["mom_score"] = momentum_score(c)
        c["tech_score"] = technical_score(c)
        c["score"] += c["fund_score"] + c["mom_score"] + c["tech_score"]
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def fund_tag(c: dict) -> str:
    # _finite_or_none everywhere: `if roe:` style checks let NaN through
    # (NaN is truthy), which printed "ROE nan%" in the 2026-06-10 push.
    parts = []
    pe = _finite_or_none(c.get("forward_pe")) or _finite_or_none(c.get("trailing_pe")) or 0
    if pe > 0:
        parts.append(f"PE {pe:.0f}")
    peg = _finite_or_none(c.get("peg")) or 0
    if peg > 0:
        parts.append(f"PEG {peg:.1f}")
    roe = _finite_or_none(c.get("roe")) or 0
    if roe:
        parts.append(f"ROE {roe * 100:.0f}%")
    rg = _finite_or_none(c.get("revenue_growth")) or 0
    if rg:
        parts.append(f"Rev {rg * 100:+.0f}%")
    return " · ".join(parts)


def signal_tag(c: dict) -> str:
    """Combined momentum + technical signal tag for the Telegram per-pick line.

    Kept as one line (instead of two) so the message doesn't bloat to 5+
    Telegram chunks. Only includes fields that actually triggered something
    interesting — avoids visual noise for neutral readings.
    """
    parts = []

    r5 = c.get("ret_5d") or 0
    if abs(r5) >= 0.5:
        parts.append(f"5d {r5:+.1f}%")

    vr = c.get("vol_ratio") or 0
    if vr >= 1.5:
        parts.append(f"量比 {vr:.1f}x")

    d = c.get("dist_52w") or 0
    if d >= 0.95:
        parts.append(f"距52w高 {(1 - d) * 100:.1f}%")
    elif 0 < d <= 0.50:
        parts.append(f"距52w高 {(1 - d) * 100:.0f}%")  # near 52w low

    ma = c.get("ma200_pos") or 0
    if ma >= 1.10:
        parts.append(f">200日均 {(ma - 1) * 100:.0f}%")
    elif 0 < ma <= 0.95:
        parts.append(f"<200日均 {(1 - ma) * 100:.0f}%")

    rsi = c.get("rsi14") or 0
    if rsi > 75:
        parts.append(f"RSI{rsi:.0f}超买")
    elif 0 < rsi < 30:
        parts.append(f"RSI{rsi:.0f}超卖")

    m = c.get("macd_state") or 0
    if m == 2:
        parts.append("MACD金叉")
    elif m == -2:
        parts.append("MACD死叉")

    return " · ".join(parts)


def pick_diverse(candidates: list[dict], n: int, max_per_sector: int) -> list[dict]:
    """Select top-n with sector cap. Backfill if strict pass comes up short."""
    picks: list[dict] = []
    counts: dict[str, int] = defaultdict(int)
    for c in candidates:
        sec = c.get("sector", "Unknown")
        if counts[sec] >= max_per_sector:
            continue
        picks.append(c)
        counts[sec] += 1
        if len(picks) == n:
            return picks
    chosen = {c["ticker"] for c in picks}
    for c in candidates:
        if c["ticker"] in chosen:
            continue
        picks.append(c)
        if len(picks) == n:
            break
    return picks


def make_reason(c: dict) -> str:
    sources = c["sources"]
    parts: list[str] = []

    if sources == {"Default universe"}:
        fund = c.get("fund_score", 0)
        mom = c.get("mom_score", 0)
        tech = c.get("tech_score", 0)
        return f"默认池：基本面 {fund:+.0f} · 动量 {mom:+.0f} · 技术 {tech:+.0f}"

    if "Gap-Up" in sources and "涨幅榜" in sources:
        parts.append("盘前 Gap-Up 叠加盘中领涨")
    elif "Gap-Up" in sources:
        parts.append("盘前 Gap-Up")
    elif "52周新高" in sources and "涨幅榜" in sources:
        parts.append("突破 52 周高，强势")
    elif "52周新高" in sources:
        parts.append("创 52 周新高")
    elif "异动量" in sources and "涨幅榜" in sources:
        parts.append("成交量异常 + 涨幅居前")
    elif "异动量" in sources:
        parts.append("成交量异常放大")
    elif "Nasdaq盘前" in sources:
        parts.append(f"盘前涨 {c['change']:+.1f}%")
    elif "涨幅榜" in sources:
        parts.append("盘中涨幅居前")

    tags = []
    if c.get("trending"):
        tags.append("社交热榜")
    if c.get("reddit", 0) >= 3:
        tags.append(f"Reddit 提及 {c['reddit']}")
    if tags:
        parts.append("; ".join(tags))

    return " · ".join(parts) or "出现在多个异动榜单"


TELEGRAM_MAX = 4096
TELEGRAM_BUDGET = 3800  # leave headroom for Markdown + multibyte chars


def _pick_block(i: int, c: dict) -> list[str]:
    ticker = c["ticker"]
    # NaN-proof: _finite_or_none rejects NaN/inf (NaN is truthy, so a bare
    # `or` chain would happily format "$nan" — seen in the 2026-06-10 push).
    price = _finite_or_none(c.get("last_close")) or _finite_or_none(c.get("price")) or 0
    # Scraper-provided live change first; fall back to yfinance close-to-close
    # ret_1d so the percentage column is meaningful even when Finviz/Nasdaq are
    # blocked from CI.
    change = _finite_or_none(c.get("change")) or _finite_or_none(c.get("ret_1d")) or 0
    sector = c.get("sector", "")
    sec_tag = f" · {sector}" if sector and sector != "Unknown" else ""

    reason = make_reason(c)
    reason_safe = reason.replace("*", "").replace("_", "").replace("[", "").replace("]", "")

    price_str = f"${price:.2f}" if price > 0 else "$—"
    block = [f"{i}. *{ticker}*  {price_str}  ({change:+.1f}%){sec_tag}"]

    atr = c.get("atr", 0)
    if atr > 0 and price > 0:
        stop = price - 1.5 * atr
        target = price + 3.0 * atr
        stop_pct = (stop - price) / price * 100
        tgt_pct = (target - price) / price * 100
        block.append(
            f"   Entry {price:.2f} · Stop {stop:.2f} ({stop_pct:+.1f}%) · "
            f"Target {target:.2f} ({tgt_pct:+.1f}%)"
        )

    block.append(f"   {reason_safe}")

    st = signal_tag(c)
    if st:
        block.append(f"   {st}")

    ft = fund_tag(c)
    if ft:
        block.append(f"   {ft}")

    block.append(
        f"   📊 [TV](https://www.tradingview.com/chart/?symbol={ticker}) · "
        f"[Yahoo](https://finance.yahoo.com/quote/{ticker}) · "
        f"[News](https://stockanalysis.com/stocks/{ticker.lower()}/)"
    )
    return block


def format_messages(picks: list[dict], partial_reason: str = "") -> list[str]:
    """Split picks across multiple Telegram messages under TELEGRAM_BUDGET each.

    Continuation messages get a lightweight "(2/N)" header so the user can
    follow the sequence.
    """
    date_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    blocks = [_pick_block(i, c) for i, c in enumerate(picks, 1)]

    footer_lines = ["", "⚠️ 基于公开免费数据源，仅供参考，不构成投资建议。"]
    footer_chars = sum(len(s) + 1 for s in footer_lines)

    def header_for(idx: int, total: int) -> list[str]:
        if total == 1:
            h = [f"📈 *每日优选 Top {len(picks)}* — {date_str}", ""]
        elif idx == 1:
            h = [f"📈 *每日优选 Top {len(picks)}* (1/{total}) — {date_str}", ""]
        else:
            return [f"📈 *每日优选 ({idx}/{total})*", ""]
        if partial_reason and idx == 1:
            h.append(f"_{partial_reason}_")
            h.append("")
        return h

    # First pass: assign blocks to message slots using a rolling char count.
    placeholder_header = sum(len(s) + 1 for s in header_for(1, 9))
    slots: list[list[list[str]]] = [[]]
    cur_chars = placeholder_header
    for block in blocks:
        block_chars = sum(len(s) + 1 for s in block)
        if slots[-1] and cur_chars + block_chars + footer_chars > TELEGRAM_BUDGET:
            slots.append([])
            cur_chars = placeholder_header
        slots[-1].append(block)
        cur_chars += block_chars

    # Second pass: emit each slot with the correct (idx/total) header.
    total = len(slots)
    messages: list[str] = []
    for idx, slot in enumerate(slots, 1):
        lines = header_for(idx, total)
        for block in slot:
            lines.extend(block)
        if idx == total:
            lines.extend(footer_lines)
        messages.append("\n".join(lines))
    return messages


async def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            if r.status_code == 200 and r.json().get("ok"):
                return True
            print(f"Markdown send failed: {r.status_code} {r.text[:200]}")
            r2 = await c.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            )
            return r2.status_code == 200 and r2.json().get("ok", False)
        except Exception as e:
            print(f"Telegram exception: {e}")
            return False


# ============================================================================
# Rolling cohort tracking (v8, 2026-05-27) — see daily.MD for design.
#
# Each weekday the picker pushes a FRESH Top-N to Telegram (daily-fresh, like
# v6). That day's batch is recorded as a "cohort" keyed by pick date, then
# tracked forward TRACKING_WINDOW trading days:
#   entry = open(pick_date)              (lockable once that day has traded)
#   closes = close(pick_date .. +window) (capped at TRACKING_WINDOW)
#   status flips to TARGET_HIT / STOPPED when a close crosses the level
#   cohort retires after TRACKING_WINDOW trading dates elapse
#
# Each run RECOMPUTES every active cohort from a single yfinance history pull,
# so it's idempotent and self-heals across missed cron ticks. State lives in
# tracking/cohorts.json; tracking/equity.json + docs/index.html (Chart.js NAV
# curve) are regenerated each run for GitHub Pages.
# ============================================================================


def _cohorts_path() -> str:
    os.makedirs(TRACKING_DIR, exist_ok=True)
    return os.path.join(TRACKING_DIR, "cohorts.json")


def _equity_path() -> str:
    os.makedirs(TRACKING_DIR, exist_ok=True)
    return os.path.join(TRACKING_DIR, "equity.json")


def load_cohorts() -> dict:
    path = _cohorts_path()
    if not os.path.exists(path):
        return {"updated_at": None, "tracking_window": TRACKING_WINDOW, "cohorts": {}}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  Cohorts load failed: {e}")
        return {"updated_at": None, "tracking_window": TRACKING_WINDOW, "cohorts": {}}


def save_cohorts(store: dict) -> None:
    store["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(_cohorts_path(), "w") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  Cohorts save failed: {e}")


def _finite_or_none(x):
    """Coerce NaN/inf/None to None so it never lands in JSON or the dashboard.
    (yfinance occasionally returns NaN for a ticker's last_close — e.g. META on
    2026-06-05 — and Python's json.dump writes a literal `NaN`, invalid JSON.)"""
    try:
        return float(x) if x is not None and math.isfinite(float(x)) else None
    except (TypeError, ValueError):
        return None


def build_cohort(pick_date: date, picks: list[dict]) -> dict:
    """Convert in-memory picks into a JSON-safe cohort record (entries=null)."""
    return {
        "pick_date": pick_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries_locked": False,
        "retired": False,
        "picks": [
            {
                "rank": i,
                "ticker": p["ticker"],
                "sector": p.get("sector", "Unknown"),
                "score": p.get("score"),
                "components": {
                    "heat": p.get("heat_score", 0),
                    "fund": p.get("fund_score", 0),
                    "mom": p.get("mom_score", 0),
                    "tech": p.get("tech_score", 0),
                },
                "screen_close": _finite_or_none(p.get("last_close")),  # close the screen ranked on
                "entry_atr": p.get("atr"),
                "entry_price": None,   # locked next run = open(pick_date)
                "stop_price": None,
                "target_price": None,
                "status": "PENDING",
                "exit_price": None,
                "exit_date": None,
                "closes": [],          # [{date, close}], pick_date .. +window
            }
            for i, p in enumerate(picks, 1)
        ],
    }


def fetch_ohlc_since(tickers: list[str], start: date) -> dict[str, dict[str, dict]]:
    """Bulk yfinance pull. Returns {ticker: {date_iso: {"open":, "close":}}}."""
    import yfinance as yf

    end = datetime.now(timezone.utc).astimezone().date() + timedelta(days=1)
    try:
        df = yf.download(
            tickers=tickers,
            start=start.isoformat(),
            end=end.isoformat(),
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=False,
        )
    except Exception as e:
        print(f"  yf OHLC download failed: {e}")
        return {}

    out: dict[str, dict[str, dict]] = {}
    for t in tickers:
        try:
            td = df[t] if len(tickers) > 1 else df
            if td is None or td.empty:
                continue
            series: dict[str, dict] = {}
            for idx, row in td.iterrows():
                o, c = row.get("Open"), row.get("Close")
                if c != c:  # NaN close → skip
                    continue
                series[idx.date().isoformat()] = {
                    "open": float(o) if o == o else None,
                    "close": float(c),
                }
            if series:
                out[t] = series
        except Exception as e:
            print(f"  fetch_ohlc_since[{t}]: {e}")
    return out


def update_all_cohorts(store: dict) -> None:
    """Recompute every non-retired cohort from one yfinance history pull.

    Idempotent + gap-tolerant: re-derives entry, closes, status from scratch
    each run, so a missed cron tick just self-corrects next time.
    """
    today = datetime.now(timezone.utc).astimezone().date()
    active = {d: c for d, c in store["cohorts"].items() if not c.get("retired")}
    if not active:
        print("  No active cohorts to update")
        return

    all_tickers = sorted({p["ticker"] for c in active.values() for p in c["picks"]})
    earliest = min(date.fromisoformat(d) for d in active)
    print(f"  Updating {len(active)} cohorts, {len(all_tickers)} tickers since {earliest}")
    hist = fetch_ohlc_since(all_tickers, earliest)

    for d_iso, c in active.items():
        pick_date = date.fromisoformat(d_iso)
        # Settled trading dates since pick_date (exclude today's still-forming
        # bar) — used for entry lock, exit detection, and retire counting.
        settled_seen = 0
        for p in c["picks"]:
            series = hist.get(p["ticker"], {})
            dates = sorted(dt for dt in series if date.fromisoformat(dt) >= pick_date)
            settled = [dt for dt in dates if date.fromisoformat(dt) < today]
            settled_seen = max(settled_seen, len(settled))

            # Entry = open(pick_date), RECOMPUTED each run (not frozen) and only
            # from a SETTLED bar (pick_date strictly before today). Rationale:
            # pre-settlement, Yahoo returns the prior session's bar relabeled as
            # today, so a same-day lock grabbed the wrong day's open and froze it
            # (the 2026-06-15 cohort fossils: entry = the prior Friday's open).
            # Recomputing from the settled bar self-heals those, and a same-day
            # cohort stays PENDING until its open is final next session.
            entry = stop = target = None
            bar = series.get(pick_date.isoformat())
            o = bar.get("open") if bar else None
            if pick_date < today and o and o > 0:
                entry = round(o, 2)
                atr = p.get("entry_atr") or 0
                if atr > 0:
                    stop = round(entry - ATR_STOP_MULT * atr, 2)
                    target = round(entry + ATR_TARGET_MULT * atr, 2)
            p["entry_price"] = entry
            p["stop_price"] = stop
            p["target_price"] = target

            # Rebuild closes. Record every close (incl. today's live bar for
            # intraday P/L display), but only DETECT target/stop crossings on
            # settled closes so an intraday spike can't falsely mark an exit.
            closes: list[dict] = []
            status = "HOLD" if entry else "PENDING"
            exit_price = exit_date = None
            for dt in dates[:TRACKING_WINDOW]:
                cl = series[dt].get("close")
                if cl is None:
                    continue
                closes.append({"date": dt, "close": round(cl, 2)})
                if status != "HOLD" or date.fromisoformat(dt) >= today:
                    continue  # don't trigger exits on the unsettled live bar
                if target and cl >= target:
                    status, exit_price, exit_date = "TARGET_HIT", round(cl, 2), dt
                    break
                if stop and cl <= stop:
                    status, exit_price, exit_date = "STOPPED", round(cl, 2), dt
                    break
            p["closes"] = closes
            p["status"] = status
            p["exit_price"] = exit_price
            p["exit_date"] = exit_date

        c["entries_locked"] = all(p.get("entry_price") for p in c["picks"])
        if settled_seen >= TRACKING_WINDOW:
            c["retired"] = True
            print(f"  Cohort {d_iso} retired ({trading_dates_seen} trading days elapsed)")


# ----- Equity curve + dashboard --------------------------------------------


def compute_equity(store: dict) -> dict:
    """Daily-rebalanced equal-weight NAV across all positions in their window.

    For each position the price path is [(pick_date, entry_open), (date, close)...].
    Day-over-day returns are attributed to the later date; each trading day's
    return = mean of every active position's return that day; NAV compounds
    from 1.0. This is a standard equal-weight strategy NAV.
    """
    from collections import defaultdict

    returns_by_date: dict[str, list[float]] = defaultdict(list)
    positions = 0
    for c in store["cohorts"].values():
        for p in c["picks"]:
            entry = p.get("entry_price")
            closes = p.get("closes") or []
            if not entry or not closes:
                continue
            positions += 1
            # path: entry(open) then each close; consecutive ratio returns
            path = [(c["pick_date"], entry)] + [(d["date"], d["close"]) for d in closes]
            for (_, prev_px), (cur_date, cur_px) in zip(path, path[1:]):
                if prev_px and prev_px > 0:
                    returns_by_date[cur_date].append(cur_px / prev_px - 1)

    series = []
    nav = 1.0
    for d in sorted(returns_by_date):
        rets = returns_by_date[d]
        daily = sum(rets) / len(rets) if rets else 0.0
        nav *= 1 + daily
        series.append(
            {
                "date": d,
                "nav": round(nav, 4),
                "daily_return_pct": round(daily * 100, 3),
                "positions": len(rets),
            }
        )

    last = series[-1] if series else None
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "method": "daily-rebalanced equal-weight NAV, "
        f"{TRACKING_WINDOW}-trading-day hold per cohort",
        "total_positions_tracked": positions,
        "latest_nav": last["nav"] if last else 1.0,
        "series": series,
    }


def _cohort_summary(store: dict) -> list[dict]:
    """Per-cohort roll-up: counts + avg current/exit P/L. Newest first."""
    rows = []
    for d_iso in sorted(store["cohorts"], reverse=True):
        c = store["cohorts"][d_iso]
        hit = stop = hold = pending = 0
        pnls: list[float] = []
        for p in c["picks"]:
            s = p.get("status")
            entry = p.get("entry_price")
            if s == "TARGET_HIT":
                hit += 1
            elif s == "STOPPED":
                stop += 1
            elif s == "HOLD":
                hold += 1
            elif s == "PENDING":
                pending += 1
            if entry:
                ref = p.get("exit_price")
                if ref is None and p.get("closes"):
                    ref = p["closes"][-1]["close"]
                if ref:
                    pnls.append((ref / entry - 1) * 100)
        rows.append(
            {
                "pick_date": d_iso,
                "retired": c.get("retired", False),
                "n": len(c["picks"]),
                "target_hit": hit,
                "stopped": stop,
                "holding": hold,
                "pending": pending,
                "avg_pnl_pct": round(sum(pnls) / len(pnls), 2) if pnls else None,
            }
        )
    return rows


def _pick_detail_rows_html(picks: list[dict]) -> str:
    """Per-pick rows for a cohort's expandable detail table."""
    tag_map = {
        "HOLD": ("持有", ""),
        "TARGET_HIT": ("🎯目标", "hit"),
        "STOPPED": ("🛑止损", "stop"),
        "PENDING": ("待锁", ""),
    }
    out = []
    for p in sorted(picks, key=lambda x: x.get("rank", 999)):
        status = p.get("status", "PENDING")
        entry = p.get("entry_price")
        if status in ("TARGET_HIT", "STOPPED"):
            latest = p.get("exit_price")
        elif p.get("closes"):
            latest = p["closes"][-1]["close"]
        else:
            latest = None
        pnl = ((latest / entry - 1) * 100) if (entry and latest) else None
        label, cls = tag_map.get(status, (status, ""))
        pnl_cls = "pos" if (pnl or 0) >= 0 else "neg"
        # PENDING picks have no locked entry yet (locks next session); show the
        # screen-close as a muted reference so the row isn't all dashes.
        sc = _finite_or_none(p.get("screen_close"))
        if status == "PENDING" and latest is None and sc:
            price_cell = f"<td class='muted'>${sc:.2f}<span class='ref'>参考</span></td>"
        else:
            price_cell = f"<td>{('$%.2f' % latest) if latest else '—'}</td>"
        out.append(
            f"<tr><td>{p.get('rank','')}</td><td>{p['ticker']}</td>"
            f"<td>{p.get('sector','')}</td><td class='{cls}'>{label}</td>"
            f"<td>{('$%.2f' % entry) if entry else '—'}</td>"
            f"{price_cell}"
            f"<td class='{pnl_cls}'>{('%+.1f%%' % pnl) if pnl is not None else '—'}</td>"
            f"<td>{('$%.2f' % p['stop_price']) if p.get('stop_price') else '—'}</td>"
            f"<td>{('$%.2f' % p['target_price']) if p.get('target_price') else '—'}</td></tr>"
        )
    return "".join(out)


def generate_dashboard_html(store: dict, equity: dict) -> str:
    """Self-contained HTML (Chart.js via CDN): NAV curve + expandable cohorts."""
    summary = {r["pick_date"]: r for r in _cohort_summary(store)}
    labels = [s["date"] for s in equity["series"]]
    navs = [s["nav"] for s in equity["series"]]
    updated = equity["updated_at"]
    latest_nav = equity["latest_nav"]
    total_ret = (latest_nav - 1) * 100

    row_parts = []
    for idx, d_iso in enumerate(sorted(store["cohorts"], reverse=True)):
        r = summary.get(d_iso)
        if not r:
            continue
        cid = f"c{idx}"
        avg = r["avg_pnl_pct"]
        row_parts.append(
            f"<tr class='ch' onclick=\"tg('{cid}')\">"
            f"<td>▸ {r['pick_date']}</td><td>{r['n']}</td>"
            f"<td class='hit'>{r['target_hit']}</td>"
            f"<td class='stop'>{r['stopped']}</td>"
            f"<td>{r['holding']}</td><td>{r['pending']}</td>"
            f"<td class='{'pos' if (avg or 0) >= 0 else 'neg'}'>"
            f"{avg if avg is not None else '—'}</td>"
            f"<td>{'✓' if r['retired'] else ''}</td></tr>"
        )
        detail = _pick_detail_rows_html(store["cohorts"][d_iso]["picks"])
        row_parts.append(
            f"<tr id='{cid}' class='dt' style='display:none'><td colspan='8'>"
            f"<table class='inner'><tr><th>#</th><th>票</th><th>行业</th><th>状态</th>"
            f"<th>入场</th><th>现价</th><th>P/L%</th><th>止损</th><th>目标</th></tr>"
            f"{detail}</table></td></tr>"
        )
    rows_html = "\n".join(row_parts)

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta http-equiv="refresh" content="600">
<title>每日选股 — 滚动追踪</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 16px;
         background: #0f1115; color: #e6e6e6; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .sub {{ color: #8a8f98; font-size: 12px; margin-bottom: 16px; }}
  .nav-big {{ font-size: 28px; font-weight: 700; }}
  .pos {{ color: #3fb950; }} .neg {{ color: #f85149; }}
  .hit {{ color: #3fb950; }} .stop {{ color: #f85149; }}
  canvas {{ background: #161b22; border-radius: 8px; padding: 8px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 6px 8px; text-align: right; border-bottom: 1px solid #21262d; }}
  th:first-child, td:first-child {{ text-align: left; }}
  th {{ color: #8a8f98; font-weight: 600; }}
  tr.ch {{ cursor: pointer; }}
  tr.ch:hover {{ background: #161b22; }}
  tr.dt > td {{ padding: 0 0 0 12px; }}
  table.inner {{ font-size: 12px; margin: 4px 0 10px; }}
  table.inner th {{ color: #6e7681; font-weight: 500; }}
  table.inner td, table.inner th {{ padding: 3px 6px; border-bottom: 1px solid #1a1f26; }}
  .muted {{ color: #6e7681; }}
  .ref {{ font-size: 10px; color: #565d66; margin-left: 4px; }}
</style>
</head>
<body>
  <h1>每日选股 · 滚动 Cohort 追踪</h1>
  <div class="sub">每日推送一批，跟踪 {TRACKING_WINDOW} 个交易日 · 更新 {updated}</div>
  <div class="nav-big {'pos' if total_ret >= 0 else 'neg'}">
    NAV {latest_nav:.4f} <span style="font-size:14px">({total_ret:+.2f}%)</span>
  </div>
  <div class="sub">{equity['method']} · 累计跟踪 {equity['total_positions_tracked']} 个持仓</div>
  <canvas id="nav" height="120"></canvas>
  <h1>各 Cohort 战绩 <span style="font-size:12px;color:#8a8f98">（点行展开 33 只明细）</span></h1>
  <table>
    <tr><th>推送日</th><th>只数</th><th>🎯目标</th><th>🛑止损</th>
        <th>持有</th><th>待锁</th><th>均 P/L%</th><th>退休</th></tr>
    {rows_html}
  </table>
  <script>
    function tg(id) {{
      var e = document.getElementById(id);
      e.style.display = (e.style.display === 'none') ? 'table-row' : 'none';
    }}
    new Chart(document.getElementById('nav'), {{
      type: 'line',
      data: {{
        labels: {json.dumps(labels)},
        datasets: [{{
          label: 'NAV (等权日度再平衡)',
          data: {json.dumps(navs)},
          borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,.1)',
          fill: true, tension: .25, pointRadius: 2,
        }}]
      }},
      options: {{
        plugins: {{ legend: {{ labels: {{ color: '#e6e6e6' }} }} }},
        scales: {{
          x: {{ ticks: {{ color: '#8a8f98' }}, grid: {{ color: '#21262d' }} }},
          y: {{ ticks: {{ color: '#8a8f98' }}, grid: {{ color: '#21262d' }} }}
        }}
      }}
    }});
  </script>
</body>
</html>
"""


def write_dashboard(store: dict, equity: dict) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)
    try:
        with open(os.path.join(DOCS_DIR, "index.html"), "w") as f:
            f.write(generate_dashboard_html(store, equity))
    except Exception as e:
        print(f"  Dashboard write failed: {e}")


async def _generate_picks_pipeline() -> tuple[list[dict], str]:
    """Full picker pipeline: fetch → score → enrich → filter → boost → diversify.

    Returns (picks, partial_reason). Used by GENERATE mode of the weekly tracker.
    """
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        print("Fetching Finviz + Nasdaq pre + StockTwits trending...")
        finviz_tasks = [fetch_finviz_screener(client, n, q) for n, q in FINVIZ_SCREENERS]
        results = await asyncio.gather(
            *finviz_tasks,
            fetch_nasdaq_premarket(client),
            fetch_stocktwits_trending(client),
        )
        *finviz_results, nasdaq_rows, st_trending = results

        all_rows: list[dict] = []
        for lst in finviz_results:
            all_rows.extend(lst)
        all_rows.extend(nasdaq_rows)

        print(
            f"  Total rows: {len(all_rows)}, Nasdaq pre: {len(nasdaq_rows)}, "
            f"ST trending: {len(st_trending)}"
        )
        agg = aggregate(all_rows)
        print(f"  Unique candidates: {len(agg)}")

        used_fallback = False
        fallback_universe: list[str] = []
        fallback_label = ""
        if len(agg) < FALLBACK_MIN:
            fallback_universe, fallback_label = await load_dynamic_universe(client)
            print(
                f"  Candidate count < {FALLBACK_MIN}; injecting {fallback_label} "
                f"({len(fallback_universe)} tickers)"
            )
            inject_fallback_universe(agg, fallback_universe)
            used_fallback = True
            print(f"  Candidates after fallback: {len(agg)}")

        if agg:
            print("Fetching Reddit mentions...")
            reddit_counts = await fetch_reddit_mentions(client, set(agg.keys()))
            print(f"  Reddit covered: {len(reddit_counts)}")
        else:
            reddit_counts = {}

    ranked = score(agg, st_trending, reddit_counts)

    if not ranked:
        return [], "⚠️ 今日未能从任何免费数据源挑选出股票"

    effective_pool = (
        max(ENRICH_POOL, len(fallback_universe)) if used_fallback else ENRICH_POOL
    )
    enrich_tickers = [c["ticker"] for c in ranked[:effective_pool]]
    print(f"Enriching top {len(enrich_tickers)} via yfinance...")
    enrichments = await enrich_candidates(enrich_tickers)
    print(f"  Enrichment covered: {len(enrichments)}/{len(enrich_tickers)}")

    filtered = hard_filter(ranked[:effective_pool], enrichments)
    print(f"  After hard filter: {len(filtered)}")

    boosted = apply_boosts(filtered)
    picks = pick_diverse(boosted, TOP_N, MAX_PER_SECTOR)

    print(f"\n=== Top {len(picks)} ===")
    for i, p in enumerate(picks, 1):
        heat = p.get("heat_score", 0)
        fund = p.get("fund_score", 0)
        mom = p.get("mom_score", 0)
        tech = p.get("tech_score", 0)
        disp_change = p.get("change") or p.get("ret_1d") or 0
        print(
            f"{i:2d}. {p['ticker']:6s} ${p.get('last_close') or p['price']:7.2f}  "
            f"{disp_change:+5.1f}%  score={p['score']:5.1f} "
            f"(h{heat:.0f}+f{fund:+.0f}+m{mom:+.0f}+t{tech:+.0f})  "
            f"sec={p.get('sector','?'):15s}  "
            f"r5={p.get('ret_5d',0):+5.1f}% vr={p.get('vol_ratio',0):.1f} "
            f"52w={p.get('dist_52w',0):.2f} ma200={p.get('ma200_pos',0):.2f} "
            f"rsi={p.get('rsi14',0):.0f} macd={p.get('macd_state',0):+d}"
        )

    run_date = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    save_run_log(run_date, picks, len(filtered), len(enrichments))

    partial = ""
    if used_fallback:
        partial = (
            f"⚠️ 免费数据源返回空，启用 {fallback_label} "
            f"（{len(fallback_universe)} 只）按基本面 + 动量 + 技术筛选"
        )
    elif len(picks) < TOP_N:
        partial = f"今日受数据源限制，仅筛选到 {len(picks)} 只"

    return picks, partial


async def run() -> int:
    """v8 entry: push fresh daily picks + roll the cohort tracker + dashboard."""
    print(f"Starting picker (v8 daily + cohort tracking) — {datetime.now(timezone.utc).isoformat()}")
    today = datetime.now(timezone.utc).astimezone().date()
    today_iso = today.isoformat()
    today_utc = datetime.now(timezone.utc).date().isoformat()

    # ---- 0a. Weekend guard --------------------------------------------------
    # Never generate on Sat/Sun. The CF cron's weekday bug (1-5 = Sun-Thu under
    # CF's convention) currently fires this on Sundays; until the 2-6 redeploy
    # lands, this prevents a junk weekend cohort (market never opens → entry can
    # never lock → 33 forever-PENDING rows, as happened with the 5/31 cohort)
    # and a pointless Sunday Telegram push. Belt-and-suspenders after redeploy.
    if today.weekday() >= 5:  # 5=Sat, 6=Sun
        print(f"  {today_iso} is a weekend (market closed); skipping generation")
        return 0

    # ---- 0b. Same-day idempotency guard -------------------------------------
    # Generation can now be triggered by multiple independent sources (CF Worker
    # cron + GitHub-native schedule backup, added 2026-06-05 after CF dropped
    # all ticks on 3 straight Fridays). If today's cohort was already generated
    # today, this is a duplicate trigger: skip pick-gen + Telegram and just
    # refresh tracking, so a late GH-schedule run never double-sends Telegram.
    store = load_cohorts()
    existing = store["cohorts"].get(today_iso)
    if existing and (existing.get("generated_at", "")[:10] in (today_iso, today_utc)):
        print(f"  Cohort {today_iso} already generated today; skip pick-gen + Telegram, refresh only")
        refresh_tracking(store)
        return 0

    # ---- 1. Generate today's fresh Top-N (daily-fresh Telegram, v6 behavior) ----
    picks, partial = await _generate_picks_pipeline()

    telegram_ok = True
    if not picks:
        await send_telegram(
            f"⚠️ 今日候选经筛选后无合格股票。\n时间：{datetime.now(timezone.utc).isoformat()}\n{partial}"
        )
        telegram_ok = False
    else:
        msgs = format_messages(picks, partial)
        for i, m in enumerate(msgs, 1):
            ok = await send_telegram(m)
            print(f"Telegram send {i}/{len(msgs)} ({len(m)} chars): {'OK' if ok else 'FAILED'}")
            telegram_ok = telegram_ok and ok

    # ---- 2. Record today's cohort (store already loaded in step 0) ----
    if picks:
        if today_iso in store["cohorts"]:
            print(f"  Cohort {today_iso} already exists; overwriting with fresh picks")
        store["cohorts"][today_iso] = build_cohort(today, picks)
        print(f"  Recorded cohort {today_iso} ({len(picks)} picks)")

    # ---- 3-4. Roll cohorts + recompute equity + regenerate dashboard ----
    refresh_tracking(store)

    return 0 if telegram_ok else 1


def refresh_tracking(store: dict) -> dict:
    """Roll active cohorts forward, recompute equity, regenerate dashboard.

    Shared by the full daily run() and the intraday refresh_only() path —
    update_all_cohorts is idempotent, so calling it intraday just re-fetches
    the latest prices (locking entries once a pick_date has opened) without
    touching pick generation or Telegram.
    """
    update_all_cohorts(store)
    save_cohorts(store)

    equity = compute_equity(store)
    try:
        with open(_equity_path(), "w") as f:
            json.dump(equity, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  Equity save failed: {e}")
    write_dashboard(store, equity)
    print(
        f"  Equity NAV={equity['latest_nav']:.4f} "
        f"({(equity['latest_nav']-1)*100:+.2f}%), "
        f"{len(equity['series'])} days, "
        f"{equity['total_positions_tracked']} positions tracked"
    )
    return equity


def refresh_only() -> int:
    """Intraday refresh: re-pull prices for active cohorts + rebuild dashboard.

    No pick generation, no Telegram. Triggered every 15 min during market
    hours so the freshest cohort locks its entry once it opens and live P/L
    flows through the dashboard.
    """
    print(f"Refreshing cohort tracking (no picks/Telegram) — {datetime.now(timezone.utc).isoformat()}")
    store = load_cohorts()
    if not store["cohorts"]:
        print("  No cohorts yet; nothing to refresh")
        return 0
    refresh_tracking(store)
    return 0


if __name__ == "__main__":
    mode = os.getenv("MODE", "daily").strip().lower()
    if mode == "refresh":
        sys.exit(refresh_only())
    sys.exit(asyncio.run(run()))
