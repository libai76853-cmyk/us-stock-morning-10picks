"""本机盯盘：券商接口下不了「止盈/止损联动单」时，用它让同一批股票同时有止盈和止损。

做法：止损单一直挂在 Robinhood（Mac 关机、断网时保护还在）；本机每隔几十秒看一次价格，
到了止盈价就撤掉止损单、改挂一张止盈限价卖单。限价单在规定时间内没成交而价格又回落，
就撤掉它、把止损单挂回去。

状态：
  armed      止损单在挂，等价格到止盈价
  cancelling 已请求撤止损单，等确认
  tp_placed  止盈限价单在挂，等成交
  done       止盈成交
  stopped    止损成交（或回落时已跌破止损价、按市价卖出）
  cancelled  盯盘被取消（用户撤了止损单，或在页面上取消）
  error      需要人工处理：note 里写了原因
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from .brokers.base import Broker, BrokerAuthError, BrokerError, BrokerUncertain
from .config import Config, write_private
from .models import ZERO, Order, SellOrder, fmt, price_str, whole_shares

ACTIVE = ("armed", "cancelling", "tp_placed")
NY = ZoneInfo("America/New_York")


def is_market_open(now: datetime | None = None) -> bool:
    """美股常规交易时段：周一到周五 9:30–16:00（纽约时间）。不识别节假日和半日市。"""
    t = (now or datetime.now(timezone.utc)).astimezone(NY)
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


@dataclass
class Watch:
    id: str
    account: str
    symbol: str
    quantity: str
    tp_price: str
    sl_price: str
    tif: str = "gtc"
    stop_order_id: str | None = None
    tp_order_id: str | None = None
    state: str = "armed"
    note: str = ""
    created_at: str = ""
    updated_at: str = ""
    tp_placed_at: float = 0.0
    reverts: int = 0
    cancel_tries: int = 0
    tp_fails: int = 0

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    @property
    def qty(self) -> Decimal:
        return Decimal(self.quantity)

    @property
    def tp(self) -> Decimal:
        return Decimal(self.tp_price)

    @property
    def sl(self) -> Decimal:
        return Decimal(self.sl_price)


class Watcher:
    def __init__(
        self,
        cfg: Config,
        broker: Broker,
        lock: threading.RLock,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        market_open: Callable[[], bool] = is_market_open,
        persist: bool = True,
    ):
        self.cfg, self.broker, self.lock = cfg, broker, lock
        self._clock, self._sleep, self._market_open = clock, sleep, market_open
        self._file = cfg.path(f"watches-{broker.name}.json") if persist else None
        self._watches: dict[str, Watch] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_tick: str = ""
        self.last_error: str = ""
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not self._file or not self._file.exists():
            return
        try:
            for d in json.loads(self._file.read_text(encoding="utf-8")):
                w = Watch(**d)
                self._watches[w.id] = w
        except (ValueError, TypeError) as e:
            self.last_error = f"盯盘记录读不出来：{e}"

    def _save(self) -> None:
        if self._file:
            data = [asdict(w) for w in self._watches.values()]
            write_private(self._file, json.dumps(data, ensure_ascii=False, indent=1))

    def _touch(self, w: Watch, state: str | None = None, note: str | None = None) -> None:
        if state:
            w.state = state
        if note is not None:
            w.note = note
        w.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.cfg.audit("watch", id=w.id, symbol=w.symbol, state=w.state, note=w.note,
                       stop_order_id=w.stop_order_id, tp_order_id=w.tp_order_id, quantity=w.quantity)
        self._save()

    # ---- 对外 ----
    def arm(self, account: str, symbol: str, quantity: Decimal, tp: Decimal, sl: Decimal,
            tif: str, stop_order_id: str) -> Watch:
        with self.lock:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            w = Watch(uuid.uuid4().hex[:10], account, symbol, fmt(quantity), price_str(tp), price_str(sl), tif,
                      stop_order_id=stop_order_id, created_at=now, updated_at=now)
            self._watches[w.id] = w
            self._touch(w, note="止损单已挂，等待到价")
            return w

    def disarm(self, *, account: str | None = None, symbol: str | None = None,
               watch_id: str | None = None, note: str = "已手动取消盯盘") -> list[Watch]:
        """只停止盯盘，不动券商那边的订单。"""
        with self.lock:
            hit = []
            for w in self._watches.values():
                if not w.active:
                    continue
                if watch_id and w.id != watch_id:
                    continue
                if account and w.account != account:
                    continue
                if symbol and w.symbol != symbol:
                    continue
                self._touch(w, "cancelled", note)
                hit.append(w)
            return hit

    def list(self, account: str | None = None, active_only: bool = False) -> list[Watch]:
        with self.lock:
            ws = [w for w in self._watches.values()
                  if (not account or w.account == account) and (w.active or not active_only)]
            return sorted(ws, key=lambda w: w.created_at, reverse=True)

    def owned_order_ids(self) -> set[str]:
        with self.lock:
            ids = set()
            for w in self._watches.values():
                if w.active:
                    ids.update(i for i in (w.stop_order_id, w.tp_order_id) if i)
            return ids

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tpsl-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._market_open():
                    self.tick()
            except Exception as e:  # 盯盘线程不能死
                self.last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.cfg.watch_interval)

    # ---- 一轮检查 ----
    def tick(self) -> None:
        with self.lock:
            active = [w for w in self._watches.values() if w.active]
            self.last_tick = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if not active:
                return
            try:
                quotes = self.broker.quotes(sorted({w.symbol for w in active}))
                self.last_error = ""
            except BrokerError as e:
                self.last_error = f"取行情失败：{e}"
                return
            for w in active:
                q = quotes.get(w.symbol)
                last = q.last if q else None
                try:
                    if w.state == "armed":
                        self._step_armed(w, last)
                    elif w.state == "cancelling":
                        self._step_cancelling(w, last)
                    elif w.state == "tp_placed":
                        self._step_tp_placed(w, last)
                except BrokerAuthError as e:
                    self.last_error = f"登录失效：{e}"
                    return
                except BrokerUncertain as e:
                    self._touch(w, "error", f"请求结果不明，请到 Robinhood 核对这只的挂单和持仓：{e}")
                except BrokerError as e:
                    w.note = f"本轮出错，下一轮重试：{e}"
                    self._save()

    def _step_armed(self, w: Watch, last: Decimal | None) -> None:
        stop = self.broker.get_order(w.account, w.stop_order_id or "")
        if stop is None:
            w.note = "查不到止损单，下一轮再查"
            self._save()
            return
        if stop.state == "filled":
            self._touch(w, "stopped", "止损单已成交")
            return
        if stop.state in ("cancelled", "rejected"):
            self._touch(w, "cancelled", "止损单已不在（被撤销或被拒），盯盘停止；这只现在没有止损保护")
            return
        if stop.state != "open" or last is None:  # 部分成交中，或状态认不出：这一轮不动
            return
        if last >= w.tp:
            self.cfg.audit("watch_trigger", id=w.id, symbol=w.symbol, last=fmt(last), tp=w.tp_price)
            self.broker.cancel(w.account, stop.id)
            w.cancel_tries = 1
            self._touch(w, "cancelling", f"现价 ${price_str(last)} 到止盈价，正在撤止损单")
            self._step_cancelling(w, last, wait=True)

    def _step_cancelling(self, w: Watch, last: Decimal | None, wait: bool = False) -> None:
        stop = self._await_closed(w.account, w.stop_order_id or "") if wait \
            else self.broker.get_order(w.account, w.stop_order_id or "")
        if stop is None:
            w.note = "撤止损单后查不到这张单，下一轮再查"
            self._save()
            return
        if not stop.is_final:
            if w.cancel_tries >= 5:
                self._touch(w, "error", "止损单撤了 5 次都没撤掉，止盈没有挂出；止损单仍在，请手动处理")
                return
            w.cancel_tries += 1
            self.broker.cancel(w.account, stop.id)
            self._save()
            return
        remaining = w.qty - stop.filled_quantity
        if stop.state == "filled" or remaining <= 0:
            self._touch(w, "stopped", "止损单在撤销前已成交")
            return
        w.quantity = fmt(remaining)
        if last is not None and last >= w.tp:
            self._place_tp(w, last)
        else:
            self._rearm(w, last, "撤掉止损单后价格已回到止盈价以下")

    def _step_tp_placed(self, w: Watch, last: Decimal | None) -> None:
        tp = self.broker.get_order(w.account, w.tp_order_id or "")
        if tp is None:
            w.note = "查不到止盈单，下一轮再查"
            self._save()
            return
        if tp.state == "filled":
            self._touch(w, "done", "止盈单已成交")
            return
        if tp.is_final:
            w.quantity = fmt(w.qty - tp.filled_quantity)
            self._rearm(w, last, "止盈单已不在（被撤销或被拒）")
            return
        if last is None:
            return
        timed_out = self._clock() - w.tp_placed_at >= self.cfg.tp_timeout
        if (last < w.tp and timed_out) or last <= w.sl:
            self.broker.cancel(w.account, tp.id)
            closed = self._await_closed(w.account, tp.id)
            if closed is None or not closed.is_final:
                w.note = "撤止盈单还没确认，下一轮再查"
                self._save()
                return
            if closed.state == "filled":
                self._touch(w, "done", "止盈单已成交")
                return
            w.quantity = fmt(w.qty - closed.filled_quantity)
            self._rearm(w, last, "止盈单没成交、价格回落")

    # ---- 动作 ----
    def _sellable(self, w: Watch) -> Decimal:
        """下单前按实际持仓再卡一次：绝不卖出超过持有的股数。

        只看持仓数量，不看「已被卖单占用」：刚撤完单时券商那边的占用数可能还没更新，
        据此判断会误以为没股可卖。真被别的单占着，券商会拒单，走下面的失败处理。
        """
        for p in self.broker.positions(w.account):
            if p.symbol == w.symbol:
                return min(w.qty, whole_shares(p.quantity))
        return ZERO

    def _place_tp(self, w: Watch, last: Decimal) -> None:
        qty = self._sellable(w)
        if qty <= 0:
            self._touch(w, "cancelled", "持仓已经不在，盯盘停止")
            return
        w.quantity = fmt(qty)
        try:
            o = self.broker.place_sell(SellOrder(w.account, w.symbol, qty, "limit", limit_price=w.tp,
                                                 tif=w.tif, ref=f"w{w.id}-tp"))
        except BrokerUncertain:
            raise
        except BrokerError as e:
            w.tp_fails += 1
            self._rearm(w, last, f"止盈单没挂上（{e}）")
            if w.tp_fails >= 3 and w.state == "armed":
                # 止损单已经挂回去了；不再每轮撤了重试
                self._touch(w, "error", f"止盈单连续 {w.tp_fails} 次没挂上（{e}）；止损单仍在，盯盘已停，请手动处理")
            return
        w.tp_fails = 0
        w.tp_order_id, w.tp_placed_at = o.id, self._clock()
        self._touch(w, "tp_placed", f"已挂止盈限价 ${w.tp_price}，等成交")

    def _rearm(self, w: Watch, last: Decimal | None, why: str) -> None:
        """把止损保护挂回去。"""
        qty = self._sellable(w)
        if qty <= 0:
            self._touch(w, "done" if w.tp_order_id else "cancelled", f"{why}；持仓已经不在，盯盘结束")
            return
        w.quantity = fmt(qty)
        try:
            if last is not None and last <= w.sl:
                # 已经在止损价以下：止损单会被拒，按市价卖出，等同止损触发
                o = self.broker.place_sell(SellOrder(w.account, w.symbol, qty, "market", tif="gfd",
                                                     ref=f"w{w.id}-mkt"))
                w.stop_order_id, w.tp_order_id = o.id, None
                self._touch(w, "stopped", f"{why}；现价 ${price_str(last)} 已低于止损价，已按市价卖出")
                return
            o = self.broker.place_sell(SellOrder(w.account, w.symbol, qty, "stop", stop_price=w.sl,
                                                 tif=w.tif, ref=f"w{w.id}-sl{w.reverts + 1}"))
        except BrokerUncertain:
            raise
        except BrokerError as e:
            self._touch(w, "error", f"{why}；重新挂止损失败（{e}）。这只现在没有止损保护，请手动处理")
            return
        w.stop_order_id, w.tp_order_id, w.cancel_tries = o.id, None, 0
        w.reverts += 1
        self._touch(w, "armed", f"{why}；止损单已重新挂上")

    def _await_closed(self, account: str, order_id: str, tries: int = 10, gap: float = 1.0) -> Order | None:
        o = None
        for i in range(tries):
            o = self.broker.get_order(account, order_id)
            if o is not None and o.is_final:
                return o
            if i < tries - 1:
                self._sleep(gap)
        return o
