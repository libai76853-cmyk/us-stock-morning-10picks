"""三个后端：demo（默认，模拟）、official（Robinhood Agentic Trading）、unofficial（robin_stocks）。"""

from __future__ import annotations

import os

from ..config import Config
from .base import Broker, BrokerAuthError, BrokerError, BrokerUncertain

__all__ = ["Broker", "BrokerAuthError", "BrokerError", "BrokerUncertain", "make_broker"]


def make_broker(cfg: Config) -> Broker:
    if cfg.backend == "demo":
        from .demo import DemoBroker

        oco = os.environ.get("TPSL_DEMO_OCO", "1").strip().lower() not in ("0", "false", "no", "off")
        return DemoBroker(native_oco=oco, drift=True)
    if cfg.backend == "official":
        from .official import OfficialBroker

        return OfficialBroker(cfg)
    if cfg.backend == "unofficial":
        from .unofficial import UnofficialBroker

        return UnofficialBroker(cfg)
    raise SystemExit(f"TPSL_BACKEND 只能是 demo / official / unofficial，收到 {cfg.backend!r}")
