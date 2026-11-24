"""券商接口。三个后端（demo / official / unofficial）都实现这一套同步方法。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Account, Capabilities, Order, Position, Quote, SellOrder


class BrokerError(Exception):
    """券商明确拒绝或报错：订单没有下出去。"""


class BrokerAuthError(BrokerError):
    """没登录或登录过期，需要在 Mac 上重新登录。"""


class BrokerUncertain(BrokerError):
    """请求发出去了但没拿到明确结果（超时、断线）。

    订单可能已经挂上也可能没有。调用方不能自动重试，要让用户到「挂单」页核对。
    """


class Broker(ABC):
    name: str = ""
    label: str = ""

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def accounts(self) -> list[Account]: ...

    @abstractmethod
    def positions(self, account: str) -> list[Position]:
        """股票和 ETF 的多头持仓，不含期权、加密货币。last_price 可以不填。"""

    @abstractmethod
    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    @abstractmethod
    def open_orders(self, account: str) -> list[Order]:
        """未成交的股票卖单。"""

    @abstractmethod
    def get_order(self, account: str, order_id: str) -> Order | None:
        """按订单号查一张单；查不到返回 None。"""

    @abstractmethod
    def place_sell(self, order: SellOrder) -> Order:
        """下一张卖单。失败抛 BrokerError；结果不明抛 BrokerUncertain。"""

    @abstractmethod
    def cancel(self, account: str, order_id: str) -> None:
        """请求撤单。撤没撤成要再用 get_order 确认。"""

    def status(self) -> dict:
        """给页面显示的连接状态。"""
        return {"ok": True, "detail": ""}

    def close(self) -> None:  # noqa: B027 - 默认什么都不用做
        pass
