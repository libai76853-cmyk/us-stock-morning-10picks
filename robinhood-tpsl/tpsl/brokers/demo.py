"""模拟券商：全在内存里，不联网，不会下出任何真实订单。

默认后端。用来看页面、练手，也是测试用的券商。行为尽量照 Robinhood：
同一批股票不能同时被两张卖单占用，止损价不能高于现价，成交后持仓减少。
"""

from __future__ import annotations

import itertools
import random
import threading
from datetime import datetime, timezone
from decimal import Decimal

from ..models import ZERO, Account, Capabilities, Order, Position, Quote, SellOrder, round_price
from .base import Broker, BrokerError

_SEED = [
    # symbol, 股数, 成本, 现价
    ("NVDA", "40", "142.50", "168.30"),
    ("AAPL", "25", "231.10", "226.45"),
    ("MSFT", "12", "418.00", "447.80"),
    ("TSLA", "15", "352.75", "318.20"),
    ("CPAY", "8", "437.36", "441.90"),
    ("PLTR", "60.5", "71.20", "88.64"),
    ("SOFI", "300", "14.85", "13.92"),
]


class DemoBroker(Broker):
    name = "demo"
    label = "演示（模拟）"

    def __init__(self, native_oco: bool = True, drift: bool = False, seed: int = 7):
        self._lock = threading.RLock()
        self._native_oco = native_oco
        self._drift = drift
        self._rng = random.Random(seed)
        self._ids = itertools.count(1)
        self._orders: dict[str, Order] = {}
        self._prices: dict[str, Decimal] = {}
        self._pos: dict[str, dict[str, Position]] = {"DEMO-1": {}, "DEMO-2": {}}
        for sym, qty, cost, last in _SEED:
            self._pos["DEMO-1"][sym] = Position("DEMO-1", sym, Decimal(qty), Decimal(cost))
            self._prices[sym] = Decimal(last)
        self._pos["DEMO-2"]["SPY"] = Position("DEMO-2", "SPY", Decimal("10"), Decimal("601.20"))
        self._prices["SPY"] = Decimal("612.75")
        self.fail_next_place: str | None = None  # 测试用：让下一次下单失败
        self.freeze = False  # 测试用：暂停撮合，模拟「挂上了但还没成交」

    # ---- 测试 / 演示用的控制 ----
    def set_price(self, symbol: str, price: str | Decimal) -> None:
        with self._lock:
            self._prices[symbol] = Decimal(str(price))
            self._match()

    def set_position(self, account: str, symbol: str, qty: str, cost: str | None, last: str) -> None:
        with self._lock:
            self._pos.setdefault(account, {})[symbol] = Position(
                account, symbol, Decimal(qty), Decimal(cost) if cost else None
            )
            self._prices[symbol] = Decimal(last)

    # ---- Broker 接口 ----
    def capabilities(self) -> Capabilities:
        return Capabilities(native_oco=self._native_oco, notes=["模拟数据，不会下出真实订单"])

    def accounts(self) -> list[Account]:
        return [Account("DEMO-1", "演示账户"), Account("DEMO-2", "演示账户 2")]

    def positions(self, account: str) -> list[Position]:
        with self._lock:
            out = []
            for p in self._pos.get(account, {}).values():
                if p.quantity <= 0:
                    continue
                held = sum(
                    (o.quantity - o.filled_quantity for o in self._orders.values()
                     if o.is_open and o.account == account and o.symbol == p.symbol),
                    ZERO,
                )
                out.append(Position(account, p.symbol, p.quantity, p.avg_cost, held))
            return out

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        with self._lock:
            if self._drift:
                for s in list(self._prices):
                    step = Decimal(str(round(self._rng.uniform(-0.004, 0.004), 5)))
                    self._prices[s] = round_price(self._prices[s] * (1 + step))
                self._match()
            out = {}
            for s in symbols:
                if s in self._prices:
                    last = self._prices[s]
                    out[s] = Quote(s, last, round_price(last * Decimal("0.9995")), round_price(last * Decimal("1.0005")))
            return out

    def open_orders(self, account: str) -> list[Order]:
        with self._lock:
            return [self._copy(o) for o in self._orders.values() if o.is_open and o.account == account]

    def get_order(self, account: str, order_id: str) -> Order | None:
        with self._lock:
            o = self._orders.get(order_id)
            return self._copy(o) if o and o.account == account else None

    def place_sell(self, order: SellOrder) -> Order:
        order.validate()
        with self._lock:
            if self.fail_next_place:
                msg, self.fail_next_place = self.fail_next_place, None
                raise BrokerError(msg)
            pos = self._pos.get(order.account, {}).get(order.symbol)
            if not pos or pos.quantity <= 0:
                raise BrokerError(f"{order.symbol}: 没有持仓")
            held = sum(
                (o.quantity - o.filled_quantity for o in self._orders.values()
                 if o.is_open and o.account == order.account and o.symbol == order.symbol),
                ZERO,
            )
            if order.quantity > pos.quantity - held:
                raise BrokerError(
                    f"{order.symbol}: 可卖股数不足（持有 {pos.quantity}，已被卖单占用 {held}）"
                )
            last = self._prices[order.symbol]
            if order.kind in ("stop", "oco") and order.stop_price >= last:  # type: ignore[operator]
                raise BrokerError(f"{order.symbol}: 止损价 {order.stop_price} 不低于现价 {last}")
            if order.kind == "oco" and not self._native_oco:
                raise BrokerError("此账户不支持止盈/止损联动单")
            o = Order(
                id=f"demo-{next(self._ids):04d}",
                account=order.account,
                symbol=order.symbol,
                kind=order.kind,
                quantity=order.quantity,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                state="open",
                tif=order.tif,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            self._orders[o.id] = o
            self._match()
            return self._copy(o)

    def cancel(self, account: str, order_id: str) -> None:
        with self._lock:
            o = self._orders.get(order_id)
            if not o or o.account != account:
                raise BrokerError(f"找不到订单 {order_id}")
            if o.is_open:
                o.state = "cancelled"

    def status(self) -> dict:
        return {"ok": True, "detail": "模拟数据"}

    # ---- 撮合 ----
    def _match(self) -> None:
        if self.freeze:
            return
        for o in self._orders.values():
            if not o.is_open:
                continue
            last = self._prices.get(o.symbol)
            if last is None:
                continue
            hit = (
                o.kind == "market"
                or (o.kind in ("limit", "oco") and o.limit_price is not None and last >= o.limit_price)
                or (o.kind in ("stop", "oco") and o.stop_price is not None and last <= o.stop_price)
            )
            if hit:
                o.state = "filled"
                o.filled_quantity = o.quantity
                pos = self._pos[o.account][o.symbol]
                pos.quantity -= o.quantity

    @staticmethod
    def _copy(o: Order) -> Order:
        return Order(**{k: getattr(o, k) for k in o.__dataclass_fields__})
