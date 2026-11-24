"""网页访问口令、失败锁定、计划的一次性确认码。"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Callable

from .planner import Plan

PLAN_TTL = 180  # 预览出来的计划 3 分钟内有效
MAX_FAILS = 10  # 10 分钟内口令错 10 次就锁 10 分钟
FAIL_WINDOW = 600


class AccessGuard:
    """所有 /api 请求都要带 Authorization: Bearer <口令>。"""

    def __init__(self, token: str, clock: Callable[[], float] = time.time):
        self._token = token.encode()
        self._clock = clock
        self._fails: list[float] = []
        self._lock = threading.Lock()

    def locked(self) -> bool:
        with self._lock:
            now = self._clock()
            self._fails = [t for t in self._fails if now - t < FAIL_WINDOW]
            return len(self._fails) >= MAX_FAILS

    def check(self, presented: str | None) -> bool:
        if self.locked():
            return False
        ok = bool(presented) and hmac.compare_digest(presented.encode(), self._token)  # type: ignore[union-attr]
        if not ok:
            with self._lock:
                self._fails.append(self._clock())
        return ok


class PlanStore:
    """预览生成的计划存在内存里；确认时必须带回同一个 id 和确认码，且只能用一次。

    确认码是对计划全文的 HMAC：用户确认的就是他看到的那一份，中间改不了。
    """

    def __init__(self, secret: str, clock: Callable[[], float] = time.time):
        self._secret = secret.encode()
        self._clock = clock
        self._plans: dict[str, tuple[Plan, float, str]] = {}
        self._lock = threading.Lock()

    def _sign(self, plan: Plan, expires: float) -> str:
        body = json.dumps(plan.to_json(), sort_keys=True, ensure_ascii=False) + f"|{expires:.0f}"
        return hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()

    def put(self, plan: Plan) -> dict:
        expires = self._clock() + PLAN_TTL
        token = self._sign(plan, expires)
        with self._lock:
            now = self._clock()
            self._plans = {k: v for k, v in self._plans.items() if v[1] > now}
            self._plans[plan.id] = (plan, expires, token)
        return {"confirm_token": token, "expires_in": PLAN_TTL}

    def take(self, plan_id: str, token: str) -> Plan:
        """取出并作废。任何不对都抛 ValueError（带给用户看的原因）。"""
        with self._lock:
            entry = self._plans.pop(plan_id, None)
        if entry is None:
            raise ValueError("这份预览不存在或已经用过，请重新预览")
        plan, expires, good = entry
        if self._clock() > expires:
            raise ValueError("预览已过期（超过 3 分钟），请重新预览")
        if not hmac.compare_digest(str(token), good) or not hmac.compare_digest(self._sign(plan, expires), good):
            raise ValueError("确认码不对，请重新预览")
        return plan
