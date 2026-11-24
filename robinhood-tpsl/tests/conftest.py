import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tpsl.brokers.demo import DemoBroker  # noqa: E402
from tpsl.config import Config  # noqa: E402
from tpsl.watcher import Watcher  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for k in ("TPSL_BACKEND", "TPSL_LIVE", "TPSL_HOME", "TPSL_PICKS_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TPSL_PICKS_FILE", str(tmp_path / "no-picks.json"))
    return Config(home=tmp_path / "home")


@pytest.fixture
def broker():
    b = DemoBroker(native_oco=True)
    # 清掉内置的演示持仓，每个测试自己摆
    b._pos = {"A": {}, "B": {}}
    b._prices = {}
    return b


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def watcher(cfg, broker, clock):
    return Watcher(cfg, broker, threading.RLock(), clock=clock, sleep=lambda s: None,
                   market_open=lambda: True, persist=True)


def snapshot(broker, account="A"):
    """和服务端一样：持仓带上现价，再取挂单。"""
    pos = broker.positions(account)
    q = broker.quotes([p.symbol for p in pos])
    for p in pos:
        if p.symbol in q:
            p.last_price = q[p.symbol].last
    return pos, broker.open_orders(account)
