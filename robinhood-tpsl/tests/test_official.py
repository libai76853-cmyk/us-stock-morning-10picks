import threading
from contextlib import asynccontextmanager
from decimal import Decimal as D

import pytest

from fake_mcp import Book, opener_for, style_a, style_b
from tpsl.brokers.base import BrokerAuthError, BrokerError
from tpsl.brokers.official import McpLink, NeedLogin, OfficialBroker
from tpsl.executor import execute
from tpsl.models import SellOrder
from tpsl.planner import PlanRequest, build_plan
from tpsl.watcher import Watcher

AG = "999999"


@pytest.fixture
def book():
    return Book()


@pytest.fixture
def a(cfg, book):
    b = OfficialBroker(cfg, opener=opener_for(style_a(book)))
    yield b
    b.close()


@pytest.fixture
def b(cfg, book):
    br = OfficialBroker(cfg, opener=opener_for(style_b(book)))
    yield br
    br.close()


# ---------------- 风格 A：Robinhood 自家叫法 ----------------

def test_a_accounts_mark_only_agentic_tradable(a):
    accts = {x.id: x for x in a.accounts()}
    assert accts["999999"].tradable and not accts["111111"].tradable


def test_a_positions_skip_crypto_and_quotes(a):
    pos = {p.symbol: p for p in a.positions(AG)}
    assert set(pos) == {"NVDA", "AAPL"}
    assert (pos["NVDA"].quantity, pos["NVDA"].avg_cost) == (D("10"), D("100.00"))
    q = a.quotes(["NVDA", "AAPL"])
    assert q["NVDA"].last == D("110.00") and q["AAPL"].last == D("210.00")


def test_a_stop_order_uses_market_plus_stop_trigger(a, book):
    o = a.place_sell(SellOrder(AG, "NVDA", D(10), "stop", stop_price=D("93.00")))
    kind, args = book.calls[-1]
    assert kind == "place" and args == {
        "account_number": AG, "symbol": "NVDA", "side": "sell", "quantity": "10", "type": "market",
        "trigger": "stop", "time_in_force": "gtc", "price": None, "stop_price": "93"}
    assert [c[0] for c in book.calls] == ["review", "place"]  # 先过 Robinhood 自己的预检
    assert (o.kind, o.state, o.stop_price, o.quantity) == ("stop", "open", D("93"), D(10))


def test_a_limit_order_and_listing_and_cancel(a, book):
    o = a.place_sell(SellOrder(AG, "NVDA", D(4), "limit", limit_price=D("115.00"), tif="gfd"))
    args = book.calls[-1][1]
    assert (args["type"], args["trigger"], args["price"], args["stop_price"], args["time_in_force"]) == (
        "limit", "immediate", "115", None, "gfd")
    (listed,) = a.open_orders(AG)
    assert (listed.id, listed.kind, listed.limit_price) == (o.id, "limit", D("115"))
    a.cancel(AG, o.id)
    assert a.get_order(AG, o.id).state == "cancelled" and a.open_orders(AG) == []


def test_a_rejection_is_a_clear_error_and_nothing_placed(a, book):
    with pytest.raises(BrokerError, match="agentic account"):
        a.place_sell(SellOrder("111111", "NVDA", D(1), "stop", stop_price=D("90")))
    assert [c[0] for c in book.calls] == ["review"] and book.orders == {}


def test_a_no_oco_but_watch_available(a):
    caps = a.capabilities()
    assert not caps.native_oco and caps.order_status
    with pytest.raises(BrokerError, match="联动单"):
        a.place_sell(SellOrder(AG, "NVDA", D(1), "oco", limit_price=D("120"), stop_price=D("90")))


def test_a_probe_passes_and_sends_no_orders(a, book):
    rep = a.probe()
    assert rep["ok"], rep["checks"]
    assert rep["roles"]["place"] == "place_equity_order" and rep["roles"]["positions"] == "get_portfolio"
    assert book.calls == [] and book.orders == {}
    built = {c["name"]: c["result"] for c in rep["checks"]}
    assert built["止损卖单的参数（未发送）"]["trigger"] == "stop"


def test_a_full_plan_with_watch_through_official_backend(a, book, cfg, clock):
    w = Watcher(cfg, a, threading.RLock(), clock=clock, sleep=lambda s: None, market_open=lambda: True)
    pos = a.positions(AG)
    q = a.quotes([p.symbol for p in pos])
    for p in pos:
        p.last_price = q[p.symbol].last
    req = PlanRequest.from_json({"account": AG, "tp_pct": 15, "sl_pct": 7})
    plan = build_plan(req, pos, a.open_orders(AG), a.capabilities())
    res = execute(plan, a, cfg, w, sleep=lambda s: None)
    assert res["ok"], res
    placed = [c[1] for c in book.calls if c[0] == "place"]
    assert {(p["symbol"], p["quantity"], p["stop_price"]) for p in placed} == {("NVDA", "10", "93"), ("AAPL", "5", "186")}
    assert len(w.list()) == 2
    # 涨到止盈价：撤止损、挂止盈
    book.prices["NVDA"] = "115.50"
    w.tick()
    nv = next(x for x in w.list() if x.symbol == "NVDA")
    assert nv.state == "tp_placed"
    last = [c[1] for c in book.calls if c[0] == "place"][-1]
    assert (last["symbol"], last["type"], last["price"], last["quantity"]) == ("NVDA", "limit", "115", "10")


def test_a_without_get_order_tool_disables_watch(cfg, book):
    br = OfficialBroker(cfg, opener=opener_for(style_a(book, with_get_order=False)))
    try:
        caps = br.capabilities()
        assert not caps.order_status and any("盯盘" in n for n in caps.notes)
        o = br.place_sell(SellOrder(AG, "NVDA", D(2), "stop", stop_price=D("90")))
        assert br.get_order(AG, o.id).state == "open"
        br.cancel(AG, o.id)
        gone = br.get_order(AG, o.id)
        assert gone.state == "gone" and gone.is_final and not gone.is_open
    finally:
        br.close()


# ---------------- 风格 B：通用叫法 + advanced order ----------------

def test_b_generic_names_and_enums(b, book):
    pos = {p.symbol: p for p in b.positions("default")}
    assert pos["AAPL"].quantity == D("5.5") and pos["NVDA"].avg_cost == D("100.0")
    assert b.quotes(["NVDA", "SPY"])["SPY"].last == D("600.0")  # 一次只收一只的 quote 工具
    o = b.place_sell(SellOrder("default", "NVDA", D(3), "stop", stop_price=D("93.00")))
    args = book.calls[-1][1]
    assert args == {"ticker": "NVDA", "side": "SELL", "shares": 3, "order_type": "STOP_MARKET",
                    "duration": "GTC", "limit_price": None, "stop_price": 93.0}
    assert (o.kind, o.state) == ("stop", "open")
    b.place_sell(SellOrder("default", "NVDA", D(2), "limit", limit_price=D("120.5"), tif="gfd"))
    args = book.calls[-1][1]
    assert (args["order_type"], args["duration"], args["limit_price"], args["stop_price"]) == ("LIMIT", "DAY", 120.5, None)


def test_b_native_oco_via_advanced_order(b, book):
    assert b.capabilities().native_oco
    o = b.place_sell(SellOrder("default", "NVDA", D(10), "oco", limit_price=D("115"), stop_price=D("93")))
    kind, args = book.calls[-1]
    assert kind == "adv_place" and (args["take_profit_price"], args["stop_loss_price"], args["shares"]) == (115.0, 93.0, 10)
    (listed,) = b.open_orders("default")
    assert (listed.id, listed.kind, listed.limit_price, listed.stop_price) == (o.id, "oco", D("115.0"), D("93.0"))
    b.cancel("default", o.id)
    assert book.calls[-1][0] == "adv_cancel" and b.open_orders("default") == []


def test_b_default_account_when_no_accounts_tool(b):
    (acct,) = b.accounts()
    assert acct.id == "default" and acct.tradable


# ---------------- 连接与登录 ----------------

def test_need_login_surfaces_as_auth_error(cfg):
    @asynccontextmanager
    async def opener():
        raise NeedLogin()
        yield  # pragma: no cover

    br = OfficialBroker(cfg, opener=opener)
    try:
        with pytest.raises(BrokerAuthError, match="login official"):
            br.positions("x")
        assert br.status()["ok"] is False
    finally:
        br.close()


def test_link_reconnects_after_idle_close(book):
    opens = []
    base = opener_for(style_a(book))

    @asynccontextmanager
    async def counting():
        opens.append(1)
        async with base() as c:
            yield c

    link = McpLink(counting, idle_seconds=0.05)
    try:
        assert len(link.list_tools()) >= 8
        import time
        time.sleep(1.4)  # 空闲回收
        link.call("get_equity_quotes", {"symbols": ["NVDA"]})
        assert len(opens) == 2
    finally:
        link.close()


def test_mapping_file_can_pin_tools_and_values(cfg, book):
    import json
    cfg.path("official_mapping.json").write_text(json.dumps({
        "tools": {"quotes": "get_equity_quotes"},
        "fixed_args": {"place_equity_order": {"time_in_force": "gfd"}},
    }))
    br = OfficialBroker(cfg, opener=opener_for(style_a(book)))
    try:
        br.place_sell(SellOrder(AG, "NVDA", D(1), "stop", stop_price=D("90")))
        assert book.calls[-1][1]["time_in_force"] == "gfd"
    finally:
        br.close()
