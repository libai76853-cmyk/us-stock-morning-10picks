"""命令行入口。

  python -m tpsl serve              启动网页服务（默认）
  python -m tpsl login official     官方接口：浏览器里授权一次
  python -m tpsl login unofficial   非官方接口：终端里登录一次
  python -m tpsl probe              官方接口只读自检，结果写到 ~/.rh-tpsl/probe.json
  python -m tpsl token [--reset]    显示（或重置）手机访问口令
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import Config, write_private


def _access_url(cfg: Config, host: str | None = None) -> str:
    return f"http://{host or cfg.host}:{cfg.port}/#k={cfg.secret('access_token')}"


def cmd_serve(cfg: Config) -> None:
    import uvicorn

    from .server import create_app

    app = create_app(cfg)
    mode = "模拟数据" if cfg.backend == "demo" else ("真实下单" if cfg.live else "演练（不发单）")
    print("=" * 62)
    print(f" Robinhood 一键止盈止损 · 后端：{cfg.backend} · {mode}")
    print(f" 本机打开： {_access_url(cfg, 'localhost' if cfg.host in ('127.0.0.1', '0.0.0.0') else None)}")
    print(" 手机打开： 把上面的 localhost 换成这台 Mac 的 Tailscale 名字，")
    print("            或先运行 tailscale serve（见 README「手机怎么连」）")
    print(" 地址里 #k= 后面是访问口令，只需要第一次带上，别发给别人。")
    if cfg.host not in ("127.0.0.1", "localhost"):
        print(" 注意：现在监听的不只是本机。不要把这个端口暴露到公网。")
    print("=" * 62)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


def cmd_login(cfg: Config, which: str) -> None:
    if which == "unofficial":
        from .brokers.unofficial import login_interactive

        login_interactive(cfg)
        return
    import asyncio

    from .brokers.official import http_opener

    async def go() -> int:
        async with http_opener(cfg, interactive=True)() as client:
            res = await client.list_tools()
            return len(res.tools)

    try:
        n = asyncio.run(go())
    except Exception as e:
        raise SystemExit(f"授权没有完成：{type(e).__name__}: {e}\n"
                         "常见原因：Agentic 账户还没在 Robinhood 开通；或回调地址被占用（改 TPSL_OAUTH_REDIRECT）。")
    print(f"授权成功，接口里有 {n} 个工具。接着运行：./run.sh probe")


def cmd_probe(cfg: Config) -> None:
    from .brokers.official import OfficialBroker

    b = OfficialBroker(cfg)
    try:
        rep = b.probe()
    finally:
        b.close()
    out = cfg.path("probe.json")
    write_private(out, json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    print(f"工具 {len(rep['tools'])} 个：{', '.join(t['name'] for t in rep['tools'])}\n")
    print("认出的对应关系：")
    for role, name in rep["roles"].items():
        print(f"  {role:12s} -> {name}")
    print()
    for c in rep["checks"]:
        mark = "通过" if c["ok"] else "失败"
        text = json.dumps(c["result"], ensure_ascii=False, default=str)
        print(f"[{mark}] {c['name']}: {text[:300]}")
    caps = rep["capabilities"]
    print(f"\n联动单：{'支持' if caps['native_oco'] else '不支持'}；盯盘：{'可用' if caps['order_status'] else '不可用'}")
    for n in caps["notes"]:
        print(f"  - {n}")
    print(f"\n完整结果（含每个工具的参数定义）在 {out}")
    print("自检" + ("通过。" if rep["ok"] else "没有全过：先别开真实下单，把上面这个文件发给开发者对一下参数。"))
    sys.exit(0 if rep["ok"] else 1)


def cmd_token(cfg: Config, reset: bool) -> None:
    if reset:
        p = cfg.path("access_token")
        if p.exists():
            p.unlink()
        print("已重置。正在运行的服务要重启后才认新口令。")
    print(_access_url(cfg, "localhost"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tpsl", description="Robinhood 一键止盈止损")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve")
    lg = sub.add_parser("login")
    lg.add_argument("which", choices=["official", "unofficial"])
    sub.add_parser("probe")
    tk = sub.add_parser("token")
    tk.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)
    cfg = Config()
    if args.cmd in (None, "serve"):
        cmd_serve(cfg)
    elif args.cmd == "login":
        cmd_login(cfg, args.which)
    elif args.cmd == "probe":
        cmd_probe(cfg)
    elif args.cmd == "token":
        cmd_token(cfg, args.reset)


if __name__ == "__main__":
    main()
