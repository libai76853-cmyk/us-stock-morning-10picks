"""官方接口后端：Robinhood Agentic Trading（MCP 服务，OAuth 登录，不存密码）。

- 地址默认 https://agent.robinhood.com/mcp/trading。
- 只能在 Robinhood 里单独开的 Agentic 账户下单；其他账户只读。
- 登录：在 Mac 上运行 ./run.sh login official，浏览器里授权一次，令牌存在 ~/.rh-tpsl。
  服务运行时令牌过期会自动续；续不了就报「需要重新登录」，不会自己弹浏览器。

MCP 的协议收发、OAuth 都交给官方 Python SDK（mcp 包）。工具名和参数的对应在 mcp_adapter.py。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable
from urllib.parse import parse_qs, urlparse

from ..config import Config, write_private
from ..models import Account, Capabilities, Order, Position, Quote, SellOrder
from . import mcp_adapter as A
from .base import Broker, BrokerAuthError, BrokerError, BrokerUncertain


class NeedLogin(Exception):
    pass


# ---------------------------------------------------------------- OAuth

class FileTokenStorage:
    """SDK 要的令牌存储：一个 600 权限的 JSON 文件。另外记下过期时间，重启后才知道该先续期。"""

    def __init__(self, path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, d: dict) -> None:
        write_private(self.path, json.dumps(d, ensure_ascii=False, indent=1))

    def expires_at(self) -> float | None:
        return self._read().get("expires_at")

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        d = self._read().get("tokens")
        return OAuthToken.model_validate(d) if d else None

    async def set_tokens(self, tokens) -> None:
        d = self._read()
        d["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        d["expires_at"] = time.time() + tokens.expires_in - 30 if tokens.expires_in else None
        self._write(d)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        d = self._read().get("client")
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def set_client_info(self, client_info) -> None:
        d = self._read()
        d["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(d)


def build_oauth(cfg: Config, *, interactive: bool):
    """interactive=True 只在终端登录命令里用：打开浏览器，在本机回环地址上等回调。"""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    storage = FileTokenStorage(cfg.path("official_oauth.json"))
    redirect = cfg.oauth_redirect
    holder: dict[str, Any] = {}

    async def redirect_handler(url: str) -> None:
        if not interactive:
            raise NeedLogin()
        import webbrowser

        u = urlparse(redirect)
        holder["future"] = asyncio.get_running_loop().create_future()

        async def on_conn(reader, writer):
            try:
                line = (await reader.readline()).decode("latin-1")
                while (await reader.readline()).strip():
                    pass
                path = line.split(" ")[1] if " " in line else ""
                qs = parse_qs(urlparse(path).query)
                ok = "code" in qs
                body = ("<meta charset=utf-8><h2>授权完成，可以关掉这个页面，回到终端。</h2>" if ok
                        else "<meta charset=utf-8><h2>没有收到授权码。</h2>").encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                             b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
                if urlparse(path).path == (u.path or "/") and not holder["future"].done():
                    if ok:
                        holder["future"].set_result(qs)
                    elif "error" in qs:
                        holder["future"].set_exception(
                            BrokerAuthError(f"授权被拒绝：{qs.get('error_description', qs['error'])[0]}"))
            finally:
                writer.close()

        holder["server"] = await asyncio.start_server(on_conn, host=u.hostname or "localhost", port=u.port or 80)
        print("\n在浏览器里完成 Robinhood 授权（没有自动打开的话，把下面的地址贴进浏览器）：\n")
        print(url + "\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    async def callback_handler():
        try:
            qs = await asyncio.wait_for(holder["future"], timeout=600)
        finally:
            holder["server"].close()
        return AuthorizationCodeResult(code=qs["code"][0], state=(qs.get("state") or [None])[0],
                                       iss=(qs.get("iss") or [None])[0])

    meta = OAuthClientMetadata(
        client_name="rh-tpsl (personal take-profit / stop-loss tool)",
        redirect_uris=[redirect],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    provider = OAuthClientProvider(
        server_url=cfg.official_url,
        client_metadata=meta,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    # SDK 重启后不知道旧令牌何时过期，会一直用到被拒再走完整授权；这里把记下的过期时间告诉它，
    # 这样过期了会先用 refresh token 续期。
    exp = storage.expires_at()
    if exp:
        provider.context.token_expiry_time = exp
    return provider


def http_opener(cfg: Config, *, interactive: bool = False) -> Callable[[], Any]:
    """返回一个「打开到 Robinhood MCP 的连接」的异步上下文管理器工厂。"""

    @asynccontextmanager
    async def opener() -> AsyncIterator[Any]:
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        from mcp.types import Implementation

        auth = build_oauth(cfg, interactive=interactive)
        async with create_mcp_http_client(auth=auth) as http:
            transport = streamable_http_client(cfg.official_url, http_client=http)
            async with Client(transport, client_info=Implementation(name="rh-tpsl", version="0.1.0"),
                              read_timeout_seconds=40) as client:
                yield client

    return opener


# ---------------------------------------------------------------- 连接

class McpLink:
    """给同步代码用的 MCP 连接：后台线程里跑事件循环，连接空闲一会儿自动断开，下次用时重连。"""

    def __init__(self, opener: Callable[[], Any], idle_seconds: float = 90.0):
        self._opener = opener
        self._idle = idle_seconds
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="tpsl-mcp", daemon=True)
        self._thread.start()
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._close: asyncio.Event | None = None
        self._open_error: BaseException | None = None
        self._last_used = 0.0

    def _submit(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise

    async def _session(self) -> None:
        try:
            async with self._opener() as client:
                self._client = client
                self._ready.set()  # type: ignore[union-attr]
                await self._close.wait()  # type: ignore[union-attr]
        except BaseException as e:
            self._open_error = e
        finally:
            self._client = None
            self._ready.set()  # type: ignore[union-attr]

    async def _ensure(self) -> Any:
        if self._client is None or self._task is None or self._task.done():
            self._ready, self._close, self._open_error = asyncio.Event(), asyncio.Event(), None
            self._task = asyncio.create_task(self._session())
            await self._ready.wait()
            if self._client is None:
                raise self._open_error or RuntimeError("连接没有建立")
        self._last_used = time.monotonic()
        self._loop.call_later(self._idle + 1, self._reap)
        return self._client

    def _reap(self) -> None:
        if self._client is not None and time.monotonic() - self._last_used >= self._idle and self._close:
            self._close.set()

    async def _drop(self) -> None:
        if self._close:
            self._close.set()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 5)
            except Exception:
                pass

    async def _call(self, name: str, args: dict | None) -> Any:
        client = await self._ensure()
        try:
            return await client.call_tool(name, args or {})
        except BaseException:
            await self._drop()  # 连接可能坏了，下次重连
            raise

    async def _tools(self) -> list:
        client = await self._ensure()
        try:
            out, cursor = [], None
            while True:
                res = await client.list_tools(cursor=cursor)
                out.extend(res.tools)
                cursor = res.next_cursor
                if not cursor:
                    return out
        except BaseException:
            await self._drop()
            raise

    def call(self, name: str, args: dict | None = None, timeout: float = 60.0) -> Any:
        return self._submit(self._call(name, args), timeout)

    def list_tools(self, timeout: float = 60.0) -> list:
        return self._submit(self._tools(), timeout)

    def close(self) -> None:
        try:
            self._submit(self._drop(), 8)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def _is_need_login(e: BaseException) -> bool:
    seen = 0
    while e is not None and seen < 8:
        if isinstance(e, NeedLogin) or type(e).__name__ in ("OAuthFlowError", "OAuthTokenError",
                                                          "OAuthRegistrationError"):
            return True
        subs = getattr(e, "exceptions", None)
        if subs and any(_is_need_login(s) for s in subs):
            return True
        e = e.__cause__ or e.__context__  # type: ignore[assignment]
        seen += 1
    return False


LOGIN_HINT = "需要登录：在 Mac 上运行 ./run.sh login official"


# ---------------------------------------------------------------- Broker

class OfficialBroker(Broker):
    name = "official"
    label = "官方接口"

    def __init__(self, cfg: Config, opener: Callable[[], Any] | None = None):
        self._cfg = cfg
        self._link = McpLink(opener or http_opener(cfg))
        self._tools: dict[str, dict] | None = None
        self._roles: dict[str, str] = {}
        self._mapping = self._load_mapping()
        self._error = ""

    def _load_mapping(self) -> dict:
        p = self._cfg.path("official_mapping.json")
        if not p.exists():
            return {}
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except ValueError as e:
            raise BrokerError(f"official_mapping.json 不是合法 JSON：{e}") from e

    # ---- 底层 ----
    def _discover(self) -> None:
        if self._tools is not None:
            return
        try:
            tools = self._link.list_tools()
        except BaseException as e:
            raise self._wrap(e, mutating=False) from e
        self._tools = {t.name: {"name": t.name, "description": t.description or "",
                                "input_schema": t.input_schema or {}} for t in tools}
        try:
            self._roles = A.assign_roles(list(self._tools), self._mapping.get("tools"))
        except A.MappingError as e:
            raise BrokerError(str(e)) from e
        self._error = ""

    def _wrap(self, e: BaseException, *, mutating: bool) -> BrokerError:
        if _is_need_login(e):
            self._error = LOGIN_HINT
            return BrokerAuthError(LOGIN_HINT)
        msg = f"{type(e).__name__}: {e}"[:300]
        # 服务器明确说「请求不合法 / 没有这个工具 / 参数不对」：一定没有执行
        if type(e).__name__ == "MCPError" and getattr(e, "code", None) in (-32600, -32601, -32602):
            return BrokerError(f"Robinhood 接口拒绝了请求：{e}")
        if mutating:
            return BrokerUncertain(f"请求发出后没有拿到结果（{msg}）")
        self._error = f"连接 Robinhood 失败：{msg}"
        return BrokerError(self._error)

    def _tool(self, role: str) -> dict | None:
        self._discover()
        name = self._roles.get(role)
        return self._tools.get(name) if name and self._tools else None

    def _call(self, role: str, facts: dict, *, mutating: bool = False, extra: dict | None = None) -> Any:
        tool = self._tool(role)
        if tool is None:
            raise BrokerError(f"Robinhood 接口里没找到做「{role}」的工具；运行 ./run.sh probe 看自检结果")
        try:
            args = A.build_args(tool, facts, fixed=(self._mapping.get("fixed_args") or {}).get(tool["name"]),
                                values=self._mapping.get("values"), extra=extra)
        except A.MappingError as e:
            raise BrokerError(str(e)) from e
        if mutating:
            self._cfg.audit("mcp_call", tool=tool["name"], args=args)
        try:
            res = self._link.call(tool["name"], args)
        except BaseException as e:
            raise self._wrap(e, mutating=mutating) from e
        if getattr(res, "is_error", False):
            raise BrokerError(f"{tool['name']}：{A.error_text(res)}")
        self._error = ""
        return A.payload_of(res)

    # ---- Broker 接口 ----
    def capabilities(self) -> Capabilities:
        notes = ["只能在 Agentic 账户里下单，其他账户只读"]
        try:
            self._discover()
        except BrokerError as e:
            return Capabilities(native_oco=False, order_status=False, notes=notes + [str(e)])
        oco = A.supports_oco(self._tool("adv_place"))
        if self._roles.get("adv_place") and not oco:
            notes.append("发现了 advanced order 工具，但参数结构认不出，联动单暂不可用")
        status = "get_order" in self._roles
        if not status:
            notes.append("接口里没有按订单号查状态的工具，「盯盘」不可用")
        return Capabilities(native_oco=oco, market_sell=True, order_status=status, notes=notes)

    def status(self) -> dict:
        if self._error:
            return {"ok": False, "detail": self._error}
        if self._tools is None:
            return {"ok": True, "detail": "还没连接"}
        missing = [r for r in ("positions", "quotes", "place", "cancel") if r not in self._roles]
        if missing:
            return {"ok": False, "detail": f"接口里缺这些工具：{missing}；运行 ./run.sh probe"}
        return {"ok": True, "detail": f"已连接，{len(self._tools)} 个工具"}

    def accounts(self) -> list[Account]:
        if self._tool("accounts") is None:
            return [Account("default", "Agentic 账户")]
        accts = A.parse_accounts(self._call("accounts", {}))
        return accts or [Account("default", "Agentic 账户")]

    def _acct(self, account: str) -> str | None:
        return None if account in ("", "default") else account

    def positions(self, account: str) -> list[Position]:
        return A.parse_positions(self._call("positions", {"account": self._acct(account)}), account)

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        tool = self._tool("quotes")
        props = {A.norm(p) for p in ((tool or {}).get("input_schema") or {}).get("properties") or {}}
        if props & set(A.SYN["symbols"]) or not props & set(A.SYN["symbol"]):
            return A.parse_quotes(self._call("quotes", {"symbols": symbols}))
        out: dict[str, Quote] = {}
        for s in symbols:  # 这个工具一次只收一只
            out.update(A.parse_quotes(self._call("quotes", {"symbol": s})))
        return out

    def open_orders(self, account: str) -> list[Order]:
        orders: list[Order] = []
        for role in ("open_orders", "adv_get"):
            if self._tool(role) is not None:
                orders += A.parse_orders(self._call(role, {"account": self._acct(account)}), account)
        seen: set[str] = set()
        out = []
        for o in orders:
            if o.id not in seen and o.is_open and o.side in ("sell", ""):
                seen.add(o.id)
                out.append(o)
        return out

    def get_order(self, account: str, order_id: str) -> Order | None:
        if not order_id:
            return None
        if self._tool("get_order") is not None:
            recs = A.parse_orders(self._call("get_order", {"account": self._acct(account), "order_id": order_id}),
                                  account)
            return next((o for o in recs if o.id == order_id), recs[0] if len(recs) == 1 else None)
        # 没有按号查的工具：只能看它还在不在挂单里
        for o in self.open_orders(account):
            if o.id == order_id:
                return o
        return Order(order_id, account, "?", "other", quantity=A.ZERO, state="gone")

    def place_sell(self, order: SellOrder) -> Order:
        order.validate()
        adv = order.kind == "oco"
        if adv and not A.supports_oco(self._tool("adv_place")):
            raise BrokerError("官方接口当前不支持止盈/止损联动单")
        facts = A.order_facts(order)
        facts["account"] = self._acct(order.account)
        extra = None
        review_role = "adv_review" if adv else "review"
        if self._tool(review_role) is not None:
            rv = self._call(review_role, facts)  # Robinhood 自己的预检；不通过会抛 BrokerError
            extra = rv if isinstance(rv, dict) else None
        data = self._call("adv_place" if adv else "place", facts, mutating=True, extra=extra)
        recs = A.parse_orders(data, order.account)
        if recs:
            o = recs[0]
            if o.state == "unknown":
                o.state = "open"
            if o.kind == "other":
                o.kind = order.kind
            return o
        oid = None
        if isinstance(data, dict):
            oid = data.get("id") or data.get("order_id") or data.get("orderId")
        if not oid:
            raise BrokerUncertain(f"下单返回里没有订单号：{str(data)[:200]}")
        return Order(str(oid), order.account, order.symbol, order.kind, order.quantity,
                     limit_price=order.limit_price, stop_price=order.stop_price, state="open", tif=order.tif)

    def cancel(self, account: str, order_id: str) -> None:
        role = "cancel"
        if self._tool("adv_cancel") is not None and self._tool("adv_get") is not None:
            adv_ids = {o.id for o in A.parse_orders(self._call("adv_get", {"account": self._acct(account)}), account)}
            if order_id in adv_ids:
                role = "adv_cancel"
        self._call(role, {"account": self._acct(account), "order_id": order_id}, mutating=True)

    def close(self) -> None:
        self._link.close()

    # ---- 自检 ----
    def probe(self) -> dict:
        """只读自检：列出工具、认出的角色、各读接口解析出了什么。不调用任何下单/撤单工具。"""
        self._discover()
        assert self._tools is not None
        rep: dict[str, Any] = {
            "tools": [{"name": t["name"], "description": t["description"][:120],
                       "input_schema": t["input_schema"]} for t in self._tools.values()],
            "roles": self._roles,
            "checks": [],
        }

        def check(name: str, fn: Callable[[], Any]) -> Any:
            try:
                v = fn()
                rep["checks"].append({"name": name, "ok": True, "result": v})
                return v
            except Exception as e:
                rep["checks"].append({"name": name, "ok": False, "result": f"{type(e).__name__}: {e}"})
                return None

        accts = check("账户", lambda: [a.to_json() for a in self.accounts()]) or []
        acct = next((a["id"] for a in accts if a["tradable"]), accts[0]["id"] if accts else "default")
        pos = check("持仓", lambda: [p.to_json() for p in self.positions(acct)]) or []
        syms = [p["symbol"] for p in pos][:3] or ["SPY"]
        check("行情", lambda: {s: A.fmt(q.last) for s, q in self.quotes(syms).items()})
        check("挂单", lambda: [o.to_json() for o in self.open_orders(acct)])
        sample = SellOrder(acct, syms[0], A.dec("1"), "limit", limit_price=A.dec("999999"))  # type: ignore[arg-type]
        sample_stop = SellOrder(acct, syms[0], A.dec("1"), "stop", stop_price=A.dec("0.01"))  # type: ignore[arg-type]
        for label, od in (("限价卖单的参数（未发送）", sample), ("止损卖单的参数（未发送）", sample_stop)):
            def build(od=od):
                tool = self._tool("place")
                if tool is None:
                    raise BrokerError("没找到下单工具")
                f = A.order_facts(od)
                f["account"] = self._acct(od.account)
                # review 返回的字段这里拿不到，所以只看不依赖 review 的必填项
                return A.build_args(tool, f, fixed=(self._mapping.get("fixed_args") or {}).get(tool["name"]),
                                    values=self._mapping.get("values"))
            check(label, build)
        caps = self.capabilities()
        rep["capabilities"] = caps.to_json()
        rep["ok"] = all(c["ok"] for c in rep["checks"]) and all(
            r in self._roles for r in ("positions", "quotes", "place", "cancel"))
        return rep
