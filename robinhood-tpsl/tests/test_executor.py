from decimal import Decimal as D

from conftest import snapshot
from tpsl.brokers.base import BrokerUncertain
from tpsl.executor import cancel_orders, execute
from tpsl.models import Capabilities, SellOrder
from tpsl.planner import PlanRequest, build_plan

NOSLEEP = dict(sleep=lambda s: None)


def make(broker, caps=None, **kw):
    req = PlanRequest.from_json({"account": "A", **kw})
    pos, orders = snapshot(broker)
    return build_plan(req, pos, orders, caps or broker.capabilities())


def test_oco_happy_path(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    res = execute(make(broker), broker, cfg, watcher, **NOSLEEP)
    assert res["ok"] and res["summary"] == {"done": 1, "bad": 0, "skipped": 0}
    (o,) = broker.open_orders("A")
    assert (o.kind, o.quantity, o.limit_price, o.stop_price) == ("oco", D(10), D("115.00"), D("93.00"))
    events = [e["event"] for e in cfg.audit_tail()]
    assert "execute_start" in events and "execute_item" in events and "execute_end" in events


def test_dry_run_sends_nothing(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    old = broker.place_sell(SellOrder("A", "NVDA", D(10), "stop", stop_price=D("80")))
    plan = make(broker, caps=Capabilities(native_oco=False), replace_existing=True)
    res = execute(plan, broker, cfg, watcher, dry_run=True, **NOSLEEP)
    assert res["dry_run"] and res["results"][0]["status"] == "dry_run"
    assert [o.id for o in broker.open_orders("A")] == [old.id]  # 旧单没撤，新单没挂
    assert watcher.list() == []


def test_price_moved_through_stop_aborts_item(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    plan = make(broker, mode="sl", sl_pct=7)  # 止损 93
    broker.set_price("NVDA", "92")
    res = execute(plan, broker, cfg, watcher, **NOSLEEP)
    assert not res["ok"] and res["results"][0]["status"] == "failed" and "止损价" in res["results"][0]["message"]
    assert broker.open_orders("A") == []


def test_price_moved_through_target_aborts_item(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    plan = make(broker, mode="tp", tp_pct=15)
    broker.set_price("NVDA", "116")
    res = execute(plan, broker, cfg, watcher, **NOSLEEP)
    assert res["results"][0]["status"] == "failed" and broker.open_orders("A") == []


def test_position_shrank_aborts_item(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    plan = make(broker, mode="sl")
    broker.set_position("A", "NVDA", "4", "100", "110")
    res = execute(plan, broker, cfg, watcher, **NOSLEEP)
    assert res["results"][0]["status"] == "failed" and "可卖股数变了" in res["results"][0]["message"]
    assert broker.open_orders("A") == []


def test_replace_existing_cancels_then_places(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    old = broker.place_sell(SellOrder("A", "NVDA", D(10), "stop", stop_price=D("80")))
    res = execute(make(broker, mode="sl", sl_pct=5, replace_existing=True), broker, cfg, watcher, **NOSLEEP)
    assert res["ok"]
    (o,) = broker.open_orders("A")
    assert o.id != old.id and o.stop_price == D("95.00")
    assert broker.get_order("A", old.id).state == "cancelled"


def test_split_second_leg_rejected_is_partial_and_keeps_stop(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    plan = make(broker, caps=Capabilities(native_oco=False), both_strategy="split")
    real = broker.place_sell
    calls = []

    def flaky(order):
        calls.append(order.kind)
        if len(calls) == 2:
            broker.fail_next_place = "模拟被拒"
        return real(order)

    broker.place_sell = flaky
    res = execute(plan, broker, cfg, watcher, **NOSLEEP)
    assert calls == ["stop", "limit"] and res["results"][0]["status"] == "partial" and not res["ok"]
    assert [o.kind for o in broker.open_orders("A")] == ["stop"]


def test_uncertain_result_is_not_retried(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    broker.set_position("A", "AAPL", "5", "100", "110")
    plan = make(broker, mode="sl")
    calls = []
    real = broker.place_sell

    def timeout_once(order):
        calls.append(order.symbol)
        if order.symbol == "AAPL":
            raise BrokerUncertain("超时")
        return real(order)

    broker.place_sell = timeout_once
    res = execute(plan, broker, cfg, watcher, **NOSLEEP)
    by = {r["symbol"]: r for r in res["results"]}
    assert by["AAPL"]["status"] == "unknown" and "核对" in by["AAPL"]["message"]
    assert by["NVDA"]["status"] == "done"
    assert calls.count("AAPL") == 1  # 没有重试


def test_watch_strategy_arms_watcher(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    res = execute(make(broker, caps=Capabilities(native_oco=False), both_strategy="watch"),
                  broker, cfg, watcher, **NOSLEEP)
    assert res["ok"]
    (o,) = broker.open_orders("A")
    (w,) = watcher.list()
    assert o.kind == "stop" and w.stop_order_id == o.id and (w.tp_price, w.sl_price, w.state) == ("115.00", "93.00", "armed")


def test_snapshot_failure_places_nothing(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    plan = make(broker, mode="sl")
    from tpsl.brokers.base import BrokerError

    def boom(_):
        raise BrokerError("断网")

    broker.positions = boom
    res = execute(plan, broker, cfg, watcher, **NOSLEEP)
    assert not res["ok"] and "一张都没下" in res["error"]


def test_cancel_orders_and_stops_owning_watch(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    execute(make(broker, caps=Capabilities(native_oco=False), both_strategy="watch"), broker, cfg, watcher, **NOSLEEP)
    (o,) = broker.open_orders("A")
    res = cancel_orders(broker, cfg, watcher, "A", [o.id, "nope"], **NOSLEEP)
    assert res["ok"] and broker.open_orders("A") == []
    assert watcher.list()[0].state == "cancelled"
    # 演练不撤
    o2 = broker.place_sell(SellOrder("A", "NVDA", D(10), "stop", stop_price=D("90")))
    assert cancel_orders(broker, cfg, watcher, "A", [o2.id], dry_run=True, **NOSLEEP)["dry_run"]
    assert len(broker.open_orders("A")) == 1


def test_unexpected_crash_stops_the_run(broker, cfg, watcher):
    broker.set_position("A", "AAPL", "5", "100", "110")
    broker.set_position("A", "NVDA", "10", "100", "110")
    plan = make(broker, mode="sl")
    real = broker.place_sell
    seen = []

    def buggy(order):
        seen.append(order.symbol)
        if order.symbol == "AAPL":
            raise KeyError("bug")
        return real(order)

    broker.place_sell = buggy
    res = execute(plan, broker, cfg, watcher, **NOSLEEP)
    by = {r["symbol"]: r for r in res["results"]}
    assert by["AAPL"]["status"] == "unknown" and by["NVDA"]["status"] == "failed"
    assert seen == ["AAPL"] and not res["ok"]  # 出错后不再继续下单


def test_replace_tolerates_stale_held_count_but_catches_partial_fill(broker, cfg, watcher):
    broker.set_position("A", "NVDA", "10", "100", "110")
    broker.place_sell(SellOrder("A", "NVDA", D(10), "stop", stop_price=D("80")))
    plan = make(broker, mode="sl", sl_pct=5, replace_existing=True)
    real = broker.positions

    def stale(account):
        ps = real(account)
        for p in ps:
            p.held_for_sells = D(10)  # 撤完了但占用数还没更新
        return ps

    broker.positions = stale
    assert execute(plan, broker, cfg, watcher, **NOSLEEP)["ok"]

    # 另一种情况：旧单在撤销前成交了一部分，持仓变少 -> 不下新单，并说清楚现在没有挂单
    broker.positions = real
    plan = make(broker, mode="sl", sl_pct=6, replace_existing=True)
    real_cancel = broker.cancel

    def cancel_after_partial_fill(account, oid):
        real_cancel(account, oid)
        broker._pos["A"]["NVDA"].quantity = D(7)

    broker.cancel = cancel_after_partial_fill
    res = execute(plan, broker, cfg, watcher, **NOSLEEP)
    assert res["results"][0]["status"] == "failed" and "没有挂单" in res["results"][0]["message"]
    assert broker.open_orders("A") == []
