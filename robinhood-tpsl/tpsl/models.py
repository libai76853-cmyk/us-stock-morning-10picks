"""数据结构。价格和数量一律用 Decimal，进出 JSON 时转成字符串。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal(0)


def dec(x: Any) -> Decimal | None:
    """宽松地转成 Decimal；空、非数字、NaN、无穷都返回 None。"""
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, Decimal):
        return x if x.is_finite() else None
    try:
        s = str(x).strip().replace(",", "").replace("$", "")
        if not s:
            return None
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def tick_for(price: Decimal) -> Decimal:
    """美股最小报价单位：1 美元及以上 0.01，1 美元以下 0.0001。"""
    return Decimal("0.01") if price >= 1 else Decimal("0.0001")


def round_price(price: Decimal) -> Decimal:
    return price.quantize(tick_for(price), rounding=ROUND_HALF_UP)


def whole_shares(q: Decimal) -> Decimal:
    return q.quantize(Decimal(1), rounding=ROUND_DOWN)


def fmt(d: Decimal | None) -> str | None:
    """Decimal -> 不带科学计数法、去掉多余 0 的字符串。"""
    if d is None:
        return None
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def price_str(d: Decimal | None) -> str | None:
    """给人看的价格：补齐小数位（1 美元以上两位，以下四位）。"""
    if d is None:
        return None
    return format(d.quantize(tick_for(d), rounding=ROUND_HALF_UP), "f")


@dataclass
class Account:
    id: str
    label: str
    tradable: bool = True  # 官方接口下主账户只读

    def to_json(self) -> dict:
        return {"id": self.id, "label": self.label, "tradable": self.tradable}


@dataclass
class Quote:
    symbol: str
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None


@dataclass
class Position:
    account: str
    symbol: str
    quantity: Decimal
    avg_cost: Decimal | None = None
    held_for_sells: Decimal = ZERO  # 已被未成交卖单占用的股数
    last_price: Decimal | None = None

    def to_json(self) -> dict:
        pnl_pct = None
        if self.avg_cost and self.last_price and self.avg_cost > 0:
            pnl_pct = float((self.last_price - self.avg_cost) / self.avg_cost * 100)
        return {
            "account": self.account,
            "symbol": self.symbol,
            "quantity": fmt(self.quantity),
            "avg_cost": fmt(self.avg_cost),
            "held_for_sells": fmt(self.held_for_sells),
            "last_price": fmt(self.last_price),
            "pnl_pct": pnl_pct,
            "market_value": fmt(self.quantity * self.last_price) if self.last_price else None,
        }


# 订单状态统一成这几种
OPEN_STATES = {"open", "partially_filled"}
# gone：已经不在挂单里，但接口说不清是成交了还是撤了（只有官方接口缺查单工具时会出现）
FINAL_STATES = {"filled", "cancelled", "rejected", "gone"}


@dataclass
class Order:
    """券商那边的一张卖单（已挂或已结束）。"""

    id: str
    account: str
    symbol: str
    kind: str  # limit | stop | stop_limit | market | oco | other
    quantity: Decimal
    side: str = "sell"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    state: str = "open"  # open | partially_filled | filled | cancelled | rejected | gone | unknown
    filled_quantity: Decimal = ZERO
    tif: str = "gtc"
    created_at: str = ""

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_final(self) -> bool:
        """确定已经结束。状态认不出（unknown）时既不算在挂、也不算结束。"""
        return self.state in FINAL_STATES

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "account": self.account,
            "symbol": self.symbol,
            "kind": self.kind,
            "side": self.side,
            "quantity": fmt(self.quantity),
            "limit_price": fmt(self.limit_price),
            "stop_price": fmt(self.stop_price),
            "state": self.state,
            "filled_quantity": fmt(self.filled_quantity),
            "tif": self.tif,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SellOrder:
    """要发给券商的一张卖单。本工具只会生成卖出平仓单。"""

    account: str
    symbol: str
    quantity: Decimal
    kind: str  # limit | stop | oco | market
    limit_price: Decimal | None = None  # limit / oco 的止盈价
    stop_price: Decimal | None = None  # stop / oco 的止损价
    tif: str = "gtc"
    ref: str = ""  # 本地流水号，写进审计日志

    def validate(self) -> None:
        if self.quantity <= 0 or self.quantity != whole_shares(self.quantity):
            raise ValueError(f"{self.symbol}: 数量必须是正整数股，收到 {self.quantity}")
        if self.kind not in ("limit", "stop", "oco", "market"):
            raise ValueError(f"{self.symbol}: 不支持的订单类型 {self.kind}")
        if self.kind in ("limit", "oco") and not (self.limit_price and self.limit_price > 0):
            raise ValueError(f"{self.symbol}: 缺少止盈限价")
        if self.kind in ("stop", "oco") and not (self.stop_price and self.stop_price > 0):
            raise ValueError(f"{self.symbol}: 缺少止损触发价")
        if self.kind == "oco" and not (self.limit_price > self.stop_price):  # type: ignore[operator]
            raise ValueError(f"{self.symbol}: 止盈价必须高于止损价")
        if self.tif not in ("gtc", "gfd"):
            raise ValueError(f"{self.symbol}: 有效期只能是 gtc 或 gfd")

    def to_json(self) -> dict:
        return {
            "account": self.account,
            "symbol": self.symbol,
            "quantity": fmt(self.quantity),
            "kind": self.kind,
            "limit_price": fmt(self.limit_price),
            "stop_price": fmt(self.stop_price),
            "tif": self.tif,
            "ref": self.ref,
        }

    @staticmethod
    def from_json(d: dict) -> "SellOrder":
        return SellOrder(
            account=d["account"],
            symbol=d["symbol"],
            quantity=Decimal(d["quantity"]),
            kind=d["kind"],
            limit_price=dec(d.get("limit_price")),
            stop_price=dec(d.get("stop_price")),
            tif=d.get("tif", "gtc"),
            ref=d.get("ref", ""),
        )


@dataclass
class Capabilities:
    native_oco: bool = False  # 券商接口能下「止盈/止损」联动单
    market_sell: bool = True
    order_status: bool = True  # 能按订单号查状态（盯盘需要）
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "native_oco": self.native_oco,
            "market_sell": self.market_sell,
            "order_status": self.order_status,
            "notes": self.notes,
        }
