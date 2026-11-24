"""网页服务：手机页面 + 一组 JSON 接口。

所有 /api 请求都要带访问口令（Authorization: Bearer ...）。口令首次启动时生成，
存在 ~/.rh-tpsl/access_token。下单分两步：/api/preview 生成计划，/api/execute 带着
计划的确认码执行；确认码 3 分钟内有效、只能用一次。
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .brokers import Broker, BrokerAuthError, BrokerError, make_broker
from .config import Config
from .executor import cancel_orders, execute
from .picks import load_picks
from .planner import PlanError, PlanRequest, build_plan
from .security import AccessGuard, PlanStore
from .watcher import Watcher, is_market_open

STATIC = Path(__file__).parent / "static"
BACKEND_LABEL = {"demo": "演示", "official": "官方接口", "unofficial": "非官方接口"}


def create_app(cfg: Config | None = None, broker: Broker | None = None, *, start_watcher: bool = True) -> FastAPI:
    cfg = cfg or Config()
    broker = broker or make_broker(cfg)
    lock = threading.RLock()  # 所有券商调用串行，盯盘线程和网页请求不会交叉
    demo = cfg.backend == "demo"
    watcher = Watcher(cfg, broker, lock, market_open=(lambda: True) if demo else is_market_open,
                      persist=not demo)
    guard = AccessGuard(cfg.secret("access_token"))
    plans = PlanStore(cfg.secret("plan_secret"))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_watcher:
            watcher.start()
        yield
        watcher.stop()
        broker.close()

    app = FastAPI(title="rh-tpsl", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.cfg, app.state.broker, app.state.watcher, app.state.lock = cfg, broker, watcher, lock

    def auth(request: Request) -> None:
        if guard.locked():
            raise HTTPException(429, "口令错误次数太多，10 分钟后再试")
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else None
        if not guard.check(token):
            raise HTTPException(401, "访问口令不对")

    @app.exception_handler(BrokerAuthError)
    async def _auth_err(_: Request, e: BrokerAuthError):
        return JSONResponse({"error": str(e), "need_login": True}, status_code=503)

    @app.exception_handler(BrokerError)
    async def _broker_err(_: Request, e: BrokerError):
        return JSONResponse({"error": str(e)}, status_code=502)

    @app.exception_handler(PlanError)
    async def _plan_err(_: Request, e: PlanError):
        return JSONResponse({"error": str(e)}, status_code=400)

    # ---- 静态文件（不含任何账户数据，不需要口令）----
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg")
    def icon():
        return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")

    @app.get("/apple-touch-icon.png")
    def touch_icon():
        return FileResponse(STATIC / "apple-touch-icon.png", media_type="image/png")

    # ---- 接口 ----
    cache: dict = {}  # 预览时连点几下选项不用每次都重新向券商取数；下单前 executor 一定重新取

    def _snapshot(account: str, max_age: float = 0.0):
        """账户表 + 持仓（带现价）+ 挂单。调用方已持有锁。"""
        hit = cache.get(account)
        if hit and max_age and time.monotonic() - hit[0] <= max_age:
            return hit[1]
        accounts = broker.accounts()
        positions = broker.positions(account) if account else []
        quotes = broker.quotes([p.symbol for p in positions]) if positions else {}
        for p in positions:
            q = quotes.get(p.symbol)
            if q and q.last:
                p.last_price = q.last
        snap = (accounts, positions, broker.open_orders(account) if account else [])
        cache.clear()
        cache[account] = (time.monotonic(), snap)
        return snap

    def _meta() -> dict:
        return {
            "backend": {"name": cfg.backend, "label": BACKEND_LABEL.get(cfg.backend, cfg.backend),
                        "live": not cfg.dry_run, "dry_run": cfg.dry_run, **broker.status()},
            "version": __version__,
            "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "market_open": is_market_open(),
            "watcher": {"interval": cfg.watch_interval, "last_tick": watcher.last_tick,
                        "last_error": watcher.last_error},
        }

    @app.get("/api/state", dependencies=[Depends(auth)])
    def state(account: str | None = None):
        with lock:
            meta = _meta()
            try:
                accounts = broker.accounts()
                caps = broker.capabilities()
            except BrokerError as e:
                meta["backend"].update(broker.status())
                return JSONResponse({**meta, "error": str(e), "need_login": isinstance(e, BrokerAuthError),
                                     "accounts": [], "positions": [], "orders": [], "watches": []},
                                    status_code=200)
            ids = [a.id for a in accounts]
            if account not in ids:
                account = next((a.id for a in accounts if a.tradable), ids[0] if ids else None)
            _, positions, orders = _snapshot(account) if account else (None, [], [])
            picks = load_picks(cfg.picks_file)
            owned = watcher.owned_order_ids()
            meta["backend"].update(broker.status())
            return {
                **meta,
                "caps": {**caps.to_json(), "watch": caps.order_status},
                "accounts": [a.to_json() for a in accounts],
                "account": account,
                "positions": [{**p.to_json(), "pick": picks.get(p.symbol)}
                              for p in sorted(positions, key=lambda x: x.symbol)],
                "orders": [{**o.to_json(), "watched": o.id in owned} for o in orders],
                "watches": [w.__dict__ for w in watcher.list(account)][:30],
                "has_picks": bool(picks),
            }

    @app.post("/api/preview", dependencies=[Depends(auth)])
    def preview(body: dict = Body(...)):
        req = PlanRequest.from_json(body)
        with lock:
            accts, positions, orders = _snapshot(req.account, max_age=8.0)
            accounts = {a.id: a for a in accts}
            if req.account not in accounts:
                raise PlanError("没有这个账户")
            if not accounts[req.account].tradable:
                raise PlanError("这个账户在当前接口下只读，不能下单")
            plan = build_plan(req, positions, orders, broker.capabilities(),
                              picks=load_picks(cfg.picks_file) if req.basis == "picks" else None,
                              watcher_available=True)
        signed = plans.put(plan)
        cfg.audit("preview", plan=plan.id, account=req.account, request=req.to_json(),
                  summary=plan.to_json()["summary"])
        return {"plan": plan.to_json(), **signed, "dry_run": cfg.dry_run}

    @app.post("/api/execute", dependencies=[Depends(auth)])
    def run(body: dict = Body(...)):
        try:
            plan = plans.take(str(body.get("plan_id", "")), str(body.get("confirm_token", "")))
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        if not plan.actionable:
            raise HTTPException(400, "这份计划里没有可以下的单")
        with lock:
            cache.clear()
            return execute(plan, broker, cfg, watcher, dry_run=cfg.dry_run)

    @app.post("/api/cancel", dependencies=[Depends(auth)])
    def cancel(body: dict = Body(...)):
        ids = body.get("order_ids")
        account = str(body.get("account") or "")
        if not account or not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise HTTPException(400, "需要 account 和 order_ids")
        with lock:
            cache.clear()
            return cancel_orders(broker, cfg, watcher, account, ids, dry_run=cfg.dry_run)

    @app.post("/api/watch/stop", dependencies=[Depends(auth)])
    def watch_stop(body: dict = Body(...)):
        wid = str(body.get("watch_id") or "")
        with lock:
            hit = watcher.disarm(watch_id=wid) if wid else []
        if not hit:
            raise HTTPException(404, "没有这个进行中的盯盘")
        return {"ok": True, "detail": "已停止盯盘；Robinhood 里的挂单没有动"}

    @app.get("/api/log", dependencies=[Depends(auth)])
    def log(n: int = 80):
        return {"entries": cfg.audit_tail(max(1, min(n, 500)))}

    return app

