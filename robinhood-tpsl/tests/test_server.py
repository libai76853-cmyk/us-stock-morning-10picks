import json

import pytest
from fastapi.testclient import TestClient

from tpsl.brokers.demo import DemoBroker
from tpsl.config import Config
from tpsl.security import MAX_FAILS
from tpsl.server import create_app


def make(tmp_path, monkeypatch, backend="demo", live=None, native_oco=True, picks=None):
    for k in ("TPSL_BACKEND", "TPSL_LIVE", "TPSL_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TPSL_BACKEND", backend)
    if live is not None:
        monkeypatch.setenv("TPSL_LIVE", live)
    pf = tmp_path / "cohorts.json"
    if picks is not None:
        pf.write_text(json.dumps(picks))
    monkeypatch.setenv("TPSL_PICKS_FILE", str(pf))
    cfg = Config(home=tmp_path / "home")
    broker = DemoBroker(native_oco=native_oco)
    app = create_app(cfg, broker, start_watcher=False)
    c = TestClient(app)
    c.headers["Authorization"] = "Bearer " + cfg.secret("access_token")
    return c, broker, cfg


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return make(tmp_path, monkeypatch)


def test_api_requires_token_but_page_does_not(rig):
    c, _, _ = rig
    assert c.get("/api/state", headers={"Authorization": ""}).status_code == 401
    assert c.get("/api/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/api/execute", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401
    page = c.get("/", headers={"Authorization": ""})
    assert page.status_code == 200 and "止盈止损" in page.text
    assert c.get("/manifest.webmanifest").status_code == 200 and c.get("/apple-touch-icon.png").status_code == 200
    assert c.get("/docs").status_code == 404


def test_lockout_after_repeated_bad_tokens(rig):
    c, _, _ = rig
    for _ in range(MAX_FAILS):
        assert c.get("/api/state", headers={"Authorization": "Bearer x"}).status_code == 401
    assert c.get("/api/state").status_code == 429  # 连正确口令也先锁住


def test_state_shape(rig):
    c, _, _ = rig
    s = c.get("/api/state").json()
    assert s["backend"]["name"] == "demo" and s["backend"]["dry_run"] is False
    assert s["account"] == "DEMO-1" and len(s["positions"]) == 7 and s["orders"] == []
    nv = next(p for p in s["positions"] if p["symbol"] == "NVDA")
    assert nv["last_price"] == "168.3" and nv["avg_cost"] == "142.5" and round(nv["pnl_pct"], 1) == 18.1
    assert c.get("/api/state?account=DEMO-2").json()["positions"][0]["symbol"] == "SPY"
    assert c.get("/api/state?account=bogus").json()["account"] == "DEMO-1"


def test_preview_then_execute_then_cannot_replay(rig):
    c, broker, _ = rig
    body = {"account": "DEMO-1", "mode": "both", "basis": "last", "tp_pct": 10, "sl_pct": 5, "symbols": ["NVDA", "AAPL"]}
    pv = c.post("/api/preview", json=body).json()
    assert pv["plan"]["summary"]["actionable"] == 2 and broker.open_orders("DEMO-1") == []  # 预览不下单
    ex = c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": pv["confirm_token"]})
    assert ex.status_code == 200 and ex.json()["ok"] and ex.json()["summary"]["done"] == 2
    assert {o.symbol for o in broker.open_orders("DEMO-1")} == {"NVDA", "AAPL"}
    again = c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": pv["confirm_token"]})
    assert again.status_code == 409 and len(broker.open_orders("DEMO-1")) == 2
    s = c.get("/api/state").json()
    assert len(s["orders"]) == 2 and all(o["kind"] == "oco" for o in s["orders"])


def test_execute_with_wrong_token_or_unknown_plan(rig):
    c, broker, _ = rig
    pv = c.post("/api/preview", json={"account": "DEMO-1", "basis": "last", "symbols": ["NVDA"]}).json()
    assert c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": "f" * 64}).status_code == 409
    assert c.post("/api/execute", json={"plan_id": "nope", "confirm_token": "x"}).status_code == 409
    assert c.post("/api/execute", json={}).status_code == 409
    assert broker.open_orders("DEMO-1") == []


def test_plan_with_nothing_actionable_cannot_execute(rig):
    c, _, _ = rig
    pv = c.post("/api/preview", json={"account": "DEMO-1", "basis": "cost", "symbols": ["TSLA"]}).json()
    assert pv["plan"]["summary"]["actionable"] == 0 and pv["plan"]["items"][0]["skipped"]
    assert c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": pv["confirm_token"]}).status_code == 400


def test_bad_preview_requests(rig):
    c, _, _ = rig
    assert c.post("/api/preview", json={"account": "DEMO-1", "mode": "buy"}).status_code == 400
    assert c.post("/api/preview", json={"account": "nope"}).status_code == 400
    assert c.post("/api/preview", json={"account": "DEMO-1", "sl_pct": "x"}).status_code == 400
    assert c.post("/api/preview", content="[1]", headers={"Content-Type": "application/json"}).status_code == 422


def test_read_only_account_cannot_preview(tmp_path, monkeypatch):
    c, broker, _ = make(tmp_path, monkeypatch)
    from tpsl.models import Account
    broker.accounts = lambda: [Account("DEMO-1", "主账户", tradable=False), Account("DEMO-2", "Agentic")]
    assert c.get("/api/state").json()["account"] == "DEMO-2"  # 默认落在可交易的账户
    r = c.post("/api/preview", json={"account": "DEMO-1", "basis": "last"})
    assert r.status_code == 400 and "只读" in r.json()["error"]


def test_real_backend_defaults_to_dry_run(tmp_path, monkeypatch):
    c, broker, cfg = make(tmp_path, monkeypatch, backend="official")
    assert cfg.dry_run
    s = c.get("/api/state").json()
    assert s["backend"]["dry_run"] is True and s["backend"]["live"] is False
    pv = c.post("/api/preview", json={"account": "DEMO-1", "basis": "last", "symbols": ["NVDA"]}).json()
    ex = c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": pv["confirm_token"]}).json()
    assert ex["dry_run"] and ex["results"][0]["status"] == "dry_run"
    assert broker.open_orders("DEMO-1") == []


def test_real_backend_live_flag_sends(tmp_path, monkeypatch):
    c, broker, cfg = make(tmp_path, monkeypatch, backend="official", live="1")
    assert not cfg.dry_run
    pv = c.post("/api/preview", json={"account": "DEMO-1", "basis": "last", "symbols": ["NVDA"]}).json()
    c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": pv["confirm_token"]})
    assert len(broker.open_orders("DEMO-1")) == 1


def test_watch_flow_and_stop_watch(tmp_path, monkeypatch):
    c, broker, _ = make(tmp_path, monkeypatch, native_oco=False)
    pv = c.post("/api/preview", json={"account": "DEMO-1", "basis": "last", "both_strategy": "watch", "symbols": ["NVDA"]}).json()
    assert pv["plan"]["items"][0]["strategy"] == "watch"
    c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": pv["confirm_token"]})
    s = c.get("/api/state").json()
    (w,) = s["watches"]
    assert w["state"] == "armed" and s["orders"][0]["watched"] is True
    assert c.post("/api/watch/stop", json={"watch_id": w["id"]}).json()["ok"]
    assert c.post("/api/watch/stop", json={"watch_id": w["id"]}).status_code == 404
    assert len(broker.open_orders("DEMO-1")) == 1  # 止损单还在


def test_cancel_endpoint(rig):
    c, broker, _ = rig
    pv = c.post("/api/preview", json={"account": "DEMO-1", "basis": "last", "symbols": ["NVDA"]}).json()
    c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": pv["confirm_token"]})
    oid = broker.open_orders("DEMO-1")[0].id
    assert c.post("/api/cancel", json={"account": "DEMO-1", "order_ids": []}).status_code == 400
    assert c.post("/api/cancel", json={"account": "DEMO-1", "order_ids": [oid]}).json()["ok"]
    assert broker.open_orders("DEMO-1") == []


def test_picks_basis_uses_tracking_file(tmp_path, monkeypatch):
    picks = {"cohorts": {
        "2026-11-20": {"pick_date": "2026-11-20", "picks": [
            {"ticker": "CPAY", "status": "HOLD", "stop_price": 400.0, "target_price": 470.0}]},
        "2026-11-24": {"pick_date": "2026-11-24", "picks": [
            {"ticker": "CPAY", "status": "HOLD", "stop_price": 412.28, "target_price": 487.53, "entry_price": 437.36},
            {"ticker": "NVDA", "status": "STOPPED", "stop_price": 150.0, "target_price": 200.0}]},
        "2026-10-01": {"pick_date": "2026-10-01", "retired": True, "picks": [
            {"ticker": "AAPL", "status": "HOLD", "stop_price": 1.0, "target_price": 999.0}]},
    }}
    c, _, _ = make(tmp_path, monkeypatch, picks=picks)
    s = c.get("/api/state").json()
    assert s["has_picks"]
    by = {p["symbol"]: p["pick"] for p in s["positions"]}
    assert by["CPAY"]["pick_date"] == "2026-11-24" and by["NVDA"] is None and by["AAPL"] is None
    pv = c.post("/api/preview", json={"account": "DEMO-1", "basis": "picks"}).json()
    items = {i["symbol"]: i for i in pv["plan"]["items"]}
    assert (items["CPAY"]["tp_price"], items["CPAY"]["sl_price"]) == ("487.53", "412.28")
    assert items["AAPL"]["skipped"] and "选股" in items["AAPL"]["reason"]


def test_log_endpoint_records_actions(rig):
    c, _, _ = rig
    pv = c.post("/api/preview", json={"account": "DEMO-1", "basis": "last", "symbols": ["NVDA"]}).json()
    c.post("/api/execute", json={"plan_id": pv["plan"]["id"], "confirm_token": pv["confirm_token"]})
    events = [e["event"] for e in c.get("/api/log").json()["entries"]]
    assert {"preview", "execute_start", "execute_item", "execute_end"} <= set(events)


def test_secrets_are_private_files(rig):
    import stat
    _, _, cfg = rig
    for name in ("access_token", "plan_secret"):
        assert stat.S_IMODE(cfg.path(name).stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.home.stat().st_mode) == 0o700
