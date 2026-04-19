"""Free-data daily US stock picker — no API keys needed.

Sources: Finviz screeners, Nasdaq pre-market, StockTwits trending, Reddit WSB.
Delivery: Telegram.
"""
from __future__ import annotations
import asyncio
import html
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TOP_N = int(os.getenv("TOP_N", "10"))

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


def score_and_pick(
    agg: dict[str, dict],
    stocktwits_trending: set[str],
    reddit_mentions: dict[str, int],
) -> list[dict]:
    for t, c in agg.items():
        score = 0
        sources = c["sources"]
        score += len(sources) * 20

        if "Gap-Up" in sources:
            score += 15
        if "52周新高" in sources:
            score += 10
        if "异动量" in sources:
            score += 10
        if "Nasdaq盘前" in sources:
            score += 15

        score += min(abs(c["change"]), 30)

        if t in stocktwits_trending:
            score += 15
            c["trending"] = True
        else:
            c["trending"] = False

        r_count = reddit_mentions.get(t, 0)
        c["reddit"] = r_count
        if r_count > 0:
            import math
            score += min(math.log(r_count + 1) * 8, 20)

        c["score"] = score

    picks = [c for c in agg.values() if c["ticker"] and c["price"] >= 3]
    picks.sort(key=lambda x: x["score"], reverse=True)
    return picks[:TOP_N]


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
        price = c["price"]
        change = c["change"]
        reason = make_reason(c)
        reason_safe = reason.replace("*", "").replace("_", "").replace("[", "").replace("]", "")
        lines.append(f"{i}. *{ticker}*  ${price:.2f}  ({change:+.1f}%)")
        lines.append(f"   {reason_safe}")

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
        print("Fetching Finviz screeners + Nasdaq pre-market + StockTwits trending...")
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
            print("Fetching Reddit mentions (this takes ~30s)...")
            reddit_counts = await fetch_reddit_mentions(client, set(agg.keys()))
            print(f"  Reddit mentions covered: {len(reddit_counts)} tickers")
        else:
            reddit_counts = {}

    picks = score_and_pick(agg, st_trending, reddit_counts)
    print(f"\n=== Top {len(picks)} ===")
    for i, p in enumerate(picks, 1):
        print(
            f"{i:2d}. {p['ticker']:6s} ${p['price']:7.2f}  {p['change']:+5.1f}%  "
            f"score={p['score']:5.1f}  sources={','.join(p['sources'])}"
        )

    partial = ""
    if len(picks) < TOP_N:
        partial = f"今日数据获取受限，仅筛选到 {len(picks)} 只"

    if not picks:
        fallback = f"⚠️ 今日未能从任何免费数据源挑选出股票。\n可能原因：周末休市 / Finviz/Nasdaq 被反爬 / 网络问题。\n时间：{datetime.now(timezone.utc).isoformat()}"
        await send_telegram(fallback)
        return 0

    msg = format_message(picks, partial)
    ok = await send_telegram(msg)
    print(f"Telegram send: {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
