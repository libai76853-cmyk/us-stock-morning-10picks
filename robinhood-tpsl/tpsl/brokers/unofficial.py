"""非官方接口后端：通过 robin_stocks 走 Robinhood App 自己用的私有接口，能操作主账户。

要清楚的风险（README 里也写了）：
- 这不是 Robinhood 公开支持的接口，使用它违反用户协议，账户可能被要求反复验证或受限。
- 接口随时可能变；变了要等 robin_stocks 更新（pip install -U robin_stocks）。
- 默认不启用：要设 TPSL_BACKEND=unofficial，并先在 Mac 终端里登录一次。

登录只在终端里交互完成（密码不落盘，只保存 Robinhood 返回的会话令牌）。服务运行时只读
已保存的会话，过期了就报错让人重新登录，绝不会自己拿空密码去撞登录接口。

robin_stocks 的读接口会吞掉错误、也不设超时，所以这里读数据直接用它的会话发请求、自己查
状态码；下单用它的 order()，这样报文格式跟着库走。
"""

from __future__ import annotations

import os
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..models import ZERO, Account, Capabilities, Order, Position, Quote, SellOrder, dec
from ..watcher import is_market_open
from .base import Broker, BrokerAuthError, BrokerError, BrokerUncertain

_STATE = {
    "queued": "open", "unconfirmed": "open", "confirmed": "open", "new": "open", "pending": "open",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "cancelled": "cancelled", "canceled": "cancelled", "pending_cancelled": "open",
    "rejected": "rejected", "failed": "rejected", "voided": "rejected",
}


def session_dir(cfg: Config) -> Path:
    d = cfg.path("unofficial")
    d.mkdir(exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def login_interactive(cfg: Config) -> None:
    """在终端里登录一次。会提示输入用户名、密码，然后在手机 Robinhood App 里点批准。"""
    import robin_stocks.robinhood as rh

    d = session_dir(cfg)
    print("用 Robinhood 账号登录（密码不会保存，只保存登录后的会话令牌）。")
    print("输入后请留意手机上的 Robinhood App，点「批准」这次登录。")
    mfa = input("如果开了验证器 App 的两步验证，输入 6 位验证码；没有就直接回车：").strip() or None
    res = rh.login(store_session=True, pickle_path=str(d), mfa_code=mfa)
    p = d / "robinhood.pickle"
    if not res or not p.exists():
        raise SystemExit("登录没有成功。检查用户名密码，或稍后再试（短时间内反复登录可能触发风控）。")
    os.chmod(p, 0o600)
    print(f"登录成功，会话保存在 {p}")


class UnofficialBroker(Broker):
    name = "unofficial"
    label = "非官方接口"

    def __init__(self, cfg: Config, rh=None, timeout: float = 20.0):
        if rh is None:
            try:
                import robin_stocks.robinhood as rh  # type: ignore[no-redef]
            except ImportError as e:  # pragma: no cover
                raise BrokerError("没装 robin_stocks：pip install robin_stocks") from e
        self._rh = rh
        self._cfg = cfg
        self._timeout = timeout
        self._session = rh.globals.SESSION
        self._instrument_symbol: dict[str, str] = {}
        self._auth_error = ""
        self._install_default_timeout()
        try:
            self._restore_session()
        except BrokerAuthError as e:
            self._auth_error = str(e)

    # ---- 会话 ----
    def _install_default_timeout(self) -> None:
        """robin_stocks 内部的请求不带超时；给会话装一个默认超时，免得卡死。"""
        try:
            from requests.adapters import HTTPAdapter
        except ImportError:  # pragma: no cover
            return
        timeout = self._timeout

        class _Adapter(HTTPAdapter):
            def send(self, request, **kw):
                if kw.get("timeout") is None:
                    kw["timeout"] = timeout
                return super().send(request, **kw)

        if hasattr(self._session, "mount"):
            self._session.mount("https://", _Adapter())

    def _restore_session(self) -> None:
        p = session_dir(self._cfg) / "robinhood.pickle"
        if not p.exists():
            raise BrokerAuthError("还没登录：在 Mac 上运行 ./run.sh login unofficial")
        try:
            with open(p, "rb") as f:
                data = pickle.load(f)
            token = f"{data['token_type']} {data['access_token']}"
        except Exception as e:
            raise BrokerAuthError(f"会话文件读不出来，请重新登录：{e}") from e
        self._rh.helper.set_login_state(True)
        self._rh.helper.update_session("Authorization", token)

    def _check_auth(self) -> None:
        if self._auth_error:
            # 可能是刚重新登录过，再读一次
            try:
                self._restore_session()
                self._auth_error = ""
            except BrokerAuthError as e:
                self._auth_error = str(e)
                raise

    # ---- HTTP ----
    def _get(self, url: str, params: dict | None = None) -> dict:
        self._check_auth()
        try:
            r = self._session.get(url, params=params, timeout=self._timeout)
        except Exception as e:
            raise BrokerError(f"网络错误：{type(e).__name__}: {e}") from e
        if r.status_code == 401:
            self._auth_error = "登录已过期：在 Mac 上运行 ./run.sh login unofficial"
            raise BrokerAuthError(self._auth_error)
        if r.status_code >= 400:
            raise BrokerError(f"Robinhood 返回 HTTP {r.status_code}：{_snippet(r)}")
        try:
            return r.json()
        except ValueError as e:
            raise BrokerError("Robinhood 返回的不是 JSON") from e

    def _get_all(self, url: str, params: dict | None = None, max_pages: int = 30) -> list[dict]:
        data = self._get(url, params)
        out = list(data.get("results") or [])
        pages = 1
        while data.get("next") and pages < max_pages:
            data = self._get(data["next"])
            out.extend(data.get("results") or [])
            pages += 1
        return out

    # ---- Broker 接口 ----
    def capabilities(self) -> Capabilities:
        return Capabilities(
            native_oco=False,
            notes=["非官方接口：违反 Robinhood 用户协议，账户有被限制的风险",
                   "这个接口下不了 App 里的止盈/止损联动单；两头都要时用「盯盘」或「拆分」"],
        )

    def status(self) -> dict:
        if self._auth_error:
            return {"ok": False, "detail": self._auth_error}
        return {"ok": True, "detail": "已载入登录会话"}

    def accounts(self) -> list[Account]:
        rows = self._get_all(self._rh.urls.account_profile_url())
        out = []
        for a in rows:
            num = a.get("account_number")
            if not num or a.get("deactivated"):
                continue
            kind = a.get("nickname") or a.get("brokerage_account_type") or a.get("type") or "账户"
            out.append(Account(num, f"{kind} ···{num[-4:]}"))
        return out

    def positions(self, account: str) -> list[Position]:
        rows = self._get_all(self._rh.urls.positions_url(account_number=account), {"nonzero": "true"})
        out = []
        for row in rows:
            qty = dec(row.get("quantity"))
            if qty is None or qty <= 0:
                continue
            sym = row.get("symbol") or self._symbol_for(row.get("instrument"))
            if not sym:
                continue
            cost = dec(row.get("average_buy_price"))
            out.append(Position(account, sym.upper(), qty, cost if cost and cost > 0 else None,
                                dec(row.get("shares_held_for_sells")) or ZERO))
        return out

    def _symbol_for(self, instrument_url: str | None) -> str | None:
        if not instrument_url:
            return None
        if instrument_url not in self._instrument_symbol:
            self._instrument_symbol[instrument_url] = self._get(instrument_url).get("symbol") or ""
        return self._instrument_symbol[instrument_url] or None

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        regular = is_market_open()
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            data = self._get(self._rh.urls.quotes_url(), {"symbols": ",".join(chunk)})
            for q in data.get("results") or []:
                if not q:
                    continue
                last = dec(q.get("last_trade_price"))
                ext = dec(q.get("last_extended_hours_trade_price"))
                if not regular and ext:
                    last = ext  # 盘前盘后用最新的盘外成交价做校验
                out[q["symbol"].upper()] = Quote(q["symbol"].upper(), last,
                                                 dec(q.get("bid_price")), dec(q.get("ask_price")))
        return out

    def _to_order(self, row: dict, account: str) -> Order:
        typ, trig = row.get("type"), row.get("trigger")
        if trig == "stop":
            kind = "stop_limit" if typ == "limit" else "stop"
        else:
            kind = "limit" if typ == "limit" else "market"
        sym = row.get("symbol") or self._symbol_for(row.get("instrument")) or "?"
        raw_state = str(row.get("state") or "").lower()
        return Order(
            id=str(row.get("id")),
            account=row.get("account_number") or account,
            symbol=sym.upper(),
            kind=kind,
            side=row.get("side") or "",
            quantity=dec(row.get("quantity")) or ZERO,
            limit_price=dec(row.get("price")) if typ == "limit" else None,
            stop_price=dec(row.get("stop_price")),
            state=_STATE.get(raw_state, "unknown"),
            filled_quantity=dec(row.get("cumulative_quantity")) or ZERO,
            tif=row.get("time_in_force") or "",
            created_at=row.get("created_at") or "",
        )

    def open_orders(self, account: str) -> list[Order]:
        # GTC 单最长 90 天，往回看 100 天就够，不用翻完整个历史
        since = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d")
        rows = self._get_all(self._rh.urls.orders_url(account_number=account, start_date=since))
        out = []
        for row in rows:
            o = self._to_order(row, account)
            if o.is_open and o.side == "sell":
                out.append(o)
        return out

    def get_order(self, account: str, order_id: str) -> Order | None:
        if not order_id:
            return None
        try:
            row = self._get(self._rh.urls.orders_url(order_id))
        except BrokerAuthError:
            raise
        except BrokerError as e:
            if "HTTP 404" in str(e):
                return None
            raise
        return self._to_order(row, account)

    def place_sell(self, order: SellOrder) -> Order:
        order.validate()
        self._check_auth()
        if order.kind == "oco":
            raise BrokerError("非官方接口不支持止盈/止损联动单")
        qty = int(order.quantity)
        limit = float(order.limit_price) if order.kind == "limit" else None
        stop = float(order.stop_price) if order.kind == "stop" else None
        try:
            # jsonify=False：拿回原始响应，自己看状态码。order() 里只有最后一步是真正下单，
            # 且那一步的异常都被库吞掉返回 None；所以这里抛出来的异常一定发生在下单请求之前。
            res = self._rh.orders.order(order.symbol, qty, "sell", limitPrice=limit, stopPrice=stop,
                                        account_number=order.account, timeInForce=order.tif,
                                        extendedHours=False, jsonify=False)
        except Exception as e:
            raise BrokerError(f"下单前准备失败（没有发出）：{type(e).__name__}: {e}") from e
        if res is None:
            raise BrokerUncertain("下单请求没有拿到响应")
        code = getattr(res, "status_code", 0)
        if code == 401:
            self._auth_error = "登录已过期：在 Mac 上运行 ./run.sh login unofficial"
            raise BrokerAuthError(self._auth_error)
        if code >= 500:
            raise BrokerUncertain(f"Robinhood 返回 HTTP {code}")
        try:
            data = res.json()
        except ValueError:
            data = {}
        if code >= 400 or not isinstance(data, dict) or not data.get("id"):
            raise BrokerError(_error_text(data) or f"HTTP {code}：{_snippet(res)}")
        return self._to_order(data, order.account)

    def cancel(self, account: str, order_id: str) -> None:
        self._check_auth()
        try:
            r = self._session.post(self._rh.urls.cancel_url(order_id), timeout=self._timeout)
        except Exception as e:
            raise BrokerUncertain(f"撤单请求没有拿到响应：{type(e).__name__}") from e
        if r.status_code == 401:
            self._auth_error = "登录已过期：在 Mac 上运行 ./run.sh login unofficial"
            raise BrokerAuthError(self._auth_error)
        if r.status_code >= 400:
            raise BrokerError(f"撤单被拒 HTTP {r.status_code}：{_snippet(r)}")


def _snippet(r) -> str:
    try:
        return str(r.text)[:200]
    except Exception:
        return ""


def _error_text(data) -> str:
    if not isinstance(data, dict):
        return ""
    for k in ("detail", "non_field_errors", "error", "message"):
        v = data.get(k)
        if v:
            return "；".join(map(str, v)) if isinstance(v, list) else str(v)
    parts = [f"{k}: {v}" for k, v in data.items() if isinstance(v, (list, str)) and k != "id"]
    return "；".join(parts)[:300]
