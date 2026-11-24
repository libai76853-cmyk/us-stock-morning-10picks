"""照着用户确认过的计划下单。

规矩：
- 下单前用最新持仓和现价再校验一遍，行情变了就不下这只。
- 只卖不买；每只卖出的股数不超过此刻实际持有的整股数。
- 任何一步结果不明（超时、断线）就停在那里，不自动重试，避免重复挂单。
- 演练模式（dry_run）走完全部校验，但不向券商发任何下单或撤单请求。
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable

from .brokers.base import Broker, BrokerError, BrokerUncertain
from .config import Config
from .models import ZERO, Order, fmt, price_str, whole_shares
from .planner import Plan, PlanItem
from .watcher import Watcher

KIND_LABEL = {"stop": "止损单", "limit": "止盈限价单", "oco": "止盈/止损联动单", "market": "市价单"}


def execute(
    plan: Plan,
    broker: Broker,
    cfg: Config,
    watcher: Watcher | None = None,
    *,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    account = plan.request.account
    items = plan.actionable
    cfg.audit("execute_start", plan=plan.id, account=account, dry_run=dry_run,
              symbols=[i.symbol for i in items], backend=broker.name)

    results: list[dict] = []
    try:
        positions = {p.symbol: p for p in broker.positions(account)}
        quotes = broker.quotes([i.symbol for i in items]) if items else {}
        open_now = {o.id: o for o in broker.open_orders(account)}
    except BrokerError as e:
        cfg.audit("execute_abort", plan=plan.id, error=str(e))
        return {"ok": False, "dry_run": dry_run, "error": f"下单前取最新持仓/行情失败，一张都没下：{e}", "results": []}

    crashed = False
    for it in items:
        if crashed:
            res = {"symbol": it.symbol, "status": "failed", "steps": [],
                   "message": "前面出了意外错误，这只没有执行"}
        else:
            try:
                res = _run_item(it, plan, broker, cfg, watcher, positions, quotes, open_now, dry_run, sleep)
            except Exception as e:  # 程序自己的错：停下来，不带着未知状态继续下单
                crashed = True
                res = {"symbol": it.symbol, "status": "unknown", "steps": [],
                       "message": f"程序内部错误（{type(e).__name__}: {e}），这只可能只做了一半。"
                                  "请到「挂单」页核对"}
        cfg.audit("execute_item", plan=plan.id, **{k: v for k, v in res.items() if k != "steps"},
                  steps=res["steps"])
        results.append(res)

    for it in plan.items:
        if it.skipped:
            results.append({"symbol": it.symbol, "status": "skipped", "message": it.reason, "steps": []})

    done = sum(1 for r in results if r["status"] in ("done", "dry_run"))
    bad = sum(1 for r in results if r["status"] in ("failed", "partial", "unknown"))
    cfg.audit("execute_end", plan=plan.id, done=done, bad=bad)
    return {"ok": bad == 0, "dry_run": dry_run, "results": results,
            "summary": {"done": done, "bad": bad,
                        "skipped": sum(1 for r in results if r["status"] == "skipped")}}


def _run_item(it: PlanItem, plan: Plan, broker: Broker, cfg: Config, watcher: Watcher | None,
              positions: dict, quotes: dict, open_now: dict[str, Order], dry_run: bool,
              sleep: Callable[[float], None]) -> dict:
    steps: list[dict] = []

    def out(status: str, message: str) -> dict:
        return {"symbol": it.symbol, "status": status, "message": message, "steps": steps}

    def step(action: str, ok: bool, detail: str, order_id: str | None = None) -> None:
        steps.append({"action": action, "ok": ok, "detail": detail, "order_id": order_id})

    # ---- 复核 ----
    pos = positions.get(it.symbol)
    if pos is None or pos.quantity <= 0:
        return out("failed", "持仓已经不在，没有下单")
    q = quotes.get(it.symbol)
    last = q.last if q else None
    if last is None or last <= 0:
        return out("failed", "拿不到现价，没有下单")
    total = sum((o.quantity for o in it.orders), ZERO)
    cancelling = sum((open_now[o.id].quantity - open_now[o.id].filled_quantity
                      for o in it.cancels if o.id in open_now), ZERO)
    held = max(pos.held_for_sells, sum((o.quantity - o.filled_quantity for o in open_now.values()
                                        if o.symbol == it.symbol and o.side == "sell"), ZERO))
    other_held = max(held - cancelling, ZERO)  # 不在这次撤单范围内的卖单占着的股数
    free = whole_shares(pos.quantity - other_held)
    if total > free:
        return out("failed", f"可卖股数变了（现在可挂 {fmt(free)} 股，计划要挂 {fmt(total)} 股），没有下单，请重新预览")
    for o in it.orders:
        if o.kind in ("limit", "oco") and o.limit_price <= last:  # type: ignore[operator]
            return out("failed", f"现价 ${price_str(last)} 已到止盈价 ${price_str(o.limit_price)}，没有下单，请重新预览")
        if o.kind in ("stop", "oco") and o.stop_price >= last:  # type: ignore[operator]
            return out("failed", f"现价 ${price_str(last)} 已到止损价 ${price_str(o.stop_price)}，没有下单，请重新预览")

    if dry_run:
        for c in it.cancels:
            step("cancel", True, f"演练：会撤掉 {_describe(c)}", c.id)
        for o in it.orders:
            step("place", True, f"演练：会挂 {_describe_sell(o)}")
        if it.watch:
            step("watch", True, f"演练：会开启盯盘，到 ${it.watch['tp_price']} 止盈")
        return out("dry_run", "演练通过，未发出任何订单")

    # ---- 撤掉要替换的旧单 ----
    if it.cancels and watcher:
        watcher.disarm(account=plan.request.account, symbol=it.symbol, note="被新的止盈止损设置替换")
    for c in it.cancels:
        if c.id not in open_now:
            step("cancel", True, f"{_describe(c)} 已经不在，跳过", c.id)
            continue
        try:
            broker.cancel(c.account, c.id)
            closed = _await_closed(broker, c.account, c.id, sleep)
        except BrokerUncertain as e:
            step("cancel", False, f"撤 {_describe(c)} 结果不明：{e}", c.id)
            return out("unknown", "撤旧单结果不明，后面的单没有下。请到「挂单」页核对")
        except BrokerError as e:
            step("cancel", False, f"撤 {_describe(c)} 失败：{e}", c.id)
            return out("failed", "撤旧单失败，后面的单没有下")
        if closed is None or not closed.is_final:
            step("cancel", False, f"撤 {_describe(c)} 还没确认", c.id)
            return out("unknown", "撤旧单没有得到确认，后面的单没有下。稍后到「挂单」页核对再试")
        if closed.state == "filled":
            step("cancel", False, f"{_describe(c)} 在撤销前已成交", c.id)
            return out("failed", "旧单在撤销前成交了，持仓已变，没有下新单")
        step("cancel", True, f"已撤 {_describe(c)}", c.id)

    if it.cancels:
        try:
            now = next((p for p in broker.positions(plan.request.account) if p.symbol == it.symbol), None)
        except BrokerError as e:
            return out("failed", f"撤完旧单后取持仓失败，没有下新单：{e}")
        # 只看持仓数量有没有变少（旧单部分成交）；券商那边的占用数刚撤完单时可能还没更新
        if now is None or whole_shares(now.quantity - other_held) < total:
            return out("failed", "旧单已撤，但持仓比预览时少了（旧单可能已部分成交），没有下新单。"
                                 "这只现在没有挂单，请重新预览")

    # ---- 挂新单 ----
    placed: list[Order] = []
    for i, o in enumerate(it.orders):
        try:
            got = broker.place_sell(o)
        except BrokerUncertain as e:
            step("place", False, f"{_describe_sell(o)} 结果不明：{e}")
            return out("unknown", "下单请求发出但没有拿到结果，可能已挂上也可能没有。没有重试，请到「挂单」页核对")
        except BrokerError as e:
            step("place", False, f"{_describe_sell(o)} 被拒：{e}")
            if placed:
                return out("partial", f"前 {len(placed)} 张已挂上，这一张没挂上")
            msg = "没有挂上"
            if it.cancels:
                msg += "；注意旧的卖单已经撤掉，这只现在没有挂单"
            return out("failed", msg)
        if got.state == "rejected":
            step("place", False, f"{_describe_sell(o)} 被券商拒绝", got.id)
            if placed:
                return out("partial", f"前 {len(placed)} 张已挂上，这一张被拒")
            return out("failed", "被券商拒绝")
        placed.append(got)
        step("place", True, f"已挂 {_describe_sell(o)}", got.id)

    # ---- 盯盘 ----
    if it.watch and watcher and placed:
        w = watcher.arm(plan.request.account, it.symbol, Decimal(it.watch["quantity"]),
                        Decimal(it.watch["tp_price"]), Decimal(it.watch["sl_price"]),
                        plan.request.tif, placed[0].id)
        step("watch", True, f"已开启盯盘，到 ${it.watch['tp_price']} 自动改挂止盈", w.id)
    return out("done", "已挂好")


def cancel_orders(broker: Broker, cfg: Config, watcher: Watcher | None, account: str,
                  order_ids: list[str], *, dry_run: bool = False,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """撤指定的卖单。属于盯盘的单一并停止盯盘。"""
    try:
        open_now = {o.id: o for o in broker.open_orders(account)}
    except BrokerError as e:
        return {"ok": False, "error": f"取挂单失败：{e}", "results": []}
    results = []
    for oid in order_ids:
        o = open_now.get(oid)
        if o is None:
            results.append({"order_id": oid, "ok": True, "detail": "已经不在挂单里"})
            continue
        if dry_run:
            results.append({"order_id": oid, "ok": True, "detail": f"演练：会撤掉 {_describe(o)}"})
            continue
        if watcher:
            for w in watcher.list(account, active_only=True):
                if oid in (w.stop_order_id, w.tp_order_id):
                    watcher.disarm(watch_id=w.id, note="挂单被手动撤销，盯盘停止")
        try:
            broker.cancel(account, oid)
            closed = _await_closed(broker, account, oid, sleep)
            ok = closed is not None and closed.is_final
            detail = f"已撤 {_describe(o)}" if ok else f"撤 {_describe(o)} 还没确认，稍后刷新看看"
        except BrokerError as e:
            ok, detail = False, f"撤 {_describe(o)} 失败：{e}"
        cfg.audit("cancel", account=account, order_id=oid, ok=ok, detail=detail)
        results.append({"order_id": oid, "ok": ok, "detail": detail})
    return {"ok": all(r["ok"] for r in results), "dry_run": dry_run, "results": results}


def _await_closed(broker: Broker, account: str, order_id: str, sleep: Callable[[float], None],
                  tries: int = 10, gap: float = 0.6) -> Order | None:
    o = None
    for i in range(tries):
        o = broker.get_order(account, order_id)
        if o is not None and o.is_final:
            return o
        if i < tries - 1:
            sleep(gap)
    return o


def _describe(o) -> str:
    parts = [o.symbol, f"{fmt(o.quantity)} 股", KIND_LABEL.get(o.kind, o.kind)]
    if o.limit_price is not None:
        parts.append(f"限价 ${price_str(o.limit_price)}")
    if o.stop_price is not None:
        parts.append(f"止损 ${price_str(o.stop_price)}")
    return " ".join(parts)


_describe_sell = _describe
