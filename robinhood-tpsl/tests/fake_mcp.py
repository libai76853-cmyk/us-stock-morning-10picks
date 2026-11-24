"""进程内的假 Robinhood MCP 服务，给官方后端的测试用。

两种参数风格都做一份：
- style A：照 Robinhood 自家接口的叫法（account_number / type + trigger / price / time_in_force）
- style B：通用叫法（account_id / ticker / shares / order_type 枚举 / duration），外加 advanced order
真实服务的参数名在这里核对不了，这两份只用来证明适配层两种叫法都能对上。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


def opener_for(server):
    @asynccontextmanager
    async def opener():
        async with Client(server) as client:
            yield client

    return opener


class Book:
    def __init__(self):
        self.positions = {"NVDA": ["10", "100.00"], "AAPL": ["5.5", "200.00"]}
        self.prices = {"NVDA": "110.00", "AAPL": "210.00", "SPY": "600.00"}
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.n = 0

    def add(self, **o) -> dict:
        self.n += 1
        o = {"id": f"ord-{self.n}", "state": "confirmed", "cumulative_quantity": "0", **o}
        self.orders[o["id"]] = o
        return o


def style_a(book: Book, with_get_order: bool = True) -> MCPServer:
    srv = MCPServer("fake-rh-a")

    @srv.tool()
    def get_accounts() -> dict:
        return {"accounts": [
            {"account_number": "111111", "brokerage_account_type": "individual"},
            {"account_number": "999999", "nickname": "Agentic", "is_agentic_account": True},
        ]}

    @srv.tool()
    def get_portfolio(account_number: str) -> dict:
        return {"account_number": account_number, "positions": [
            *({"symbol": s, "quantity": q, "average_buy_price": c, "shares_held_for_sells": "0", "type": "equity"}
              for s, (q, c) in book.positions.items()),
            {"symbol": "BTC", "quantity": "0.1", "average_buy_price": "60000", "type": "crypto"},
        ]}

    @srv.tool()
    def get_equity_quotes(symbols: list[str]) -> dict:
        return {"quotes": [{"symbol": s, "last_trade_price": book.prices[s], "bid_price": book.prices[s],
                            "ask_price": book.prices[s]} for s in symbols if s in book.prices]}

    @srv.tool()
    def get_equity_orders(account_number: str) -> dict:
        return {"orders": list(book.orders.values())}

    if with_get_order:
        @srv.tool()
        def get_equity_order(order_id: str) -> dict:
            return book.orders[order_id]

    def check(account_number, symbol, side, quantity, type, trigger, time_in_force, price, stop_price):
        if account_number != "999999":
            raise ToolError("Trading is only allowed in the agentic account")
        if side != "sell":
            raise ToolError("test server only expects sells")
        if trigger == "stop" and stop_price is None:
            raise ToolError("stop_price required")
        if type == "limit" and price is None:
            raise ToolError("price required")

    @srv.tool()
    def review_equity_order(account_number: str, symbol: str, side: Literal["buy", "sell"], quantity: str,
                            type: Literal["market", "limit"], trigger: Literal["immediate", "stop"],
                            time_in_force: Literal["gfd", "gtc"], price: str | None = None,
                            stop_price: str | None = None) -> dict:
        book.calls.append(("review", dict(symbol=symbol, type=type, trigger=trigger)))
        check(account_number, symbol, side, quantity, type, trigger, time_in_force, price, stop_price)
        return {"ok": True, "estimated_proceeds": "0"}

    @srv.tool()
    def place_equity_order(account_number: str, symbol: str, side: Literal["buy", "sell"], quantity: str,
                           type: Literal["market", "limit"], trigger: Literal["immediate", "stop"],
                           time_in_force: Literal["gfd", "gtc"], price: str | None = None,
                           stop_price: str | None = None) -> dict:
        args = dict(account_number=account_number, symbol=symbol, side=side, quantity=quantity, type=type,
                    trigger=trigger, time_in_force=time_in_force, price=price, stop_price=stop_price)
        book.calls.append(("place", args))
        check(**args)
        return book.add(**args)

    @srv.tool()
    def cancel_equity_order(order_id: str) -> dict:
        book.calls.append(("cancel", {"order_id": order_id}))
        book.orders[order_id]["state"] = "cancelled"
        return {}

    return srv


def style_b(book: Book) -> MCPServer:
    srv = MCPServer("fake-rh-b")

    @srv.tool()
    def get_positions(account_id: str | None = None) -> list[dict]:
        return [{"ticker": s, "shares": float(q), "avg_cost": float(c), "current_price": float(book.prices[s])}
                for s, (q, c) in book.positions.items()]

    @srv.tool()
    def get_quote(ticker: str) -> dict:
        return {"ticker": ticker, "price": float(book.prices[ticker])}

    @srv.tool()
    def list_orders(account_id: str | None = None) -> list[dict]:
        return [o for o in book.orders.values() if "take_profit_price" not in o]

    @srv.tool()
    def get_order(order_id: str) -> dict:
        return book.orders[order_id]

    @srv.tool()
    def place_equity_order(ticker: str, side: Literal["BUY", "SELL"], shares: int,
                           order_type: Literal["MARKET", "LIMIT", "STOP_MARKET", "STOP_LIMIT"],
                           duration: Literal["DAY", "GTC"], limit_price: float | None = None,
                           stop_price: float | None = None, account_id: str | None = None) -> dict:
        args = dict(ticker=ticker, side=side, shares=shares, order_type=order_type, duration=duration,
                    limit_price=limit_price, stop_price=stop_price)
        book.calls.append(("place", args))
        return {"order": book.add(status="OPEN", **args)}

    @srv.tool()
    def cancel_order(order_id: str) -> str:
        book.orders[order_id]["status"] = "CANCELED"
        book.orders[order_id].pop("state", None)
        return "cancelled"

    @srv.tool()
    def place_advanced_order(ticker: str, shares: int, take_profit_price: float, stop_loss_price: float,
                             duration: Literal["DAY", "GTC"], account_id: str | None = None) -> dict:
        args = dict(ticker=ticker, shares=shares, take_profit_price=take_profit_price,
                    stop_loss_price=stop_loss_price, duration=duration, side="SELL")
        book.calls.append(("adv_place", args))
        return book.add(status="OPEN", **args)

    @srv.tool()
    def get_advanced_orders(account_id: str | None = None) -> list[dict]:
        return [o for o in book.orders.values() if "take_profit_price" in o]

    @srv.tool()
    def cancel_advanced_order(order_id: str) -> dict:
        book.calls.append(("adv_cancel", {"order_id": order_id}))
        book.orders[order_id]["status"] = "CANCELED"
        book.orders[order_id].pop("state", None)
        return {}

    return srv
