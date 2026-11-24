# Robinhood 一键止盈止损（手机网页）

给 Robinhood 里已有的股票、ETF 持仓批量挂止盈和止损。手机上打开一个网页：选百分比，看清每张单，按住确认。

服务跑在你自己的 Mac 上，手机通过 Tailscale 连进来。这个目录和每日选股互不影响，只读它的 `tracking/cohorts.json` 来取选股的止损价和目标价。

**它只做一件事：对已有的多头持仓挂卖出单。** 不买入，不做空，不碰期权和加密货币。每只卖出的股数不超过当时实际持有的整股数。

## 先看演示

```bash
cd robinhood-tpsl
./run.sh            # 第一次会建 .venv 并装依赖
```

终端会打印一个带口令的地址（`http://localhost:8787/#k=...`），在 Mac 浏览器里打开。默认是演示后端：持仓和行情都是模拟的，怎么点都不会下出真实订单。

## 手机怎么连

服务默认只监听本机（127.0.0.1）。推荐用 Tailscale，不用开端口，在外面用蜂窝网络也能连：

1. Mac 和 iPhone 都装 Tailscale，登录同一个账号。
2. Mac 上运行 `tailscale serve --bg 8787`。它会给出一个 `https://<Mac 名字>.<tailnet>.ts.net` 的地址，只有你自己的设备能访问。第一次可能提示去 Tailscale 管理后台开启 HTTPS。
3. 手机 Safari 打开 `https://<那个地址>/#k=<口令>`。口令只需要带这一次，之后记在手机浏览器里。
4. Safari 分享菜单里选“添加到主屏幕”，之后像 app 一样打开。

口令忘了：Mac 上运行 `./run.sh token`。要作废旧口令：`./run.sh token --reset`，然后重启服务。

只在家里用也可以设 `TPSL_HOST=0.0.0.0`，手机访问 `http://<Mac 的局域网 IP>:8787/#k=...`。这样是明文 HTTP，同一个 Wi-Fi 上的人能看到流量，不建议在别人的网络上这么用。**任何情况下都不要把这个端口映射到公网。**

## 三个后端

用环境变量 `TPSL_BACKEND` 切换，或写进本目录的 `.env`（参考 `.env.example`）。

| 后端 | 能操作哪个账户 | 止盈止损联动单 | 状态 |
|---|---|---|---|
| `demo`（默认） | 模拟账户 | 有 | 全部功能可用，不联网 |
| `official` | 只有 Agentic 账户能下单，其他账户只读 | 取决于接口，见下 | 协议和登录链路已在本地测通；Robinhood 的具体工具参数还没对着真实服务核对 |
| `unofficial` | 主账户 | 没有 | 逻辑已测；没有对着真实 Robinhood 跑过 |

两个真实后端**默认是演练模式**：持仓和行情是真的，确认后只做全部校验，不发任何下单或撤单请求。看几次预览都对了，再设 `TPSL_LIVE=1` 重启，才会真的下单。页面右上角会显示“演练”或“实盘”。

### official：Robinhood Agentic Trading

Robinhood 在 2026 年 5 月开放的官方接口（MCP 服务，地址 `https://agent.robinhood.com/mcp/trading`），用 OAuth 授权，不需要也不会保存你的密码。

前提：在 Robinhood 里开一个 Agentic 账户（需要在电脑上操作），把想用这个工具管的资金或持仓放进去。接口只能在这个账户里下单，主账户只能看。

```bash
./run.sh login official     # 浏览器里授权一次，令牌存在 ~/.rh-tpsl/official_oauth.json
./run.sh probe              # 只读自检：列出接口里的工具，试读账户、持仓、行情、挂单
TPSL_BACKEND=official ./run.sh
```

**`probe` 这一步不要跳过。** 我写这个后端时查不到 Robinhood 接口每个工具的参数定义，所以没有写死，而是按接口自己声明的参数表来填（`tpsl/brokers/mcp_adapter.py`）：工具按名字认，参数名对上已知的叫法才填，有一个必填参数不知道怎么填就拒绝发送。`probe` 会把认出来的对应关系和将要发送的参数样例打印出来，完整结果在 `~/.rh-tpsl/probe.json`。自检没全过就先别开实盘，把这个文件发给我对一下。

对不上的地方可以在 `~/.rh-tpsl/official_mapping.json` 里手动指定，不用改代码：

```json
{
  "tools": {"place": "place_equity_order", "get_order": "get_equity_order"},
  "fixed_args": {"place_equity_order": {"market_hours": "regular_hours"}},
  "values": {"type.stop": "stop_market", "tif.gtc": "gtc"}
}
```

联动单：如果接口里有 advanced order 工具，且参数里同时有止盈价和止损价，页面会直接用它。否则退回到“盯盘”或“拆分”（见下一节）。

令牌过期会自动续；续不了时页面提示需要登录，回 Mac 重新运行 `./run.sh login official`。

### unofficial：非官方接口（robin_stocks）

走的是 Robinhood App 自己用的私有接口，能直接操作主账户里的现有持仓。用之前要清楚：

- 这不是 Robinhood 公开支持的用法，违反用户协议，账户可能被要求反复验证或受到限制。风险由你承担。
- 近期有不少登录卡在设备验证的报告；短时间内反复登录可能触发风控。
- 接口随时可能变。变了要等 robin_stocks 更新：`.venv/bin/pip install -U robin_stocks`。

```bash
./run.sh login unofficial   # 终端里输入账号密码，再到手机 Robinhood App 里点批准
TPSL_BACKEND=unofficial ./run.sh
```

密码不落盘，只保存登录后的会话令牌（`~/.rh-tpsl/unofficial/robinhood.pickle`，权限 600）。服务运行时只读这个会话，过期了报错让你重新登录，不会自己去撞登录接口。

这个接口下不了 App 里的止盈/止损联动单，两头都要时用“盯盘”或“拆分”。

## 止盈止损怎么挂

Robinhood 不允许同一批股票同时被两张普通卖单占用，所以“既要止盈又要止损”有三种挂法：

| 挂法 | 做法 | 依赖 Mac 吗 |
|---|---|---|
| 联动单 | 一张 Robinhood 的止盈/止损联动单，一边成交另一边自动撤 | 不依赖 |
| 本机盯盘 | 止损单全部挂在 Robinhood；Mac 每 20 秒看一次价格，到止盈价就撤止损、改挂止盈限价单 | 止盈依赖，止损不依赖 |
| 拆成两半 | 一半股数挂止损，一半挂止盈限价 | 不依赖 |

接口支持联动单时自动用联动单，否则在页面上选后两种之一。

**盯盘的行为：**

- 只在美股常规时段（纽约时间周一到周五 9:30 到 16:00）运行。不识别节假日和半日市，那些时候行情不动，不会误触发。
- 到止盈价后：撤止损单，确认撤掉了，再挂止盈限价单。限价单价格就是止盈价，不会低于它成交。
- 限价单挂出 120 秒还没成交、价格又回到止盈价以下：撤掉限价单，把止损单挂回去。
- 限价单挂着的时候价格直接跌破止损价：撤掉限价单，按市价卖出，等同止损触发。
- 止损单被你在 Robinhood 里手动撤了：盯盘停止，不会替你补挂或卖出。
- 任何一步结果不明（超时、断线）：停下来标成“需要处理”，在页面上显示，不自动重试。
- Mac 关机或断网：Robinhood 里的止损单照常有效，止盈不会触发。服务重启后盯盘记录会接着跑。

从撤掉止损到挂上止盈之间有几秒钟这批股票没有挂单，这是这种做法本身的代价。

## 价位怎么算

- **基准**：成本价、现价，或每日选股给这只票定的止损价和目标价（`tracking/cohorts.json` 里还在持有的最新一期）。
- 价格四舍五入到最小报价单位（1 美元以上 0.01，以下 0.0001）。
- 止盈价不高于现价、或止损价不低于现价的，会跳过并说明原因，因为挂上就会立刻成交。按成本价算时，已经涨过止盈线或跌破止损线的持仓会遇到这种情况，改成按现价即可。
- 限价单和止损单只支持整股，零碎股不包含在内。
- 已经被别的卖单占用的股数不会重复挂；勾选“替换已有卖单”会先撤掉这只的旧卖单再挂新的。
- 每只可以单独改止盈价、止损价和股数。
- 止损单是“触发后市价卖出”，跳空或急跌时成交价可能低于止损价。

## 安全设计

- 只生成卖出平仓单，数量在计划、执行、盯盘三处各卡一次，不超过实际持有。
- 两步确认：预览生成一份计划和它的确认码，确认时必须带回同一份。确认码 3 分钟有效，只能用一次，计划内容被改过就对不上。
- 下单前重新取持仓和现价再校验，行情变了的那只不下。
- 不自动重试下单。请求发出去没拿到明确结果时标成“结果未知”，让你到“挂单”页核对，避免重复挂单。
- 真实后端默认演练。
- 网页接口全部要口令，口令错 10 次锁 10 分钟。口令、令牌、会话文件都在 `~/.rh-tpsl`，目录权限 700，文件 600。
- 每次预览、下单、撤单、盯盘动作都写进 `~/.rh-tpsl/audit.jsonl`，页面“记录”页能看。

## 验证到哪一步

已经验证的：

- 123 项自动测试全过（`.venv/bin/python -m pytest tests -q`）：价位计算、执行、盯盘的各种分支、三个后端、网页接口、口令和确认码。
- 官方后端：对着进程内的假 MCP 服务跑通了两种参数叫法、联动单、盯盘；对着本地带 OAuth 的假服务用真实 HTTP 跑通了“登录、无人值守使用、令牌过期自动续、续不了时报需要登录”。
- 非官方后端：用假的 HTTP 会话核对了持仓、行情、挂单的解析，以及下单各种返回（成功、被拒、超时、5xx）的分类。URL 用的是 robin_stocks 3.4.0 真实的拼法，下单走它的 `order()`。
- 页面：在手机尺寸的浏览器里实际点过预览、按住确认、撤单、记录，深色浅色都看过。

没有验证的：

- 没有连过真实的 Robinhood。写代码的环境访问不了 robinhood.com，也没有账户。
- 官方接口的工具名和参数是按公开报道里的名字（`get_portfolio`、`place_equity_order` 等）和通用规则写的，真实定义要靠你跑一次 `probe` 才知道。
- 非官方接口的登录（设备验证）没法在这里试。

所以上实盘前的顺序是：演示模式熟悉页面，登录，`probe`（官方），演练模式核对预览，然后先拿 1 股试一张单。

## 设置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TPSL_BACKEND` | `demo` | `demo` / `official` / `unofficial` |
| `TPSL_LIVE` | `0` | 真实后端设成 `1` 才真下单 |
| `TPSL_HOST` / `TPSL_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `TPSL_WATCH_INTERVAL` | `20` | 盯盘间隔（秒，最小 5） |
| `TPSL_TP_TIMEOUT` | `120` | 止盈限价单多久没成交且价格回落就撤掉（秒） |
| `TPSL_HOME` | `~/.rh-tpsl` | 状态文件目录 |
| `TPSL_PICKS_FILE` | `../tracking/cohorts.json` | 每日选股的跟踪文件 |
| `TPSL_OFFICIAL_URL` | `https://agent.robinhood.com/mcp/trading` | 官方接口地址 |
| `TPSL_OAUTH_REDIRECT` | `http://localhost:8765/callback` | 官方登录的本机回调地址，端口被占用时改 |
| `TPSL_DEMO_OCO` | `1` | 演示后端是否支持联动单；设 `0` 可以看盯盘和拆分的样子 |

## 目录

```
run.sh                 建环境并启动；./run.sh [serve|login official|login unofficial|probe|token]
tpsl/
  planner.py           设置 -> 一张张具体的卖单（只算不下）
  executor.py          照确认过的计划下单、撤单
  watcher.py           本机盯盘
  security.py          访问口令、计划确认码
  server.py            网页服务和 JSON 接口
  picks.py             读每日选股的止损价、目标价
  brokers/
    base.py            券商接口
    demo.py            模拟券商
    official.py        Robinhood Agentic Trading：连接、OAuth、自检
    mcp_adapter.py     官方接口的工具和参数匹配
    unofficial.py      robin_stocks
  static/index.html    手机页面（单文件）
tests/                 自动测试；fake_mcp.py 是测试用的假 Robinhood 服务
```

服务要一直开着盯盘才有效。最简单是在 Mac 的终端里一直跑着；Mac 会睡眠的话用 `caffeinate -i ./run.sh`。

## 免责

个人工具，不是投资建议。止盈止损不保证成交价，也不保证一定成交。实盘前请先用演练模式和小数量确认行为符合预期。
