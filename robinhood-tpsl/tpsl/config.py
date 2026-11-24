"""配置：全部来自环境变量，状态文件放在 ~/.rh-tpsl（权限 700，文件 600）。"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, home: str | os.PathLike | None = None):
        self.home = Path(home or os.environ.get("TPSL_HOME") or Path.home() / ".rh-tpsl")
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.home, 0o700)
        except OSError:
            pass
        # demo | official | unofficial
        self.backend = os.environ.get("TPSL_BACKEND", "demo").strip().lower()
        # 真实后端默认演练：只预览、不发单。确认无误后设 TPSL_LIVE=1 才真下单。
        self.live = _flag("TPSL_LIVE", False)
        self.host = os.environ.get("TPSL_HOST", "127.0.0.1")
        self.port = int(os.environ.get("TPSL_PORT", "8787"))
        self.watch_interval = max(5, int(os.environ.get("TPSL_WATCH_INTERVAL", "20")))
        self.tp_timeout = max(30, int(os.environ.get("TPSL_TP_TIMEOUT", "120")))
        self.official_url = os.environ.get("TPSL_OFFICIAL_URL", "https://agent.robinhood.com/mcp/trading")
        self.oauth_redirect = os.environ.get("TPSL_OAUTH_REDIRECT", "http://localhost:8765/callback")
        self.picks_file = os.environ.get(
            "TPSL_PICKS_FILE",
            str(Path(__file__).resolve().parents[2] / "tracking" / "cohorts.json"),
        )
        self._audit_lock = threading.Lock()

    @property
    def dry_run(self) -> bool:
        """演示后端永远可以「真下单」（反正是模拟的）；真实后端要显式打开。"""
        return self.backend != "demo" and not self.live

    def path(self, name: str) -> Path:
        return self.home / name

    def secret(self, name: str, nbytes: int = 32) -> str:
        """读取或首次生成一个随机密钥文件。"""
        p = self.path(name)
        if p.exists():
            s = p.read_text().strip()
            if s:
                return s
        s = secrets.token_urlsafe(nbytes)
        write_private(p, s + "\n")
        return s

    def audit(self, event: str, **fields) -> None:
        """追加一行审计日志（JSONL）。每次预览、下单、撤单、盯盘动作都记。"""
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._audit_lock:
            p = self.path("audit.jsonl")
            new = not p.exists()
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if new:
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass

    def audit_tail(self, n: int = 100) -> list[dict]:
        p = self.path("audit.jsonl")
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()[-n:]
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out


def write_private(path: Path, text: str) -> None:
    """原子写 + 600 权限。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
