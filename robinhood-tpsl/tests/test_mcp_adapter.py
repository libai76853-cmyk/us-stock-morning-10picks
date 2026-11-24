from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from tpsl.brokers import mcp_adapter as A
from tpsl.models import SellOrder


def tool(name, props, required=()):
    return {"name": name, "input_schema": {"type": "object", "properties": props, "required": list(required)}}


S = {"type": "string"}


def test_roles_from_published_names():
    names = ["get_portfolio", "get_equity_quotes", "review_equity_order", "place_equity_order",
             "cancel_equity_order", "get_equity_orders", "place_option_order", "get_crypto_quotes"]
    r = A.assign_roles(names)
    assert r == {"positions": "get_portfolio", "quotes": "get_equity_quotes", "review": "review_equity_order",
                 "place": "place_equity_order", "cancel": "cancel_equity_order", "open_orders": "get_equity_orders"}


def test_roles_never_pick_option_or_crypto_tools():
    r = A.assign_roles(["place_option_order", "place_crypto_order", "cancel_option_order", "get_crypto_quotes"])
    assert "place" not in r and "cancel" not in r and "quotes" not in r


def test_roles_ambiguous_keyword_match_is_not_guessed():
    r = A.assign_roles(["place_stock_order", "place_etf_order"])
    assert "place" not in r


def test_roles_override_and_bad_override():
    assert A.assign_roles(["a", "b"], {"place": "a"})["place"] == "a"
    with pytest.raises(A.MappingError):
        A.assign_roles(["a"], {"place": "zzz"})


def test_missing_required_argument_refuses():
    t = tool("place_equity_order", {"symbol": S, "quantity": S, "side": S, "stop_price": S, "client_token": S},
             required=["symbol", "client_token"])
    f = A.order_facts(SellOrder("x", "NVDA", D(1), "stop", stop_price=D("90")))
    with pytest.raises(A.MappingError, match="client_token"):
        A.build_args(t, f)
    # review 返回里有同名字段就能补上
    assert A.build_args(t, f, extra={"client_token": "abc"})["client_token"] == "abc"


def test_free_text_order_type_for_stop_is_not_guessed():
    t = tool("place_equity_order", {"symbol": S, "quantity": S, "side": S, "order_type": S, "stop_price": S},
             required=["order_type"])
    f = A.order_facts(SellOrder("x", "NVDA", D(1), "stop", stop_price=D("90")))
    with pytest.raises(A.MappingError, match="type.stop"):
        A.build_args(t, f)
    assert A.build_args(t, f, values={"type.stop": "stop_loss"})["order_type"] == "stop_loss"


def test_price_with_nowhere_to_go_refuses():
    t = tool("place_equity_order", {"symbol": S, "quantity": S, "side": S})
    f = A.order_facts(SellOrder("x", "NVDA", D(1), "stop", stop_price=D("90")))
    with pytest.raises(A.MappingError, match="stop_price"):
        A.build_args(t, f)


def test_side_enum_without_sell_refuses():
    t = tool("place_equity_order", {"symbol": S, "quantity": S, "side": {"type": "string", "enum": ["long", "short"]},
                                    "stop_price": S})
    f = A.order_facts(SellOrder("x", "NVDA", D(1), "stop", stop_price=D("90")))
    with pytest.raises(A.MappingError):
        A.build_args(t, f)


def test_nullable_schema_and_number_coercion():
    t = tool("place_equity_order", {
        "symbol": S, "side": S, "quantity": {"type": "integer"},
        "limit_price": {"anyOf": [{"type": "number"}, {"type": "null"}], "default": None}})
    args = A.build_args(t, A.order_facts(SellOrder("x", "NVDA", D(3), "limit", limit_price=D("12.50"))))
    assert args == {"symbol": "NVDA", "side": "sell", "quantity": 3, "limit_price": 12.5}


def test_supports_oco_needs_both_flat_prices():
    assert A.supports_oco(tool("place_advanced_order", {"take_profit_price": S, "stop_loss_price": S}))
    assert not A.supports_oco(tool("place_advanced_order", {"legs": {"type": "array"}}))
    assert not A.supports_oco(None)


def test_payload_prefers_structured_then_json_text():
    r = SimpleNamespace(structured_content={"result": [1, 2]}, content=[])
    assert A.payload_of(r) == [1, 2]
    r = SimpleNamespace(structured_content=None, content=[SimpleNamespace(text='{"a": 1}')])
    assert A.payload_of(r) == {"a": 1}
    r = SimpleNamespace(structured_content=None, content=[SimpleNamespace(text="plain words")])
    assert A.payload_of(r) == "plain words"


def test_parse_positions_shapes():
    nested = {"data": {"holdings": [{"ticker": "nvda", "shares": 2, "cost_basis": 300, "asset_class": "us_equity"},
                                    {"ticker": "SPY 250117C600", "shares": 1, "asset_class": "option"}]}}
    (p,) = A.parse_positions(nested, "acct")
    assert (p.symbol, p.quantity, p.avg_cost) == ("NVDA", D(2), D(150))
    assert A.parse_positions({"positions": []}, "acct") == []
    assert A.parse_positions("no positions", "acct") == []


def test_parse_quotes_keyed_by_symbol():
    q = A.parse_quotes({"NVDA": {"last_trade_price": "110.5"}, "AAPL": {"last_trade_price": "210"}})
    assert q["NVDA"].last == D("110.5") and q["AAPL"].last == D("210")


def test_parse_order_states_and_kinds():
    rows = [
        {"id": 1, "symbol": "A", "side": "sell", "type": "market", "trigger": "stop", "stop_price": "9", "state": "queued", "quantity": "1"},
        {"id": 2, "symbol": "A", "side": "sell", "type": "limit", "trigger": "immediate", "price": "11", "state": "filled", "quantity": "1"},
        {"id": 3, "symbol": "A", "side": "sell", "order_type": "STOP_LIMIT", "limit_price": 8.9, "stop_price": 9, "status": "PARTIALLY_FILLED", "quantity": 4, "filled_quantity": 1},
        {"id": 4, "symbol": "A", "side": "sell", "type": "limit", "price": "11", "state": "weird_new_state", "quantity": "1"},
    ]
    o = A.parse_orders({"orders": rows}, "acct")
    assert [(x.kind, x.state) for x in o] == [("stop", "open"), ("limit", "filled"),
                                               ("stop_limit", "partially_filled"), ("limit", "unknown")]
    assert o[0].limit_price is None and o[2].filled_quantity == D(1)
    assert not o[3].is_open and not o[3].is_final  # 认不出的状态两头都不算


def test_accounts_all_tradable_when_agentic_unidentifiable():
    accts = A.parse_accounts({"accounts": [{"account_number": "1"}, {"account_number": "2"}]})
    assert [a.tradable for a in accts] == [True, True]
