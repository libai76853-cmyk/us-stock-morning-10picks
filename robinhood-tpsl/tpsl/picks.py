"""读每日选股的跟踪文件（tracking/cohorts.json），取每只还在持有的票的止损价和目标价。"""

from __future__ import annotations

import json
from pathlib import Path


def load_picks(path: str | Path) -> dict[str, dict]:
    """返回 {代码: {stop_price, target_price, entry_price, pick_date}}。

    同一只票出现在多期里时取最新一期；已经止损或达标的（status 不是 HOLD）不算。
    读不到文件或格式不对就返回空，不影响别的功能。
    """
    try:
        store = json.loads(Path(path).read_text(encoding="utf-8"))
        cohorts = store["cohorts"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out: dict[str, dict] = {}
    for key in sorted(cohorts, reverse=True):  # 日期倒序，先见到的是最新的
        co = cohorts[key]
        if not isinstance(co, dict) or co.get("retired"):
            continue
        for p in co.get("picks") or []:
            sym = str(p.get("ticker") or "").upper()
            if not sym or sym in out or p.get("status") != "HOLD":
                continue
            if p.get("stop_price") is None or p.get("target_price") is None:
                continue
            out[sym] = {
                "stop_price": p["stop_price"],
                "target_price": p["target_price"],
                "entry_price": p.get("entry_price"),
                "pick_date": co.get("pick_date") or key,
            }
    return out
