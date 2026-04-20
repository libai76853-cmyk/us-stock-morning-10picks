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
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TOP_N = int(os.getenv("TOP_N", "10"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "5"))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP_M", "500")) * 1e6
MIN_DOLLAR_VOL = float(os.getenv("MIN_DOLLAR_VOL_M", "5")) * 1e6
MAX_PER_SECTOR = int(os.getenv("MAX_PER_SECTOR", "2"))
ENRICH_POOL = int(os.getenv("ENRICH_POOL", "50"))
CACHE_DIR = os.getenv("CACHE_DIR", "cache")

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
            hist = t.history(period="1mo", auto_adjust=False)
            if hist.empty or len(hist) < 15:
                return None
            high, low, close = hist["High"], hist["Low"], hist["Close"]
            prev = close.shift(1)
            tr = pd.concat(
                [high - low, (high - prev).abs(), (low - prev).abs()], axis=1
            ).max(axis=1)
            atr_val = tr.rolling(14).mean().iloc[-1]
            return {
                "ticker": ticker,
                "market_cap": float(info.get("marketCap") or 0),
                "avg_volume": float(
                    info.get("averageVolume10days") or info.get("averageVolume") or 0
                ),
                "sector": info.get("sector") or "Unknown",
                "last_close": float(close.iloc[-1]),
                "atr": float(atr_val) if atr_val == atr_val else 0.0,
                "trailing_pe": float(info.get("trailingPE") or 0),
                "forward_pe": float(info.get("forwardPE") or 0),
                "peg": float(info.get("pegRatio") or info.get("trailingPegRatio") or 0),
                "roe": float(info.get("returnOnEquity") or 0),
                "revenue_growth": float(info.get("revenueGrowth") or 0),
                "earnings_growth_q": float(info.get("earningsQuarterlyGrowth") or 0),
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


def apply_fundamental_boost(candidates: list[dict]) -> list[dict]:
    for c in candidates:
        c["fund_score"] = fundamental_score(c)
        c["score"] += c["fund_score"]
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def fund_tag(c: dict) -> str:
    parts = []
    pe = c.get("forward_pe") or c.get("trailing_pe") or 0
    if pe > 0:
        parts.append(f"PE {pe:.0f}")
    peg = c.get("peg") or 0
    if peg > 0:
        parts.append(f"PEG {peg:.1f}")
    roe = c.get("roe") or 0
    if roe:
        parts.append(f"ROE {roe * 100:.0f}%")
    rg = c.get("revenue_growth") or 0
    if rg:
        parts.append(f"Rev {rg * 100:+.0f}%")
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


def format_message(picks: list[dict], partial_reason: str = "") -> str:
    date_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    lines = [f"📈 *每日优选 Top {len(picks)}* — {date_str}", ""]
    if partial_reason:
        lines.append(f"_{partial_reason}_")
        lines.append("")

    for i, c in enumerate(picks, 1):
        ticker = c["ticker"]
        price = c.get("last_close") or c["price"]
        change = c["change"]
        sector = c.get("sector", "")
        sec_tag = f" · {sector}" if sector and sector != "Unknown" else ""

        reason = make_reason(c)
        reason_safe = reason.replace("*", "").replace("_", "").replace("[", "").replace("]", "")
        lines.append(f"{i}. *{ticker}*  ${price:.2f}  ({change:+.1f}%){sec_tag}")

        atr = c.get("atr", 0)
        if atr > 0 and price > 0:
            stop = price - 1.5 * atr
            target = price + 3.0 * atr
            stop_pct = (stop - price) / price * 100
            tgt_pct = (target - price) / price * 100
            lines.append(
                f"   Entry {price:.2f} · Stop {stop:.2f} ({stop_pct:+.1f}%) · "
                f"Target {target:.2f} ({tgt_pct:+.1f}%)"
            )

        lines.append(f"   {reason_safe}")

        ft = fund_tag(c)
        if ft:
            lines.append(f"   {ft}")

        lines.append(
            f"   📊 [TV](https://www.tradingview.com/chart/?symbol={ticker}) · "
            f"[Yahoo](https://finance.yahoo.com/quote/{ticker}) · "
            f"[News](https://stockanalysis.com/stocks/{ticker.lower()}/)"
        )

    lines.append("")
    lines.append("⚠️ 基于公开免费数据源，仅供参考，不构成投资建议。")
    return "\n".join(lines)


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


async def run() -> int:
    print(f"Starting picker — {datetime.now(timezone.utc).isoformat()}")

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

        print(f"  Total rows: {len(all_rows)}, Nasdaq pre: {len(nasdaq_rows)}, ST trending: {len(st_trending)}")
        agg = aggregate(all_rows)
        print(f"  Unique candidates: {len(agg)}")

        if agg:
            print("Fetching Reddit mentions...")
            reddit_counts = await fetch_reddit_mentions(client, set(agg.keys()))
            print(f"  Reddit covered: {len(reddit_counts)}")
        else:
            reddit_counts = {}

    ranked = score(agg, st_trending, reddit_counts)

    if not ranked:
        fallback = (
            f"⚠️ 今日未能从任何免费数据源挑选出股票。\n"
            f"可能原因：周末休市 / Finviz/Nasdaq 被反爬 / 网络问题。\n"
            f"时间：{datetime.now(timezone.utc).isoformat()}"
        )
        await send_telegram(fallback)
        return 0

    enrich_tickers = [c["ticker"] for c in ranked[:ENRICH_POOL]]
    print(f"Enriching top {len(enrich_tickers)} via yfinance...")
    enrichments = await enrich_candidates(enrich_tickers)
    print(f"  Enrichment covered: {len(enrichments)}/{len(enrich_tickers)}")

    filtered = hard_filter(ranked[:ENRICH_POOL], enrichments)
    print(f"  After hard filter: {len(filtered)}")

    boosted = apply_fundamental_boost(filtered)
    picks = pick_diverse(boosted, TOP_N, MAX_PER_SECTOR)

    print(f"\n=== Top {len(picks)} ===")
    for i, p in enumerate(picks, 1):
        heat = p.get("heat_score", 0)
        fund = p.get("fund_score", 0)
        print(
            f"{i:2d}. {p['ticker']:6s} ${p.get('last_close') or p['price']:7.2f}  "
            f"{p['change']:+5.1f}%  score={p['score']:5.1f} "
            f"(heat {heat:.0f} + fund {fund:+.0f})  "
            f"sec={p.get('sector','?'):15s}  atr={p.get('atr',0):.2f}  "
            f"srcs={','.join(p['sources'])}"
        )

    run_date = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    save_run_log(run_date, picks, len(filtered), len(enrichments))

    partial = ""
    if len(picks) < TOP_N:
        partial = f"今日受数据源限制，仅筛选到 {len(picks)} 只"

    if not picks:
        fallback = (
            f"⚠️ 今日候选经硬过滤后无合格股票（市值/流动性/价格门槛）。\n"
            f"时间：{datetime.now(timezone.utc).isoformat()}"
        )
        await send_telegram(fallback)
        return 0

    msg = format_message(picks, partial)
    ok = await send_telegram(msg)
    print(f"Telegram send: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
