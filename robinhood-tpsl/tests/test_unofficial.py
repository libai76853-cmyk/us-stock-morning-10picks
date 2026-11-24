"""非官方后端：用假的 HTTP 会话顶替 Robinhood，URL 用 robin_stocks 真实的拼法。"""

import pickle
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

urls = pytest.importorskip("robin_stocks.robinhood.urls")

from tpsl.brokers.base import BrokerAuthError, BrokerError, BrokerUncertain  # noqa: E402
from tpsl.brokers.unofficial import UnofficialBroker, session_dir  # noqa: E402
from tpsl.models import SellOrder  # noqa: E402


class Resp:
    def __init__(self, code=200, data=None, text=None):
        self.status_code, self._data = code, data
        self.text = text if text is not None else str(data)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.routes = {}
        self.gets, self.posts = [], []
        self.mounted = False

    def mount(self, prefix, adapter):
        self.mounted = True

    def get(self, url, params=None, timeout=None):
        assert timeout, "读请求必须带超时"
        self.gets.append((url, params))
        for prefix, fn in self.routes.items():
            if url.startswith(prefix):
                return fn(url, params)
        return Resp(404, {"detail": "Not found."})

    def post(self, url, data=None, timeout=None):
        self.posts.append(url)
        return self.routes.get("POST", lambda u: Resp(200, {}))(url)


ACCT = "5QR12345"
ORDERS = [
    {"id": "o-stop", "symbol": "NVDA", "side": "sell", "type": "market", "trigger": "stop", "price": None,
     "stop_price": "93.00", "quantity": "10.00000", "cumulative_quantity": "0", "state": "confirmed",
     "time_in_force": "gtc", "created_at": "2026-11-20T15:00:00Z", "account_number": ACCT},
    {"id": "o-buy", "symbol": "AAPL", "side": "buy", "type": "limit", "trigger": "immediate", "price": "190",
     "quantity": "1", "cumulative_quantity": "0", "state": "queued", "time_in_force": "gtc"},
    {"id": "o-done", "symbol": "NVDA", "side": "sell", "type": "limit", "trigger": "immediate", "price": "120",
     "quantity": "2", "cumulative_quantity": "2", "state": "filled", "time_in_force": "gtc"},
]


@pytest.fixture
def rig(cfg):
    sess = FakeSession()
    placed = []
    rh = SimpleNamespace(
        globals=SimpleNamespace(SESSION=sess), urls=urls,
        helper=SimpleNamespace(set_login_state=lambda v: None,
                               update_session=lambda k, v: sess.headers.__setitem__(k, v)),
        orders=SimpleNamespace(order=None),
    )

    def order(symbol, quantity, side, limitPrice=None, stopPrice=None, account_number=None,
              timeInForce="gtc", extendedHours=False, jsonify=True):
        placed.append(dict(symbol=symbol, quantity=quantity, side=side, limitPrice=limitPrice,
                           stopPrice=stopPrice, account_number=account_number, timeInForce=timeInForce,
                           extendedHours=extendedHours, jsonify=jsonify))
        return rig_ns.order_response

    rh.orders.order = order
    sess.routes = {
        "https://api.robinhood.com/accounts/": lambda u, p: Resp(200, {"results": [
            {"account_number": ACCT, "brokerage_account_type": "individual", "type": "margin"},
            {"account_number": "DEAD0000", "deactivated": True}], "next": None}),
        "https://api.robinhood.com/positions/": lambda u, p: Resp(200, {"results": [
            {"symbol": "NVDA", "quantity": "10.00000000", "average_buy_price": "100.5000",
             "shares_held_for_sells": "10.0000", "instrument": "https://api.robinhood.com/instruments/n/"},
            {"quantity": "2.5", "average_buy_price": "0.0000", "shares_held_for_sells": "0",
             "instrument": "https://api.robinhood.com/instruments/a/"},
            {"symbol": "GONE", "quantity": "0.0000", "average_buy_price": "1"}], "next": None}),
        "https://api.robinhood.com/instruments/a/": lambda u, p: Resp(200, {"symbol": "AAPL"}),
        "https://api.robinhood.com/quotes/": lambda u, p: Resp(200, {"results": [
            {"symbol": "NVDA", "last_trade_price": "110.250000", "last_extended_hours_trade_price": None,
             "bid_price": "110.20", "ask_price": "110.30"}, None]}),
        "https://api.robinhood.com/orders/o-stop/": lambda u, p: Resp(200, ORDERS[0]),
        "https://api.robinhood.com/orders/missing/": lambda u, p: Resp(404, {"detail": "Not found."}),
        "https://api.robinhood.com/orders/": lambda u, p: Resp(200, {"results": ORDERS, "next": None}),
    }
    d = session_dir(cfg)
    with open(d / "robinhood.pickle", "wb") as f:
        pickle.dump({"token_type": "Bearer", "access_token": "tok", "refresh_token": "r", "device_token": "d"}, f)
    rig_ns = SimpleNamespace(sess=sess, rh=rh, placed=placed, order_response=None, cfg=cfg)
    rig_ns.broker = UnofficialBroker(cfg, rh=rh)
    return rig_ns


def test_session_restored_without_touching_login(rig):
    assert rig.sess.headers["Authorization"] == "Bearer tok" and rig.sess.mounted
    assert rig.broker.status()["ok"] and rig.sess.posts == [] and rig.sess.gets == []


def test_accounts_positions_quotes(rig):
    b = rig.broker
    (acct,) = b.accounts()
    assert acct.id == ACCT and acct.label.endswith("2345")
    pos = {p.symbol: p for p in b.positions(ACCT)}
    assert set(pos) == {"NVDA", "AAPL"}
    assert (pos["NVDA"].quantity, pos["NVDA"].avg_cost, pos["NVDA"].held_for_sells) == (D("10"), D("100.5"), D("10"))
    assert pos["AAPL"].avg_cost is None  # 成本 0 当作没有
    q = b.quotes(["NVDA", "ZZZZ"])
    assert q["NVDA"].last == D("110.25") and "ZZZZ" not in q


def test_open_orders_only_open_sells(rig):
    (o,) = rig.broker.open_orders(ACCT)
    assert (o.id, o.kind, o.stop_price, o.quantity, o.limit_price) == ("o-stop", "stop", D("93.00"), D("10"), None)
    url, params = rig.sess.gets[-1]
    assert "account_numbers=" + ACCT in url and "updated_at" in url


def test_get_order_and_missing(rig):
    assert rig.broker.get_order(ACCT, "o-stop").state == "open"
    assert rig.broker.get_order(ACCT, "missing") is None


def test_place_stop_passes_through_library_order(rig):
    rig.order_response = Resp(201, {**ORDERS[0], "id": "new-1", "state": "unconfirmed"})
    o = rig.broker.place_sell(SellOrder(ACCT, "NVDA", D(10), "stop", stop_price=D("93.00")))
    assert rig.placed == [dict(symbol="NVDA", quantity=10, side="sell", limitPrice=None, stopPrice=93.0,
                               account_number=ACCT, timeInForce="gtc", extendedHours=False, jsonify=False)]
    assert (o.id, o.kind, o.state) == ("new-1", "stop", "open")


def test_place_limit(rig):
    rig.order_response = Resp(201, {**ORDERS[2], "id": "new-2", "state": "queued", "cumulative_quantity": "0"})
    o = rig.broker.place_sell(SellOrder(ACCT, "NVDA", D(2), "limit", limit_price=D("120"), tif="gfd"))
    assert rig.placed[0]["limitPrice"] == 120.0 and rig.placed[0]["stopPrice"] is None
    assert rig.placed[0]["timeInForce"] == "gfd" and o.kind == "limit"


def test_place_rejected_no_response_and_server_error(rig):
    order = SellOrder(ACCT, "NVDA", D(1), "stop", stop_price=D("93"))
    rig.order_response = Resp(400, {"detail": "Not enough shares to sell."})
    with pytest.raises(BrokerError, match="Not enough shares") as e:
        rig.broker.place_sell(order)
    assert not isinstance(e.value, BrokerUncertain)
    rig.order_response = None  # 库吞掉了超时
    with pytest.raises(BrokerUncertain):
        rig.broker.place_sell(order)
    rig.order_response = Resp(503, None, text="upstream")
    with pytest.raises(BrokerUncertain):
        rig.broker.place_sell(order)
    rig.order_response = Resp(200, {"non_field_errors": ["Stop price must be below market"]})
    with pytest.raises(BrokerError, match="below market"):
        rig.broker.place_sell(order)


def test_exception_before_post_means_not_sent(rig):
    def boom(*a, **k):
        raise IndexError("list index out of range")

    rig.rh.orders.order = boom
    with pytest.raises(BrokerError, match="没有发出") as e:
        rig.broker.place_sell(SellOrder(ACCT, "NVDA", D(1), "stop", stop_price=D("93")))
    assert not isinstance(e.value, BrokerUncertain)


def test_oco_not_supported(rig):
    assert not rig.broker.capabilities().native_oco
    with pytest.raises(BrokerError, match="联动单"):
        rig.broker.place_sell(SellOrder(ACCT, "NVDA", D(1), "oco", limit_price=D("120"), stop_price=D("90")))
    assert rig.placed == []


def test_cancel_posts_cancel_url(rig):
    rig.broker.cancel(ACCT, "o-stop")
    assert rig.sess.posts == ["https://api.robinhood.com/orders/o-stop/cancel/"]


def test_expired_session_reports_login_needed(rig):
    rig.sess.routes["https://api.robinhood.com/positions/"] = lambda u, p: Resp(401, {"detail": "expired"})
    with pytest.raises(BrokerAuthError, match="login unofficial"):
        rig.broker.positions(ACCT)
    assert rig.broker.status()["ok"] is False


def test_no_session_file_never_calls_robinhood(cfg):
    sess = FakeSession()
    rh = SimpleNamespace(globals=SimpleNamespace(SESSION=sess), urls=urls, orders=SimpleNamespace(),
                         helper=SimpleNamespace(set_login_state=lambda v: None, update_session=lambda k, v: None))
    b = UnofficialBroker(cfg, rh=rh)
    assert b.status()["ok"] is False
    with pytest.raises(BrokerAuthError):
        b.positions("x")
    with pytest.raises(BrokerAuthError):
        b.place_sell(SellOrder("x", "NVDA", D(1), "stop", stop_price=D("9")))
    assert sess.gets == [] and sess.posts == []
