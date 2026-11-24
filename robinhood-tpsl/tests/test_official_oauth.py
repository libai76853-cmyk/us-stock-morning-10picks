"""官方后端的登录链路：本地起一个带 OAuth 的 MCP 服务（HTTP），走真实的传输和授权流程。

验证的是我们自己这一段：回环地址收授权码、令牌落盘、重启后续期、没登录时不弹浏览器而是报错。
Robinhood 自己的授权服务器长什么样，这里证明不了。
"""

import asyncio
import json
import secrets
import socket
import stat
import threading
import time

import pytest

httpx = pytest.importorskip("httpx")
uvicorn = pytest.importorskip("uvicorn")

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,  # noqa: E402
                                      RefreshToken, construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.shared.auth import OAuthToken  # noqa: E402

from tpsl.brokers.base import BrokerAuthError  # noqa: E402
from tpsl.brokers.official import OfficialBroker, http_opener  # noqa: E402
from tpsl.config import Config  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MemoryAS:
    """最小的内存版授权服务器：用户「总是同意」，访问令牌 1 小时有效。"""

    def __init__(self):
        self.clients, self.codes, self.access, self.refresh = {}, {}, {}, {}
        self.authorizations = 0
        self.refreshes = 0

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params: AuthorizationParams) -> str:
        self.authorizations += 1
        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 60, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        return self.codes.get(authorization_code)

    def _issue(self, client_id, scopes, resource) -> OAuthToken:
        at, rt = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.access[at] = AccessToken(token=at, client_id=client_id, scopes=scopes,
                                      expires_at=int(time.time()) + 3600, resource=resource)
        self.refresh[rt] = RefreshToken(token=rt, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(access_token=at, refresh_token=rt, expires_in=3600, scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refreshes += 1
        self.refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token):
        t = self.access.get(token)
        return t if t and (t.expires_at or 0) > time.time() else None

    async def revoke_token(self, token):
        self.access.pop(getattr(token, "token", ""), None)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    port, cb_port = free_port(), free_port()
    base = f"http://127.0.0.1:{port}"
    provider = MemoryAS()
    srv = MCPServer("fake-rh-oauth", auth_server_provider=provider, auth=AuthSettings(
        issuer_url=base, resource_server_url=f"{base}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True), validate_token_resource=False))

    @srv.tool()
    def get_portfolio(account_number: str | None = None) -> dict:
        return {"positions": [{"symbol": "NVDA", "quantity": "3", "average_buy_price": "100"}]}

    server = uvicorn.Server(uvicorn.Config(srv.streamable_http_app(), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    for k in ("TPSL_BACKEND", "TPSL_LIVE", "TPSL_HOME"):
        monkeypatch.delenv(k, raising=False)
    for k in ("NO_PROXY", "no_proxy"):  # 本机回环不走代理
        monkeypatch.setenv(k, "127.0.0.1,localhost")
    monkeypatch.setenv("TPSL_OFFICIAL_URL", f"{base}/mcp")
    monkeypatch.setenv("TPSL_OAUTH_REDIRECT", f"http://localhost:{cb_port}/callback")
    cfg = Config(home=tmp_path / "home")

    # 假浏览器：访问授权地址并跟随跳转，相当于用户点了「同意」
    opened = []

    def fake_browser(url):
        opened.append(url)
        threading.Thread(target=lambda: httpx.get(url, follow_redirects=True, timeout=10), daemon=True).start()
        return True

    monkeypatch.setattr("webbrowser.open", fake_browser)
    yield cfg, provider, opened
    server.should_exit = True
    t.join(timeout=5)


def login(cfg) -> int:
    async def go():
        async with http_opener(cfg, interactive=True)() as client:
            return len((await client.list_tools()).tools)

    return asyncio.run(go())


def test_login_then_headless_use_then_refresh_then_relogin_needed(rig, capsys):
    cfg, provider, opened = rig
    token_file = cfg.path("official_oauth.json")

    # 没登录过：服务端（非交互）不弹浏览器，只报需要登录
    b = OfficialBroker(cfg)
    try:
        with pytest.raises(BrokerAuthError, match="login official"):
            b.positions("default")
    finally:
        b.close()
    assert opened == [] and provider.authorizations == 0

    # 终端登录一次
    assert login(cfg) == 1
    assert len(opened) == 1 and provider.authorizations == 1
    saved = json.loads(token_file.read_text())
    assert saved["tokens"]["access_token"] and saved["tokens"]["refresh_token"] and saved["client"]["client_id"]
    assert saved["expires_at"] > time.time() + 3000
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert "code_challenge_method=S256" in opened[0]

    # 之后服务端直接用存下的令牌，不再授权
    b = OfficialBroker(cfg)
    try:
        (p,) = b.positions("default")
        assert p.symbol == "NVDA"
        assert "缺这些工具" in b.status()["detail"]  # 这个假服务只有一个工具，自检会如实报缺
    finally:
        b.close()
    assert provider.authorizations == 1 and provider.refreshes == 0

    # 访问令牌过期：自动用 refresh token 续，不需要人
    old_access = saved["tokens"]["access_token"]
    provider.access.pop(old_access)
    saved["expires_at"] = time.time() - 10
    token_file.write_text(json.dumps(saved))
    b = OfficialBroker(cfg)
    try:
        assert b.positions("default")[0].symbol == "NVDA"
    finally:
        b.close()
    assert provider.refreshes == 1 and provider.authorizations == 1 and len(opened) == 1
    assert json.loads(token_file.read_text())["tokens"]["access_token"] != old_access

    # refresh token 也失效：报需要登录，仍然不弹浏览器
    provider.access.clear()
    provider.refresh.clear()
    saved = json.loads(token_file.read_text())
    saved["expires_at"] = time.time() - 10
    token_file.write_text(json.dumps(saved))
    b = OfficialBroker(cfg)
    try:
        with pytest.raises(BrokerAuthError, match="login official"):
            b.positions("default")
        assert b.status()["ok"] is False
    finally:
        b.close()
    assert len(opened) == 1
