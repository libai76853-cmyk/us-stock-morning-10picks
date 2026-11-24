from decimal import Decimal as D

import pytest

from conftest import snapshot
from tpsl.models import Capabilities, round_price
from tpsl.planner import PlanError, PlanRequest, build_plan

OCO = Capabilities(native_oco=True)
PLAIN = Capabilities(native_oco=False)


def plan(broker, caps=OCO, picks=None, watcher_available=True, **kw):
    req = PlanRequest.from_json({"account": "A", **kw})
    pos, orders = snapshot(broker)
    return build_plan(req, pos, orders, caps, picks=picks, watcher_available=watcher_available)


def one(broker, **kw):
    p = plan(broker, **kw)
    assert len(p.items) == 1
    return p.items[0]


def test_percent_from_cost_rounds_to_cent(broker):
    broker.set_position("A", "NVDA", "10", "133.33", "140")
    it = one(broker, mode="both", basis="cost", tp_pct=15, sl_pct=7)
    assert it.tp_price == D("153.33")  # 133.33 * 1.15 = 153.3295
    assert it.sl_price == D("124.00")  # 133.33 * 0.93 = 123.9969
    assert it.strategy == "oco" and len(it.orders) == 1
    o = it.orders[0]
    assert (o.kind, o.quantity, o.limit_price, o.stop_price, o.tif) == ("oco", D(10), D("153.33"), D("124.00"), "gtc")


def test_percent_from_last(broker):
    broker.set_position("A", "NVDA", "10", "100", "200")
    it = one(broker, basis="last", tp_pct=10, sl_pct=5)
    assert (it.tp_price, it.sl_price) == (D("220.00"), D("190.00"))


def test_sub_dollar_uses_four_decimals(broker):
    broker.set_position("A", "PENY", "1000", "0.5123", "0.52")
    it = one(broker, mode="sl", sl_pct=10)
    assert it.sl_price == D("0.4611")
    assert round_price(D("0.99994")) == D("0.9999") and round_price(D("1.005")) == D("1.01")


def test_take_profit_already_passed_is_skipped(broker):
    broker.set_position("A", "NVDA", "10", "100", "130")
    it = one(broker, tp_pct=15, sl_pct=7)
    assert it.skipped and "立刻成交" in it.reason and not it.orders


def test_stop_already_passed_is_skipped(broker):
    broker.set_position("A", "TSLA", "10", "100", "90")
    it = one(broker, mode="sl", sl_pct=7)
    assert it.skipped and "立刻触发" in it.reason and not it.orders


def test_no_quote_is_skipped(broker):
    broker.set_position("A", "NVDA", "10", "100", "110")
    pos, orders = snapshot(broker)
    pos[0].last_price = None
    p = build_plan(PlanRequest.from_json({"account": "A"}), pos, orders, OCO)
    assert p.items[0].skipped and "现价" in p.items[0].reason


def test_no_cost_basis_needs_last(broker):
    broker.set_position("A", "GIFT", "10", None, "50")
    assert one(broker, basis="cost").skipped
    assert not one(broker, basis="last").skipped


def test_fractional_shares_only_whole_part(broker):
    broker.set_position("A", "PLTR", "60.5", "70", "75")
    it = one(broker, mode="sl", sl_pct=5)
    assert it.orders[0].quantity == D(60) and any("零碎股" in w for w in it.warnings)


def test_less_than_one_share_skipped(broker):
    broker.set_position("A", "BRK", "0.4", "100", "110")
    assert one(broker, mode="sl").skipped


def test_existing_orders_reduce_sellable_or_skip(broker):
    broker.set_position("A", "NVDA", "10", "100", "110")
    from tpsl.models import SellOrder
    broker.place_sell(SellOrder("A", "NVDA", D(4), "stop", stop_price=D("90")))
    it = one(broker, mode="sl", sl_pct=5)
    assert it.orders[0].quantity == D(6) and not it.cancels
    broker.place_sell(SellOrder("A", "NVDA", D(6), "stop", stop_price=D("91")))
    it = one(broker, mode="sl", sl_pct=5)
    assert it.skipped and "替换已有卖单" in it.reason


def test_replace_existing_cancels_then_uses_full_quantity(broker):
    broker.set_position("A", "NVDA", "10", "100", "110")
    from tpsl.models import SellOrder
    old = broker.place_sell(SellOrder("A", "NVDA", D(10), "stop", stop_price=D("90")))
    it = one(broker, mode="sl", sl_pct=5, replace_existing=True)
    assert [c.id for c in it.cancels] == [old.id] and it.orders[0].quantity == D(10)


def test_overrides(broker):
    broker.set_position("A", "NVDA", "10", "100", "110")
    it = one(broker, overrides={"nvda": {"tp_price": "125.5", "sl_price": "101", "qty": "3"}})
    o = it.orders[0]
    assert (o.limit_price, o.stop_price, o.quantity) == (D("125.50"), D("101.00"), D(3))
    assert one(broker, overrides={"NVDA": {"qty": "11"}}).skipped
    assert one(broker, overrides={"NVDA": {"qty": "2.5"}}).skipped
    assert one(broker, overrides={"NVDA": {"tp_price": "abc"}}).skipped


def test_strategy_resolution(broker):
    broker.set_position("A", "NVDA", "10", "100", "110")
    assert one(broker, caps=OCO).strategy == "oco"
    it = one(broker, caps=PLAIN)  # auto：没有联动单就盯盘
    assert it.strategy == "watch" and [o.kind for o in it.orders] == ["stop"]
    assert it.orders[0].quantity == D(10) and it.watch == {"tp_price": "115.00", "sl_price": "93.00", "quantity": "10"}
    assert one(broker, caps=Capabilities(native_oco=False, order_status=False)).strategy == "split"
    with pytest.raises(PlanError):
        plan(broker, caps=PLAIN, both_strategy="oco")
    with pytest.raises(PlanError):
        plan(broker, caps=Capabilities(native_oco=False, order_status=False), both_strategy="watch")


def test_split_puts_stop_first_and_covers_all_shares(broker):
    broker.set_position("A", "NVDA", "11", "100", "110")
    it = one(broker, caps=PLAIN, both_strategy="split")
    assert [(o.kind, o.quantity) for o in it.orders] == [("stop", D(6)), ("limit", D(5))]
    j = it.to_json()
    assert j["pnl_at_tp"] == "75" and j["pnl_at_sl"] == "-42"  # 5 股 × 15，6 股 × -7


def test_split_single_share_falls_back_to_stop_only(broker):
    broker.set_position("A", "NVDA", "1", "100", "110")
    it = one(broker, caps=PLAIN, both_strategy="split")
    assert [o.kind for o in it.orders] == ["stop"] and it.tp_price is None and it.warnings


def test_picks_basis(broker):
    broker.set_position("A", "CPAY", "8", "437.36", "441.9")
    broker.set_position("A", "AAPL", "5", "200", "210")
    picks = {"CPAY": {"stop_price": 412.28, "target_price": 487.53}}
    p = plan(broker, basis="picks", picks=picks)
    by = {i.symbol: i for i in p.items}
    assert (by["CPAY"].tp_price, by["CPAY"].sl_price) == (D("487.53"), D("412.28"))
    assert by["AAPL"].skipped and "选股" in by["AAPL"].reason


def test_symbols_filter_and_unknown_symbol(broker):
    broker.set_position("A", "NVDA", "10", "100", "110")
    broker.set_position("A", "AAPL", "10", "100", "110")
    p = plan(broker, symbols=["nvda", "ZZZZ"])
    assert [i.symbol for i in p.items] == ["NVDA", "ZZZZ"]
    assert p.items[1].skipped and p.to_json()["summary"] == {
        "actionable": 1, "skipped": 1, "orders": 1, "cancels": 0, "watches": 0}


@pytest.mark.parametrize("bad", [
    {"mode": "buy"}, {"basis": "x"}, {"tp_pct": 0}, {"tp_pct": 501}, {"sl_pct": 0}, {"sl_pct": 91},
    {"sl_pct": "abc"}, {"tif": "ioc"}, {"account": ""}, {"both_strategy": "yolo"}, {"symbols": "NVDA"},
    {"split_tp_ratio": 0.95},
])
def test_bad_requests_rejected(bad):
    with pytest.raises(PlanError):
        PlanRequest.from_json({"account": "A", **bad})


def test_every_planned_order_is_a_sell_within_holdings(broker):
    """不变量：无论怎么设，生成的都是卖单，且每只合计不超过持有的整股数。"""
    import itertools
    broker.set_position("A", "AAA", "7", "50", "55")
    broker.set_position("A", "BBB", "3.9", "20", "19.5")
    broker.set_position("A", "CCC", "1", "9", "9.5")
    held = {p.symbol: p.quantity for p in broker.positions("A")}
    for mode, basis, strat, caps in itertools.product(
            ("both", "sl", "tp"), ("cost", "last"), ("auto", "split", "watch"), (OCO, PLAIN)):
        p = plan(broker, caps=caps, mode=mode, basis=basis, both_strategy=strat, tp_pct=12, sl_pct=6)
        for it in p.items:
            total = sum(o.quantity for o in it.orders)
            assert total <= int(held[it.symbol]), (mode, basis, strat, it.symbol)
            for o in it.orders:
                o.validate()
                assert o.quantity == int(o.quantity) and o.quantity > 0
                if o.limit_price is not None:
                    assert o.limit_price > it.last
                if o.stop_price is not None:
                    assert o.stop_price < it.last
