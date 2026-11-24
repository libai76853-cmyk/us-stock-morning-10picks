"""官方接口的适配层：把「卖出 10 股 NVDA 止损 $130」翻译成 MCP 工具调用，再把返回读回来。

Robinhood 没有在这里能查到的公开文档写明每个工具的参数名，所以不写死：
- 工具按名字里的关键词认（get_portfolio / place_equity_order 这类）。
- 参数按工具自己声明的 JSON Schema 填：属性名对上已知的同义词就填，枚举值在枚举里挑。
- 必填参数有一个不知道怎么填，就报错、不发请求。宁可不下单，不乱猜。
- 都可以用 ~/.rh-tpsl/official_mapping.json 覆盖（见 README）。

这个文件全是纯函数，不联网，测试直接喂 schema 和返回值。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from ..models import ZERO, Account, Order, Position, Quote, SellOrder, dec, fmt


class MappingError(Exception):
    """认不出工具或参数。出现时一定没有发出下单请求。"""


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# ---------------------------------------------------------------- 工具角色

# 角色 -> 候选名字（按优先级）。没有精确命中时再按关键词找。
ROLE_NAMES: dict[str, list[str]] = {
    "accounts": ["get_accounts", "list_accounts", "get_account", "get_account_info"],
    "positions": ["get_portfolio", "get_positions", "get_equity_positions", "get_holdings", "list_positions"],
    "quotes": ["get_equity_quotes", "get_quotes", "get_equity_quote", "get_quote"],
    "open_orders": ["get_equity_orders", "list_equity_orders", "get_open_equity_orders", "get_orders",
                    "list_orders", "get_open_orders"],
    "get_order": ["get_equity_order", "get_order", "get_equity_order_status", "get_order_status"],
    "review": ["review_equity_order", "preview_equity_order"],
    "place": ["place_equity_order", "submit_equity_order", "create_equity_order"],
    "cancel": ["cancel_equity_order", "cancel_order"],
    "adv_review": ["review_advanced_order", "review_advanced_equity_order"],
    "adv_place": ["place_advanced_order", "place_advanced_equity_order"],
    "adv_cancel": ["cancel_advanced_order", "cancel_advanced_equity_order"],
    "adv_get": ["get_advanced_orders", "get_advanced_order", "list_advanced_orders"],
}
# 关键词兜底：(必须都包含, 不能包含)
ROLE_KEYWORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "accounts": (("account",), ("order", "position", "transfer")),
    "positions": (("position",), ("option", "crypto", "order")),
    "quotes": (("quote",), ("option", "crypto")),
    "open_orders": (("orders",), ("option", "crypto", "advanced", "place", "cancel", "review")),
    "review": (("review", "order"), ("option", "crypto", "advanced")),
    "place": (("place", "order"), ("option", "crypto", "advanced")),
    "cancel": (("cancel", "order"), ("option", "crypto", "advanced")),
    "adv_review": (("review", "advanced"), ("option", "crypto")),
    "adv_place": (("place", "advanced"), ("option", "crypto")),
    "adv_cancel": (("cancel", "advanced"), ("option", "crypto")),
    "adv_get": (("get", "advanced"), ("option", "crypto")),
}


def assign_roles(tool_names: list[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    by_norm = {norm(n): n for n in tool_names}
    roles: dict[str, str] = {}
    for role, cands in ROLE_NAMES.items():
        for c in cands:
            if norm(c) in by_norm:
                roles[role] = by_norm[norm(c)]
                break
        else:
            must, never = ROLE_KEYWORDS.get(role, ((), ()))
            if must:
                hits = [n for n in tool_names
                        if all(k in n.lower() for k in must) and not any(k in n.lower() for k in never)]
                if len(hits) == 1:  # 只有唯一命中才用，多个就不猜
                    roles[role] = hits[0]
    for role, name in (overrides or {}).items():
        if name in tool_names:
            roles[role] = name
        elif name in ("", None):
            roles.pop(role, None)
        else:
            raise MappingError(f"official_mapping.json 里 {role} 指向的工具 {name} 不存在")
    return roles


# ---------------------------------------------------------------- 填参数

SYN: dict[str, tuple[str, ...]] = {
    "account": ("accountnumber", "accountid", "account", "accountnum", "rhsaccountnumber"),
    "symbol": ("symbol", "ticker", "instrumentsymbol", "equitysymbol"),
    "symbols": ("symbols", "tickers"),
    "side": ("side", "direction", "action", "orderside"),
    "quantity": ("quantity", "qty", "shares", "sharequantity", "numshares", "sharecount"),
    "type": ("type", "ordertype"),
    "trigger": ("trigger", "triggertype"),
    "limit_price": ("limitprice", "price", "limit"),
    "stop_price": ("stopprice", "stop", "triggerprice", "stoptriggerprice"),
    "tif": ("timeinforce", "tif", "duration"),
    "tp_price": ("takeprofitprice", "takeprofit", "takeprofitlimitprice", "profitprice", "tpprice",
                 "profittargetprice", "takeprofitlimit"),
    "sl_price": ("stoplossprice", "stoploss", "stoplossstopprice", "slprice", "stoplosstriggerprice",
                 "stoplossstop"),
    "order_id": ("orderid", "id", "equityorderid", "advancedorderid"),
    "market_hours": ("markethours", "tradinghours", "session"),
    "extended_hours": ("extendedhours",),
}
# 这些事实是「价格」，在 OCO 工具里普通的 limit/stop 同义词也算止盈/止损价
_OCO_FALLBACK = {"tp_price": SYN["limit_price"], "sl_price": SYN["stop_price"]}

ENUM_PICKS: dict[str, tuple[str, ...]] = {
    "side.sell": ("sell", "sellorder", "sellside"),
    "type.limit": ("limit", "limitorder"),
    "type.market": ("market", "marketorder"),
    "type.stop": ("stopmarket", "stoploss", "stop", "stoplossmarket", "marketstop", "stoporder"),
    "type.oco": ("takeprofitstoploss", "tpsl", "oco", "onecancelsother", "takeprofitandstoploss", "bracket"),
    "trigger.stop": ("stop",),
    "trigger.immediate": ("immediate", "none"),
    "tif.gtc": ("gtc", "goodtillcanceled", "goodtilcanceled", "goodtillcancelled", "goodtilcancelled"),
    "tif.gfd": ("gfd", "day", "goodforday"),
    "market_hours.regular": ("regularhours", "regular", "rth"),
}


def _schema_of(prop: dict) -> dict:
    """处理 anyOf/oneOf [X, null] 这种可空写法，取非空的那个。"""
    for key in ("anyOf", "oneOf"):
        if key in prop:
            subs = [s for s in prop[key] if isinstance(s, dict) and s.get("type") != "null"]
            if len(subs) == 1:
                merged = {k: v for k, v in prop.items() if k not in ("anyOf", "oneOf")}
                merged.update(subs[0])
                return merged
    return prop


def _pick_enum(key: str, enum: list, values_override: dict[str, str]) -> Any:
    if key in values_override:
        return values_override[key]
    wanted = ENUM_PICKS.get(key, ())
    by_norm = {norm(e): e for e in enum}
    for w in wanted:
        if w in by_norm:
            return by_norm[w]
    raise MappingError(f"枚举 {enum} 里找不到 {key} 对应的取值")


def _coerce(value: Any, schema: dict) -> Any:
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if isinstance(value, Decimal):
        if t == "integer":
            return int(value)
        if t == "number":
            return float(value)
        return fmt(value)
    if t == "string" and not isinstance(value, str):
        return str(value)
    return value


def order_facts(order: SellOrder) -> dict[str, Any]:
    f: dict[str, Any] = {
        "account": order.account if order.account and order.account != "default" else None,
        "symbol": order.symbol,
        "symbols": [order.symbol],
        "side": "sell",
        "quantity": order.quantity,
        "tif": order.tif,
        "kind": order.kind,
        "extended_hours": False,
    }
    if order.kind == "limit":
        f["limit_price"] = order.limit_price
    elif order.kind == "stop":
        f["stop_price"] = order.stop_price
    elif order.kind == "oco":
        f["tp_price"] = order.limit_price
        f["sl_price"] = order.stop_price
    return f


def build_args(tool: dict, facts: dict[str, Any], *, fixed: dict | None = None,
               values: dict[str, str] | None = None, extra: dict | None = None) -> dict:
    """按工具的 inputSchema 填参数。

    tool: {"name", "input_schema"}；facts: order_facts() 或 {"order_id": ...} 之类；
    fixed: 映射文件里对这个工具写死的参数；extra: review 返回里可以拿来补必填项的字段。
    """
    schema = tool.get("input_schema") or {}
    props: dict[str, dict] = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    values = values or {}
    kind = facts.get("kind")
    has_trigger = any(norm(p) in SYN["trigger"] for p in props)
    args: dict[str, Any] = {}

    for pname, praw in props.items():
        ps = _schema_of(praw if isinstance(praw, dict) else {})
        n = norm(pname)
        enum = ps.get("enum")

        def put(v: Any) -> None:
            args[pname] = _coerce(v, ps)

        if n in SYN["side"]:
            put(_pick_enum("side.sell", enum, values) if enum else values.get("side.sell", "sell"))
        elif n in SYN["type"] and kind:
            # Robinhood 自家风格：type 只有 market/limit，止损靠 trigger=stop 表达
            k = kind
            if has_trigger and kind == "stop":
                k = "market"
            key = f"type.{k}"
            if enum:
                try:
                    put(_pick_enum(key, enum, values))
                except MappingError:
                    if pname in required:
                        raise
            elif key in values:
                put(values[key])
            elif k in ("limit", "market"):
                put(k)
            elif pname in required:
                raise MappingError(
                    f"{tool.get('name')} 的 {pname} 没有枚举，不知道止损单该填什么；"
                    f"请在 official_mapping.json 的 values 里写 \"{key}\"")
        elif n in SYN["trigger"] and kind:
            key = "trigger.stop" if kind == "stop" else "trigger.immediate"
            if enum:
                put(_pick_enum(key, enum, values))
            else:
                put(values.get(key, "stop" if kind == "stop" else "immediate"))
        elif n in SYN["tif"] and facts.get("tif"):
            key = f"tif.{facts['tif']}"
            put(_pick_enum(key, enum, values) if enum else values.get(key, facts["tif"]))
        elif n in SYN["market_hours"]:
            if enum:
                try:
                    put(_pick_enum("market_hours.regular", enum, values))
                except MappingError:
                    if pname in required:
                        raise
            elif "market_hours.regular" in values:
                put(values["market_hours.regular"])
        elif n in SYN["extended_hours"]:
            put(False)
        else:
            for fact in ("tp_price", "sl_price", "account", "symbols", "symbol", "quantity",
                         "limit_price", "stop_price", "order_id"):
                syns = SYN[fact]
                if kind == "oco" and fact in _OCO_FALLBACK:
                    syns = syns + _OCO_FALLBACK[fact]
                if n in syns and facts.get(fact) is not None:
                    v = facts[fact]
                    if ps.get("type") == "array" and not isinstance(v, list):
                        v = [v]
                    if isinstance(v, list) and ps.get("type") == "string":
                        v = ",".join(v)
                    put(v)
                    break

    if extra:
        for pname in required:
            if pname not in args and pname in extra and extra[pname] is not None:
                args[pname] = extra[pname]
    if fixed:
        args.update(fixed)
    missing = [p for p in required if p not in args and "default" not in _schema_of(props.get(p) or {})]
    if missing:
        raise MappingError(f"工具 {tool.get('name')} 的必填参数 {missing} 不知道怎么填，没有发出请求")
    # 事实里有价格但没有任何参数接住它：说明 schema 对不上，不能发
    for fact in ("limit_price", "stop_price", "tp_price", "sl_price", "symbol", "quantity"):
        if facts.get(fact) is None or kind is None:
            continue
        v = _as_dec(facts[fact]) if fact != "symbol" else facts[fact]
        if not any(_same(a, v) for a in args.values()):
            raise MappingError(f"工具 {tool.get('name')} 没有参数能接住 {fact}，没有发出请求")
    return args


def _as_dec(v: Any) -> Any:
    return v if isinstance(v, Decimal) else dec(v)


def _same(arg: Any, v: Any) -> bool:
    if isinstance(arg, list):
        return any(_same(a, v) for a in arg)
    if isinstance(v, Decimal):
        d = dec(arg)
        return d is not None and d == v
    return str(arg) == str(v)


def supports_oco(tool: dict | None) -> bool:
    """advanced order 工具的 schema 里同时有止盈价和止损价两个平铺的参数，才算支持联动单。"""
    if not tool:
        return False
    props = (tool.get("input_schema") or {}).get("properties") or {}
    names = {norm(p) for p in props}
    return bool(names & set(SYN["tp_price"])) and bool(names & set(SYN["sl_price"]))


# ---------------------------------------------------------------- 读返回

def payload_of(result: Any) -> Any:
    """从 CallToolResult 里取数据：优先结构化内容，否则把文本当 JSON 解析。"""
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        # FastMCP 风格会把非对象的返回包成 {"result": ...}
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    texts = [getattr(c, "text", None) for c in (getattr(result, "content", None) or [])]
    texts = [t for t in texts if isinstance(t, str)]
    for t in texts:
        try:
            return json.loads(t)
        except ValueError:
            continue
    return "\n".join(texts)


def error_text(result: Any) -> str:
    p = payload_of(result)
    if isinstance(p, dict):
        for k in ("error", "message", "detail", "reason"):
            if p.get(k):
                return str(p[k])[:400]
    return (p if isinstance(p, str) else json.dumps(p, ensure_ascii=False, default=str))[:400]


def _get(rec: dict, names: tuple[str, ...]) -> Any:
    by = {norm(k): v for k, v in rec.items()}
    for n in names:
        if n in by and by[n] not in (None, ""):
            return by[n]
    return None


def find_records(payload: Any, need: tuple[tuple[str, ...], ...], depth: int = 0) -> list[dict]:
    """在返回里找「一组记录」：第一个元素都是对象、且含有所需字段的列表。"""
    if depth > 6:
        return []
    if isinstance(payload, list):
        dicts = [x for x in payload if isinstance(x, dict)]
        if dicts and all(_get(d, names) is not None for d in dicts[:3] for names in need):
            return dicts
        for x in payload:
            got = find_records(x, need, depth + 1)
            if got:
                return got
        return []
    if isinstance(payload, dict):
        if all(_get(payload, names) is not None for names in need):
            return [payload]
        for v in payload.values():
            got = find_records(v, need, depth + 1)
            if got:
                return got
    return []


F_SYMBOL = ("symbol", "ticker", "instrumentsymbol")
F_QTY = ("quantity", "qty", "shares", "sharequantity", "totalquantity")
F_AVG = ("averagebuyprice", "averagecost", "avgcost", "averageprice", "avgprice", "costbasispershare",
         "averagecostpershare", "avgbuyprice")
F_TOTAL_COST = ("costbasis", "totalcost", "totalcostbasis")
F_HELD = ("sharesheldforsells", "heldforsells", "sharesheldfororders", "heldfororders")
F_LAST = ("lasttradeprice", "lastprice", "currentprice", "price", "last", "markprice", "marketprice", "mark")
F_ASSET = ("assettype", "assetclass", "instrumenttype", "securitytype", "type")
F_ID = ("id", "orderid")
F_SIDE = ("side", "direction")
F_OTYPE = ("type", "ordertype")
F_TRIGGER = ("trigger", "triggertype")
F_LIMIT = ("limitprice", "price", "takeprofitprice")
F_STOP = ("stopprice", "triggerprice", "stoplossprice")
F_STATE = ("state", "status", "orderstate", "orderstatus")
F_FILLED = ("cumulativequantity", "filledquantity", "executedquantity", "filledqty")
F_ACCT = ("accountnumber", "accountid", "account", "id")

_STATE = {
    "queued": "open", "unconfirmed": "open", "confirmed": "open", "new": "open", "pending": "open",
    "open": "open", "working": "open", "accepted": "open", "received": "open", "active": "open",
    "partiallyfilled": "partially_filled", "partial": "partially_filled",
    "filled": "filled", "executed": "filled", "completed": "filled",
    "cancelled": "cancelled", "canceled": "cancelled", "expired": "cancelled",
    "rejected": "rejected", "failed": "rejected", "voided": "rejected", "error": "rejected",
}


def parse_positions(payload: Any, account: str) -> list[Position]:
    out = []
    for r in find_records(payload, (F_SYMBOL, F_QTY)):
        asset = norm(_get(r, F_ASSET) or "")
        if any(k in asset for k in ("option", "crypto", "future", "cash")):
            continue
        qty = dec(_get(r, F_QTY))
        sym = _get(r, F_SYMBOL)
        if qty is None or qty <= 0 or not isinstance(sym, str):
            continue
        avg = dec(_get(r, F_AVG))
        if avg is None:
            total = dec(_get(r, F_TOTAL_COST))
            avg = total / qty if total and total > 0 else None
        out.append(Position(account, sym.upper(), qty, avg if avg and avg > 0 else None,
                            dec(_get(r, F_HELD)) or ZERO, dec(_get(r, F_LAST))))
    return out


def parse_quotes(payload: Any) -> dict[str, Quote]:
    out: dict[str, Quote] = {}
    recs = find_records(payload, (F_SYMBOL, F_LAST))
    if not recs and isinstance(payload, dict):  # {"NVDA": {...}} 这种按代码做键的
        for k, v in payload.items():
            if isinstance(v, dict) and _get(v, F_LAST) is not None:
                recs.append({"symbol": k, **v})
    for r in recs:
        sym = _get(r, F_SYMBOL)
        if isinstance(sym, str):
            out[sym.upper()] = Quote(sym.upper(), dec(_get(r, F_LAST)),
                                     dec(_get(r, ("bidprice", "bid"))), dec(_get(r, ("askprice", "ask"))))
    return out


def parse_order(r: dict, account: str) -> Order | None:
    oid = _get(r, F_ID)
    sym = _get(r, F_SYMBOL)
    if oid is None or not isinstance(sym, str):
        return None
    typ, trig = norm(_get(r, F_OTYPE) or ""), norm(_get(r, F_TRIGGER) or "")
    tp, sl = dec(_get(r, ("takeprofitprice",))), dec(_get(r, ("stoplossprice",)))
    limit, stop = dec(_get(r, F_LIMIT)), dec(_get(r, F_STOP))
    if (tp and sl) or any(k in typ for k in ("takeprofit", "oco", "bracket", "tpsl")):
        kind, limit, stop = "oco", tp or limit, sl or stop
    elif "stop" in typ or trig == "stop":
        kind = "stop_limit" if "limit" in typ else "stop"
        if kind == "stop":
            limit = None
    elif "limit" in typ:
        kind = "limit"
    elif "market" in typ:
        kind, limit = "market", None
    else:
        kind = "other"
    return Order(
        id=str(oid), account=account, symbol=sym.upper(), kind=kind,
        side=norm(_get(r, F_SIDE) or "sell"),
        quantity=dec(_get(r, F_QTY)) or ZERO, limit_price=limit, stop_price=stop,
        state=_STATE.get(norm(_get(r, F_STATE) or ""), "unknown"),
        filled_quantity=dec(_get(r, F_FILLED)) or ZERO,
        tif=norm(_get(r, ("timeinforce", "tif")) or ""),
        created_at=str(_get(r, ("createdat", "submittedat", "createdtime")) or ""),
    )


def parse_orders(payload: Any, account: str) -> list[Order]:
    out = []
    for r in find_records(payload, (F_ID, F_SYMBOL)):
        o = parse_order(r, account)
        if o:
            out.append(o)
    return out


def parse_accounts(payload: Any) -> list[Account]:
    out = []
    for r in find_records(payload, (("accountnumber", "accountid"),)) or find_records(payload, (F_ACCT,)):
        num = _get(r, F_ACCT)
        if num is None:
            continue
        num = str(num)
        label = _get(r, ("nickname", "name", "displayname", "brokerageaccounttype", "accounttype", "type")) or "账户"
        blob = norm(json.dumps(r, ensure_ascii=False, default=str))
        agentic = "agent" in blob or "mcp" in blob
        out.append(Account(num, f"{label} ···{num[-4:]}", tradable=agentic))
    if out and not any(a.tradable for a in out):
        # 认不出哪个是 Agentic 账户：都标成可交易，让 Robinhood 自己拒绝不该动的那个
        for a in out:
            a.tradable = True
    return out
