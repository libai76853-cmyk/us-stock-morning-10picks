import threading
from decimal import Decimal as D

from tpsl.brokers.base import BrokerUncertain
from tpsl.models import SellOrder
from tpsl.watcher import Watcher, is_market_open


def arm(broker, watcher, qty="10", tp="115", sl="93"):
    broker.set_position("A", "NVDA", qty, "100", "110")
    stop = broker.place_sell(SellOrder("A", "NVDA", D(qty), "stop", stop_price=D(sl)))
    return watcher.arm("A", "NVDA", D(qty), D(tp), D(sl), "gtc", stop.id), stop


def position(broker):
    return next((p.quantity for p in broker.positions("A") if p.symbol == "NVDA"), D(0))


def test_nothing_happens_between_stop_and_target(broker, watcher):
    w, stop = arm(broker, watcher)
    for px in ("100", "114.99", "93.01"):
        broker.set_price("NVDA", px)
        watcher.tick()
    assert w.state == "armed" and [o.id for o in broker.open_orders("A")] == [stop.id]


def test_target_hit_swaps_stop_for_limit_and_fills(broker, watcher):
    w, stop = arm(broker, watcher)
    broker.set_price("NVDA", "115.20")
    watcher.tick()
    assert broker.get_order("A", stop.id).state == "cancelled"
    tp = broker.get_order("A", w.tp_order_id)
    assert (tp.kind, tp.limit_price, tp.quantity) == ("limit", D("115.00"), D(10))
    watcher.tick()
    assert w.state == "done" and position(broker) == 0


def test_stop_fill_ends_watch(broker, watcher):
    w, _ = arm(broker, watcher)
    broker.set_price("NVDA", "92")
    watcher.tick()
    assert w.state == "stopped" and position(broker) == 0 and broker.open_orders("A") == []


def test_stop_cancelled_by_user_ends_watch_without_orders(broker, watcher):
    w, stop = arm(broker, watcher)
    broker.cancel("A", stop.id)
    broker.set_price("NVDA", "120")
    watcher.tick()
    assert w.state == "cancelled" and "没有止损保护" in w.note
    assert broker.open_orders("A") == [] and position(broker) == 10  # 没有擅自卖


def test_unfilled_target_reverts_to_stop_after_timeout(broker, watcher, clock, cfg):
    w, stop = arm(broker, watcher)
    broker.freeze = True  # 限价单挂上但不成交
    broker.set_price("NVDA", "115.10")
    watcher.tick()
    assert w.state == "tp_placed"
    tp_id = w.tp_order_id
    broker.set_price("NVDA", "114")
    clock.t += cfg.tp_timeout - 1
    watcher.tick()
    assert w.state == "tp_placed"  # 还没到时间，继续等
    clock.t += 2
    watcher.tick()
    assert w.state == "armed" and w.reverts == 1 and w.tp_order_id is None
    assert broker.get_order("A", tp_id).state == "cancelled"
    (o,) = broker.open_orders("A")
    assert (o.kind, o.stop_price, o.quantity) == ("stop", D("93.00"), D(10)) and o.id == w.stop_order_id != stop.id


def test_unfilled_target_stays_while_price_holds(broker, watcher, clock, cfg):
    w, _ = arm(broker, watcher)
    broker.freeze = True
    broker.set_price("NVDA", "116")
    watcher.tick()
    clock.t += cfg.tp_timeout * 3
    watcher.tick()
    assert w.state == "tp_placed"


def test_crash_below_stop_while_target_resting_sells_at_market(broker, watcher):
    w, _ = arm(broker, watcher)
    broker.freeze = True
    broker.set_price("NVDA", "115.10")
    watcher.tick()
    broker.set_price("NVDA", "90")
    watcher.tick()
    assert w.state == "stopped" and "市价卖出" in w.note
    broker.freeze = False
    broker.set_price("NVDA", "90")
    assert position(broker) == 0


def test_target_order_rejected_puts_stop_back(broker, watcher):
    w, stop = arm(broker, watcher)
    broker.set_price("NVDA", "115.10")
    broker.fail_next_place = "模拟被拒"
    watcher.tick()
    assert w.state == "armed" and w.reverts == 1
    (o,) = broker.open_orders("A")
    assert o.kind == "stop" and o.id != stop.id


def test_uncertain_placement_needs_human(broker, watcher):
    w, _ = arm(broker, watcher)
    broker.set_price("NVDA", "115.10")

    def boom(order):
        raise BrokerUncertain("超时")

    broker.place_sell = boom
    watcher.tick()
    assert w.state == "error" and "核对" in w.note
    watcher.tick()  # error 之后不再自动动作
    assert w.state == "error"


def test_never_sells_more_than_held(broker, watcher):
    w, stop = arm(broker, watcher)
    broker.freeze = True
    broker.set_price("NVDA", "115.10")
    # 止损单撤掉之后、止盈单挂出之前，持仓被别处卖掉一部分
    real_cancel = broker.cancel

    def cancel_and_shrink(account, oid):
        real_cancel(account, oid)
        broker._pos["A"]["NVDA"].quantity = D(4)

    broker.cancel = cancel_and_shrink
    watcher.tick()
    tp = broker.get_order("A", w.tp_order_id)
    assert tp.quantity == D(4) and w.quantity == "4"


def test_position_gone_stops_quietly(broker, watcher):
    w, _ = arm(broker, watcher)
    broker.freeze = True
    broker.set_price("NVDA", "115.10")
    real_cancel = broker.cancel

    def cancel_and_sell_all(account, oid):
        real_cancel(account, oid)
        broker._pos["A"]["NVDA"].quantity = D(0)

    broker.cancel = cancel_and_sell_all
    watcher.tick()
    assert w.state == "cancelled" and broker.open_orders("A") == []


def test_cancel_not_confirmed_retries_then_gives_up(broker, watcher):
    w, stop = arm(broker, watcher)
    broker.set_price("NVDA", "115.10")
    broker.cancel = lambda account, oid: None  # 撤单请求石沉大海
    for _ in range(8):
        watcher.tick()
    assert w.state == "error" and "止损单仍在" in w.note
    assert [o.id for o in broker.open_orders("A")] == [stop.id]


def test_disarm_leaves_orders_alone(broker, watcher):
    w, stop = arm(broker, watcher)
    assert watcher.disarm(watch_id=w.id) and w.state == "cancelled"
    broker.set_price("NVDA", "120")
    watcher.tick()
    assert [o.id for o in broker.open_orders("A")] == [stop.id]


def test_watches_survive_restart(broker, watcher, cfg, clock):
    w, stop = arm(broker, watcher)
    again = Watcher(cfg, broker, threading.RLock(), clock=clock, sleep=lambda s: None, market_open=lambda: True)
    (w2,) = again.list()
    assert (w2.id, w2.state, w2.stop_order_id) == (w.id, "armed", stop.id)
    broker.set_price("NVDA", "92")
    again.tick()
    assert again.list()[0].state == "stopped"


def test_market_hours():
    from datetime import datetime, timezone
    def at(s):
        return is_market_open(datetime.fromisoformat(s).replace(tzinfo=timezone.utc))
    assert at("2026-11-24T14:30:00")       # 周二 9:30 纽约（冬令时 UTC-5）
    assert not at("2026-11-24T14:29:00")
    assert at("2026-11-24T20:59:00") and not at("2026-11-24T21:00:00")
    assert not at("2026-11-28T16:00:00")   # 周六
    assert at("2026-07-01T13:30:00")       # 夏令时 UTC-4


def test_stale_held_shares_after_cancel_does_not_look_like_no_position(broker, watcher):
    """刚撤完止损单时，券商报的「占用股数」可能还是旧的；不能因此当成没持仓、把保护丢掉。"""
    w, _ = arm(broker, watcher)
    broker.freeze = True
    broker.set_price("NVDA", "115.10")
    real = broker.positions

    def stale(account):
        ps = real(account)
        for p in ps:
            p.held_for_sells = p.quantity  # 还显示全部被占用
        return ps

    broker.positions = stale
    watcher.tick()
    assert w.state == "tp_placed" and broker.get_order("A", w.tp_order_id).quantity == D(10)


def test_target_keeps_failing_stops_retrying_and_leaves_stop_in_place(broker, watcher):
    w, _ = arm(broker, watcher)
    broker.set_price("NVDA", "115.10")
    real = broker.place_sell

    def no_limits(order):
        if order.kind == "limit":
            from tpsl.brokers.base import BrokerError
            raise BrokerError("模拟：限价单一直被拒")
        return real(order)

    broker.place_sell = no_limits
    for _ in range(6):
        watcher.tick()
    assert w.state == "error" and w.tp_fails == 3 and "止损单仍在" in w.note
    (o,) = broker.open_orders("A")
    assert o.kind == "stop" and o.id == w.stop_order_id and o.quantity == D(10)
