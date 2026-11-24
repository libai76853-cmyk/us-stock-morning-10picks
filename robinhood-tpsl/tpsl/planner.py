"""把「止盈 +15% / 止损 -7%」这样的设置变成一张张具体的卖单。

只算、不下单。算出来的计划原样给用户预览，确认后由 executor 照着执行。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from .models import ZERO, Capabilities, Order, Position, SellOrder, dec, fmt, price_str, round_price, whole_shares

MODES = ("both", "sl", "tp")
BASES = ("cost", "last", "picks")
STRATEGIES = ("auto", "oco", "split", "watch")

HUNDRED = Decimal(100)
NEAR = Decimal("0.01")  # 离现价不到 1% 给提醒


class PlanError(ValueError):
    """请求本身不合法（参数错），整份计划都不生成。"""


@dataclass
class PlanRequest:
    account: str
    mode: str = "both"
    basis: str = "cost"
    tp_pct: Decimal = Decimal(15)
    sl_pct: Decimal = Decimal(7)
    both_strategy: str = "auto"
    split_tp_ratio: Decimal = Decimal("0.5")
    replace_existing: bool = False
    tif: str = "gtc"
    symbols: list[str] | None = None  # None = 全部持仓
    overrides: dict[str, dict] = field(default_factory=dict)  # symbol -> {tp_price, sl_price, qty}

    @staticmethod
    def from_json(d: dict) -> "PlanRequest":
        def need_dec(key: str, default: str) -> Decimal:
            v = dec(d.get(key, default))
            if v is None:
                raise PlanError(f"{key} 不是数字")
            return v

        syms = d.get("symbols")
        if syms is not None:
            if not isinstance(syms, list) or not all(isinstance(s, str) for s in syms):
                raise PlanError("symbols 应该是代码列表")
            syms = [s.strip().upper() for s in syms if s.strip()]
        ov = d.get("overrides") or {}
        if not isinstance(ov, dict):
            raise PlanError("overrides 格式不对")
        req = PlanRequest(
            account=str(d.get("account") or ""),
            mode=str(d.get("mode", "both")),
            basis=str(d.get("basis", "cost")),
            tp_pct=need_dec("tp_pct", "15"),
            sl_pct=need_dec("sl_pct", "7"),
            both_strategy=str(d.get("both_strategy", "auto")),
            split_tp_ratio=need_dec("split_tp_ratio", "0.5"),
            replace_existing=bool(d.get("replace_existing", False)),
            tif=str(d.get("tif", "gtc")),
            symbols=syms,
            overrides={str(k).upper(): v for k, v in ov.items() if isinstance(v, dict)},
        )
        req.check()
        return req

    def check(self) -> None:
        if not self.account:
            raise PlanError("没有选账户")
        if self.mode not in MODES:
            raise PlanError(f"mode 只能是 {MODES}")
        if self.basis not in BASES:
            raise PlanError(f"basis 只能是 {BASES}")
        if self.both_strategy not in STRATEGIES:
            raise PlanError(f"both_strategy 只能是 {STRATEGIES}")
        if self.tif not in ("gtc", "gfd"):
            raise PlanError("tif 只能是 gtc 或 gfd")
        if self.mode in ("both", "tp") and not (ZERO < self.tp_pct <= 500):
            raise PlanError("止盈百分比要在 0 到 500 之间")
        if self.mode in ("both", "sl") and not (ZERO < self.sl_pct <= 90):
            raise PlanError("止损百分比要在 0 到 90 之间")
        if not (Decimal("0.1") <= self.split_tp_ratio <= Decimal("0.9")):
            raise PlanError("拆分比例要在 0.1 到 0.9 之间")

    def to_json(self) -> dict:
        return {
            "account": self.account,
            "mode": self.mode,
            "basis": self.basis,
            "tp_pct": fmt(self.tp_pct),
            "sl_pct": fmt(self.sl_pct),
            "both_strategy": self.both_strategy,
            "split_tp_ratio": fmt(self.split_tp_ratio),
            "replace_existing": self.replace_existing,
            "tif": self.tif,
            "symbols": self.symbols,
            "overrides": self.overrides,
        }


@dataclass
class PlanItem:
    symbol: str
    position_qty: Decimal
    last: Decimal | None
    avg_cost: Decimal | None
    strategy: str = ""  # oco | split | watch | sl | tp
    quantity: Decimal = ZERO  # 这次要挂的总股数
    tp_price: Decimal | None = None
    sl_price: Decimal | None = None
    cancels: list[Order] = field(default_factory=list)
    orders: list[SellOrder] = field(default_factory=list)
    watch: dict | None = None  # {tp_price, sl_price, quantity}：由本机盯盘负责止盈
    skipped: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> "PlanItem":
        self.skipped, self.reason = True, reason
        self.orders, self.watch, self.cancels = [], None, []
        return self

    def to_json(self) -> dict:
        def pct(p: Decimal | None, ref: Decimal | None) -> float | None:
            if p is None or not ref:
                return None
            return float((p - ref) / ref * 100)

        def pnl(p: Decimal | None, leg: str) -> str | None:
            if p is None or self.avg_cost is None or not self.quantity:
                return None
            q = self.quantity
            if self.strategy == "split":  # 两条腿各管一部分股数
                q = sum((o.quantity for o in self.orders if o.kind == leg), ZERO)
            return fmt(((p - self.avg_cost) * q).quantize(Decimal("0.01")))

        return {
            "symbol": self.symbol,
            "position_qty": fmt(self.position_qty),
            "last": fmt(self.last),
            "avg_cost": fmt(self.avg_cost),
            "strategy": self.strategy,
            "quantity": fmt(self.quantity),
            "tp_price": fmt(self.tp_price),
            "sl_price": fmt(self.sl_price),
            "tp_vs_last_pct": pct(self.tp_price, self.last),
            "sl_vs_last_pct": pct(self.sl_price, self.last),
            "tp_vs_cost_pct": pct(self.tp_price, self.avg_cost),
            "sl_vs_cost_pct": pct(self.sl_price, self.avg_cost),
            "pnl_at_tp": pnl(self.tp_price, "limit"),
            "pnl_at_sl": pnl(self.sl_price, "stop"),
            "cancels": [o.to_json() for o in self.cancels],
            "orders": [o.to_json() for o in self.orders],
            "watch": self.watch,
            "skipped": self.skipped,
            "reason": self.reason,
            "warnings": self.warnings,
        }


@dataclass
class Plan:
    id: str
    request: PlanRequest
    items: list[PlanItem]

    @property
    def actionable(self) -> list[PlanItem]:
        return [i for i in self.items if not i.skipped]

    def to_json(self) -> dict:
        act = self.actionable
        return {
            "id": self.id,
            "request": self.request.to_json(),
            "items": [i.to_json() for i in self.items],
            "summary": {
                "actionable": len(act),
                "skipped": len(self.items) - len(act),
                "orders": sum(len(i.orders) for i in act),
                "cancels": sum(len(i.cancels) for i in act),
                "watches": sum(1 for i in act if i.watch),
            },
        }


def resolve_strategy(req: PlanRequest, caps: Capabilities, watcher_available: bool) -> str:
    """止盈止损都要时用哪种挂法。"""
    s = req.both_strategy
    if s == "auto":
        if caps.native_oco:
            return "oco"
        return "watch" if (watcher_available and caps.order_status) else "split"
    if s == "oco" and not caps.native_oco:
        raise PlanError("当前接口不支持止盈/止损联动单，请改用「盯盘」或「拆分」")
    if s == "watch" and not (watcher_available and caps.order_status):
        raise PlanError("当前接口查不了订单状态，不能用「盯盘」，请改用「拆分」")
    return s


def build_plan(
    req: PlanRequest,
    positions: list[Position],
    open_orders: list[Order],
    caps: Capabilities,
    *,
    picks: dict[str, dict] | None = None,
    watcher_available: bool = True,
) -> Plan:
    req.check()
    both = resolve_strategy(req, caps, watcher_available) if req.mode == "both" else ""
    by_symbol: dict[str, list[Order]] = {}
    for o in open_orders:
        if o.side == "sell" and o.is_open:
            by_symbol.setdefault(o.symbol, []).append(o)

    wanted = set(req.symbols) if req.symbols is not None else None
    items: list[PlanItem] = []
    seen: set[str] = set()
    for p in sorted(positions, key=lambda x: x.symbol):
        if wanted is not None and p.symbol not in wanted:
            continue
        seen.add(p.symbol)
        items.append(_plan_one(req, p, by_symbol.get(p.symbol, []), both, picks or {}))
    if wanted:
        for s in sorted(wanted - seen):
            items.append(PlanItem(s, ZERO, None, None).skip("这个账户里没有这只持仓"))
    return Plan(id=uuid.uuid4().hex, request=req, items=items)


def _plan_one(req: PlanRequest, p: Position, existing: list[Order], both: str, picks: dict[str, dict]) -> PlanItem:
    it = PlanItem(p.symbol, p.quantity, p.last_price, p.avg_cost)
    ov = req.overrides.get(p.symbol, {})
    want_tp = req.mode in ("both", "tp")
    want_sl = req.mode in ("both", "sl")

    if p.quantity <= 0:
        return it.skip("不是多头持仓")
    if p.last_price is None or p.last_price <= 0:
        return it.skip("拿不到现价，无法校验价位")
    last = p.last_price

    # ---- 可挂股数 ----
    if req.replace_existing:
        it.cancels = list(existing)
        sellable = whole_shares(p.quantity)
    else:
        held = max(p.held_for_sells, sum((o.quantity - o.filled_quantity for o in existing), ZERO))
        sellable = whole_shares(p.quantity - held)
        if sellable <= 0 and held > 0:
            return it.skip("股数已全部被现有卖单占用；勾选「替换已有卖单」可以撤掉重挂")
    if sellable <= 0:
        return it.skip("不足 1 整股，限价单和止损单都不支持零碎股")
    qty = sellable
    if ov.get("qty") is not None:
        q = dec(ov.get("qty"))
        if q is None or q <= 0 or q != whole_shares(q):
            return it.skip("自定数量必须是正整数股")
        if q > sellable:
            return it.skip(f"自定数量 {fmt(q)} 超过可挂股数 {fmt(sellable)}")
        qty = q
    it.quantity = qty
    if p.quantity != whole_shares(p.quantity) and qty == sellable:
        it.warnings.append(f"零碎股 {fmt(p.quantity - whole_shares(p.quantity))} 股不支持这类订单，没有包含在内")

    # ---- 价位 ----
    tp = dec(ov.get("tp_price")) if ov.get("tp_price") not in (None, "") else None
    sl = dec(ov.get("sl_price")) if ov.get("sl_price") not in (None, "") else None
    if ov.get("tp_price") not in (None, "") and tp is None:
        return it.skip("自定止盈价不是数字")
    if ov.get("sl_price") not in (None, "") and sl is None:
        return it.skip("自定止损价不是数字")

    need_ref = (want_tp and tp is None) or (want_sl and sl is None)
    if need_ref:
        if req.basis == "picks":
            pk = picks.get(p.symbol)
            if not pk:
                return it.skip("每日选股记录里没有这只，换成按成本价或现价")
            if want_tp and tp is None:
                tp = dec(pk.get("target_price"))
            if want_sl and sl is None:
                sl = dec(pk.get("stop_price"))
            if (want_tp and tp is None) or (want_sl and sl is None):
                return it.skip("选股记录里这只缺止损价或目标价")
        else:
            ref = p.avg_cost if req.basis == "cost" else last
            if ref is None or ref <= 0:
                return it.skip("没有成本价，换成按现价")
            if want_tp and tp is None:
                tp = ref * (1 + req.tp_pct / HUNDRED)
            if want_sl and sl is None:
                sl = ref * (1 - req.sl_pct / HUNDRED)

    if want_tp:
        tp = round_price(tp)  # type: ignore[arg-type]
        it.tp_price = tp
        if tp <= last:
            return it.skip(f"止盈价 ${price_str(tp)} 不高于现价 ${price_str(last)}，挂上会立刻成交；可改按现价计算")
        if (tp - last) / last < NEAR:
            it.warnings.append("止盈价离现价不到 1%")
    if want_sl:
        sl = round_price(sl)  # type: ignore[arg-type]
        it.sl_price = sl
        if sl <= 0:
            return it.skip("止损价算出来不是正数")
        if sl >= last:
            return it.skip(f"止损价 ${price_str(sl)} 不低于现价 ${price_str(last)}，挂上会立刻触发；可改按现价计算")
        if (last - sl) / last < NEAR:
            it.warnings.append("止损价离现价不到 1%，容易被正常波动触发")

    # ---- 订单 ----
    def order(kind: str, q: Decimal, lim: Decimal | None, stp: Decimal | None) -> SellOrder:
        return SellOrder(req.account, p.symbol, q, kind, lim, stp, req.tif, ref=uuid.uuid4().hex[:12])

    if req.mode == "sl":
        it.strategy = "sl"
        it.orders = [order("stop", qty, None, sl)]
    elif req.mode == "tp":
        it.strategy = "tp"
        it.orders = [order("limit", qty, tp, None)]
    elif both == "oco":
        it.strategy = "oco"
        it.orders = [order("oco", qty, tp, sl)]
    elif both == "watch":
        it.strategy = "watch"
        it.orders = [order("stop", qty, None, sl)]
        it.watch = {"tp_price": price_str(tp), "sl_price": price_str(sl), "quantity": fmt(qty)}
    else:  # split
        tp_qty = whole_shares(qty * req.split_tp_ratio)
        sl_qty = qty - tp_qty
        if tp_qty <= 0 or sl_qty <= 0:
            it.strategy = "sl"
            it.orders = [order("stop", qty, None, sl)]
            it.tp_price = None
            it.warnings.append("股数太少没法拆分，只挂了止损")
        else:
            it.strategy = "split"
            # 先挂止损：万一第二张没挂上，留下的是保护性的那张
            it.orders = [order("stop", sl_qty, None, sl), order("limit", tp_qty, tp, None)]
    for o in it.orders:
        o.validate()
    return it
