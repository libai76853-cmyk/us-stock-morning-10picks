import pytest

from conftest import snapshot
from tpsl.planner import PlanRequest, build_plan
from tpsl.security import FAIL_WINDOW, MAX_FAILS, PLAN_TTL, AccessGuard, PlanStore


def a_plan(broker):
    broker.set_position("A", "NVDA", "10", "100", "110")
    pos, orders = snapshot(broker)
    return build_plan(PlanRequest.from_json({"account": "A"}), pos, orders, broker.capabilities())


def test_plan_is_single_use(broker, clock):
    store = PlanStore("s3cret", clock)
    plan = a_plan(broker)
    tok = store.put(plan)["confirm_token"]
    assert store.take(plan.id, tok) is plan
    with pytest.raises(ValueError, match="已经用过"):
        store.take(plan.id, tok)


def test_wrong_token_burns_the_plan(broker, clock):
    store = PlanStore("s3cret", clock)
    plan = a_plan(broker)
    tok = store.put(plan)["confirm_token"]
    with pytest.raises(ValueError, match="确认码不对"):
        store.take(plan.id, "0" * 64)
    with pytest.raises(ValueError):
        store.take(plan.id, tok)


def test_plan_expires(broker, clock):
    store = PlanStore("s3cret", clock)
    plan = a_plan(broker)
    tok = store.put(plan)["confirm_token"]
    clock.t += PLAN_TTL + 1
    with pytest.raises(ValueError, match="过期"):
        store.take(plan.id, tok)


def test_plan_changed_after_preview_is_refused(broker, clock):
    store = PlanStore("s3cret", clock)
    plan = a_plan(broker)
    tok = store.put(plan)["confirm_token"]
    from decimal import Decimal
    from tpsl.models import SellOrder
    o = plan.items[0].orders[0]
    plan.items[0].orders[0] = SellOrder(o.account, o.symbol, o.quantity, o.kind, o.limit_price, Decimal("1"), o.tif, o.ref)
    with pytest.raises(ValueError, match="确认码不对"):
        store.take(plan.id, tok)


def test_access_guard_locks_after_repeated_failures(clock):
    g = AccessGuard("good", clock)
    assert g.check("good") and not g.check("bad") and not g.check(None) and not g.check("")
    for _ in range(MAX_FAILS):
        g.check("bad")
    assert g.locked() and not g.check("good")
    clock.t += FAIL_WINDOW + 1
    assert not g.locked() and g.check("good")
