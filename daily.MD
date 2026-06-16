# 每日选股 changelog

每次代码改动按日倒序记录。最上面是最新。

格式：日期 | commit | 摘要 + 影响 + 验证。

---
## 2026-06-16 — 入场价对不上：脏 entry 固化（锁到错误交易日 + 永不重算）

**Commit**: 5d851c1 + 6c4a17c

**症状**：用户「数据价格对不上」。6/15 cohort 里 ANET entry=160.11，但那其实是 6/12(周五)的开盘价，6/15 真实开盘是 165.96；TPR/MU/NVDA 同样锁到了各自 6/12 开盘价，且 closes 全空。

**根因**：cohort 6/15 盘前生成，盘中 `*/15` 刷新去锁「6/15 开盘价」时，Yahoo 在当日数据**未结算阶段把 6/12 的旧 K 线重贴成「6/15」返回**，entry 锁进脏值。`update_all_cohorts` 注释写「each run re-derives entry from scratch」，但代码是 `if not p.get("entry_price")` —— **只锁一次永不重算**，脏值固化。close 每次重算所以自愈，entry 不会，于是 entry 与 close 来自不同交易日，价格对不上。

**修复**（三段，当天迭代）：
1. `5d851c1` entry 只从**已结算交易日**（pick_date < 今天）的 K 线锁，且**每次重算**（去掉 `if not entry_price` 冻结）→ 脏值自愈。exit 检测也只在已结算 close 上（防当天盘中假突破误触发止损/止盈）。retire 用已结算天数计。
2. `6c4a17c` entry 回退到「第一个已结算交易日」开盘 → 容忍节假日。
3. `e0fc21b` **再放宽**：第 1、2 步的「只用已结算日」导致最近 cohort 大面积待锁——因为本环境 yfinance **按票跳过交易日**（给 25/33 只跳过了 6/15，从 6/12 直接跳到 6/16），「第一个已结算日 >= pick_date」当天无可锁。既然 entry 已每次重算（瞬时脏值会在下个 15min 刷新自愈），不必再等结算 → 改为从「pick_date 当天或之后第一个**可得** K 线」开盘锁，开盘后当场填满（也恢复了 5/29 的盘中锁行为）。exit 仍只在已结算 close 检测。

**验证**：CI refresh 后 6/11/6/12/6/15/6/16 cohort 全部 33/33 锁定、0 待锁，价格 + P/L 正常（缺 6/15 的票回落到 6/16 开盘入场）；entry vs screen_close 偏离 >25% 记录 = 0。live dashboard「待锁」归零。

**已知非代码问题**：本环境 yfinance 数据高度不稳定——MU(美光)返回 ~$1000+(实际应 ~$130)、不同票「最新交易日」不一致(有的卡 6/12 有的到 6/16)、近端开盘价会被事后修正。存的价直接来自 yfinance；这类源头脏数据非代码可控，只能靠「只用已结算数据 + 每次重算」降低影响。

**教训**：「锁一次」语义遇到**数据源会事后修正/重贴近端 K 线**时很危险——会把瞬时脏值固化。对不稳定数据源，宁可**每次从结算数据重算**（幂等自愈），也不要冻结一个可能错的早读值。注释和代码必须一致(这里注释早就写了"re-derive each run"，但代码没做到，掩盖了 bug)。

---

## 2026-06-11 — CF Worker `2-6` 修复终于部署（用户重新 wrangler login）

**Commit**: 无 repo 代码改动（CF Worker 部署，版本 b4c5e04b）

6/5 发现的 CF cron 星期约定 bug（`1-5`=周日~周四）的修复在 OAuth 过期上卡了几天，今天用户配合重新授权后部署完成：

```
schedule: 30 12 * * 2-6      ← 生成（周一~周五 12:30 UTC 准点）
schedule: */15 13-20 * * 2-6 ← 盘中刷新（周一~周五每 15 分钟）
```

**至此周五问题闭环**：根因（DOW off-by-one）已修；三重冗余依然在（CF 12:30 + CF */15 兜底 + GH 原生 schedule 迟到备份），全部幂等。周日不会再误触发（cron 本身不含周日 + run() 周末护栏双保险）。

**验证**：deploy 输出确认两个 `2-6` schedule 注册；Worker URL 403 健康（key gate 正常）。下个周五（6/12）应准点 12:30 推送 —— 这是 4 个周五以来第一次。

---

## 2026-06-10 — Yahoo 盘前空 K 线毒化推送（$nan 大面积出现）

**Commit**: 8ad7c1a

**症状**：用户截图，当日推送大部分票显示 `$nan (+nan%)`、`ROE nan%`，且动量列清一色 +0（HUBB 等少数正常）。

**根因**：盘前时段 Yahoo 给大部分 ticker 的 1y history 末尾**追加了当天尚未开盘的空 K 线**（OHLC 全 NaN）。`close.iloc[-1]` 取到 NaN → `last_close`/`ret_1d` 全 NaN → 动量桶比较全 False（+0）→ NaN 一路流进 Telegram 格式化。NaN 在 Python 是 truthy，`price or fallback`、`if roe:` 这类写法全部拦不住。与 6/5 META 单票 NaN 同族，但这次是几乎全量。

**修复（三层）**：
1. **根治**：`_enrich_sync` 拉到 history 后先 `hist = hist[hist["Close"].notna()]` 再算一切 → 盘前自动用昨收，信号恢复真实
2. **显示防御**：`_pick_block` 的 price/change 过 `_finite_or_none`，价格未知渲染 `$—` 而不是格式化 NaN
3. **fund_tag 防御**：`if roe:`/`if rg:` 改为先 coerce（裸 truthiness 检查放行了 NaN → "ROE nan%"）

**处置**：删除被毒化的 6/10 cohort（动量清零下选出的）→ 重跑 run 27280332177 → 重发修正版推送（4 条，TPR/APA/CI/INCY/NVDA 领衔，全真实价格+动量），cohort 用干净信号重建。

**单测**：构造全 NaN 毒化 pick 过 `_pick_block`，断言输出无独立 `nan` token（注意 "fi**nan**ce" 误报要用词边界正则）。

**教训**：对外部数据源，**NaN 是 truthy** —— `x or default` / `if x:` 都不是判空。所有从 yfinance 进来的数值在使用/格式化前必须过 `_finite_or_none` 这类有限性检查；显示层要有最后一道格式化防御。

---


## 2026-05-27 — v8 滚动 cohort 追踪 + HTML 权益曲线（撤销 v7 周锁）

**Commit**: 本次推送

**症状/动机**：用户原话「这样，每天还是推送不一样，每天推送的在本地监测一周或者两周，每天统计进一个excel表或者html。还做一个线型来看走势」。即撤销 v7「整周锁同一篮子」，回到每日新鲜 picks，但把每天那批作为 cohort 往后跟踪 2 周，统计进 dashboard 看走势。

**用户拍板的 3 个决策**（AskUserQuestion）：
- 跟踪窗口：**2 周（10 个交易日）**
- 输出格式：**HTML（GitHub Pages 手机可看）**
- 折线图：**累计权益曲线（等权日度再平衡 NAV）**

**架构**：
- **Telegram 回到每日新鲜 Top 33**（复用 v6 的 `format_messages`），撤掉 v7 的 preview/tracking 消息模板
- **滚动 cohort 模型** `tracking/cohorts.json`：每天一个 cohort（按推送日 keyed），跟踪 `TRACKING_WINDOW=10` 个交易日后 `retired=true`
  - entry = 推送当日 open（次日起可从 yfinance 锁定）
  - stop = entry − 1.5×ATR, target = entry + 3×ATR
  - closes 从 pick_date 起逐日（capped 10），首次 close 穿越 target/stop → 标记 exit
- **幂等重算设计**：每次 run 用单次 `fetch_ohlc_since(all_tickers, earliest)` 把所有活跃 cohort 从历史全量重算，**对 cron 漏拍免疫**（不再依赖每天增量 append）
- **权益曲线** `tracking/equity.json`：每个持仓价格路径 = [(pick_date, entry_open), (date, close)...]，日度收益归到后一天，每个交易日 NAV 收益 = 所有活跃持仓当日收益均值，从 1.0 链式复利
- **Dashboard** `docs/index.html`：自包含页面 + Chart.js CDN 折线图 + 各 cohort 战绩表，暗色手机友好

**重构**：
- `main.py`：v7 weekly 模块整体换成 v8 cohort 模块（`load_cohorts`/`save_cohorts`/`build_cohort`/`fetch_ohlc_since`/`update_all_cohorts`/`compute_equity`/`_cohort_summary`/`generate_dashboard_html`/`write_dashboard`），`run()` 重写为：生成今日 picks → 发 Telegram → 记录 cohort → 重算所有 cohort → 重算 equity + 重生成 dashboard
- `.github/workflows/daily_picks.yml`：commit step 改为 add `tracking/` + `docs/` + `weekly/`
- 新目录 `tracking/`（cohorts.json + equity.json）、`docs/`（index.html）

**本地验证**（合成数据）：equity 链式复利数学手算核对正确（止损后的持仓正确从次日剔除）；dashboard HTML 含 Chart.js + canvas 正常生成。

**GitHub Pages**：需开启 main 分支 /docs 目录（部署后操作）。

**未来**：今天起 cohort 历史从 0 累积，曲线慢慢长。可选：用已 commit 的 v7 篮子（5/25 那 33 只）+ yfinance 历史回填 5/25 cohort 让曲线一上来有几天数据。

**当天后续迭代**：
- **GitHub Pages 开启**：`gh api -X POST repos/.../pages -f 'source[branch]=main' -f 'source[path]=/docs'`，URL = https://libai76853-cmyk.github.io/us-stock-morning-10picks/
- **回填 5/25 cohort**（用户「回填，后面自然累积」）：纯 JSON 把 v7 篮子塞进 `tracking/cohorts.json`，CI run 从 yfinance 补 closes。注意 **5/25 是 Memorial Day 美股休市**，所以 5/25 cohort 实际入场是 5/26 open，tracking 从 5/26 起（日期标签和首个交易日差一天）。
- **可展开明细**（用户「没有详情」）：dashboard 点 cohort 行展开那 33 只逐只表（# / 票 / 行业 / 状态 / 入场 / 现价 / P/L% / 止损 / 目标），`function tg(id)` toggle 隐藏行。状态标签：持有 / 🎯目标 / 🛑止损 / 待锁。
- **规模确认**：稳态 ~10 活跃 cohort × 33 = ~330 持仓记录，去重 ~100-180 只（同一票可在多个 cohort 各有不同 entry，设计如此）。

---

## 2026-05-26 — v7 周度跟踪模式（picks 周一锁定 + 全周 P/L 监测）（已被 v8 取代）

**Commit**: 本次推送

**症状/动机**：用户原话「根据这些天推送的数据。我觉得还是一周推送一样的做比较。然后部署一个监测数据系统 ，看哪些target了。哪些出局？这个方向更好」。痛点：每日重生 picks 模式下 list 一变就没法验证「这只票推荐之后到底涨没涨」，等于一直换主意永不复盘，无法形成调评分桶的反馈闭环。

**新架构**：每周一生成 33 picks，整周锁定，每日推送状态表（含 P/L），周末汇总。

| 模式 | 触发条件 | 动作 |
|---|---|---|
| GENERATE | `weekly/weekly_picks_<Monday>.json` 不存在 | 跑 picker pipeline → 写文件，entries=null |
| LOCK | 文件存在 + entries=null + today > Monday | 从 yfinance 拉周一 Open → 锁定 entry / stop (=entry−1.5×ATR) / target (=entry+3×ATR) |
| TRACK | entries 已锁定 | 拉最新 close → 计算 P/L → 检测 target_hit / stopped_out → append daily_closes |
| PREVIEW | 周一盘前 entries 还没锁 | 只发 ticker 列表预览，无 entry/target/stop |

**文件 schema** `weekly/weekly_picks_<YYYY-MM-DD>.json`（按周一日期命名，commit 进 repo）：

```json
{
  "week_start": "2026-05-25",
  "generated_at": "...",
  "entries_locked_at": "...",  // null until locked
  "picks": [{
    "rank": 1, "ticker": "TPR", "sector": "...",
    "score": 83, "components": {"heat":20,"fund":32,"mom":10,"tech":21},
    "signals": {...}, "fundamentals": {...},
    "preview_close": 139.20,   // Friday close at gen time
    "entry_atr": 6.20,
    "entry_price": null,        // null until LOCK
    "stop_price": null, "target_price": null,
    "status": "PENDING|HOLD|TARGET_HIT|STOPPED",
    "exit_price": null, "exit_date": null,
    "daily_closes": [{"date":"...","close":...}, ...],
    "reason": "..."
  }, ...],
  "weekly_stats": {"target_hit":N, "stopped_out":N, "holding":N, "avg_pnl_pct":X.X}
}
```

**Telegram 模板**：
- TRACK 模式：`📈 本周 Picks 状态 — Day N` + 每只票 `[HOLD/🎯/🛑] +X.X% Entry→Now Stop/Target 距离`
- 出场后的票仍每日显示但标 `[🎯 TARGET]` / `[🛑 STOP]`，不再跟 P/L（exit_price 已锁）

**改动文件**：
- `main.py`：新增 weekly tracking 模块（~300 行），refactor 原 run() → `_generate_picks_pipeline()` + 新的 mode-switching `run()`
- `.github/workflows/daily_picks.yml`：加 `permissions: contents: write` + commit step
- 新目录 `weekly/`（不在 .gitignore 里，会被 commit）

**首次部署引导**：今天周二 5/26，本周 Monday=5/25 已过。预期流程：
1. 触发后无 `weekly/weekly_picks_2026-05-25.json` → GENERATE
2. today (5/26) > monday (5/25) → 立即 LOCK，从 yfinance 抓 5/25 Open
3. entries 已锁 → TRACK，发 Day 2 状态表
4. 末尾 auto-commit weekly/weekly_picks_2026-05-25.json 回 repo

**Telegram 预期变化**：从每日重生 33 picks → 整周看同一份 33 picks 的演进
- 周一：preview 预览（盘前）
- 周二：locked entries + Day 1 P/L
- 周三-五：每日 P/L 更新 + 触发的 target/stop 事件
- 周五收盘后：本周战果 append 到 `stats.md`

---

## 2026-05-26 — checkout 加一次 60s retry 防 GH 反滥用瞬时 flag

**Commit**: 本次推送

**症状/动机**：今天 12:30 UTC cron 触发的 run [26448175994](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/26448175994) 在 `actions/checkout@v4` 步骤连续 3 次失败，错误：
```
remote: Your account is suspended. Please visit https://support.github.com...
fatal: unable to access '...': The requested URL returned error: 403
```

`actions/checkout@v4` 内置 retry 是 3 次 / 总共 ~30 秒（11s+18s 间隔），全部撞在同一个 GH 反滥用瞬时 flag 上。2 小时后我手动 `gh workflow run` 重触发，同一份代码同一个 commit `b1f0c87` checkout 完全正常 → 确认是 GH side 瞬时误判，账号本身没问题（`api.github.com/users/libai76853-cmyk` 全程返回 200）。

**改动**：`.github/workflows/daily_picks.yml`
- `checkout` 第 1 次：加 `id: checkout1` + `continue-on-error: true`，失败不杀 job
- 中间步骤：`if: steps.checkout1.outcome == 'failure'` 时 `sleep 60`
- `checkout` 第 2 次：同 `if` 条件，无 `continue-on-error`，失败则整个 job 才失败
- 两次 checkout 都显式 `set-safe-directory: true`（v4 默认已 true，但写出来更明显）

**预期效果**：
- 大多数瞬时 flag 在 60-90 秒内消散，这次 retry 能自愈
- 极端长持续的 flag（比如 5/26 那次持续 ~2h）仍需手动 `gh workflow run` 重触发，但发生频率极低
- 正常路径无开销：if 条件挂着，attempt 2 不会跑

**未来如果还频繁出**：考虑升级为 2-3 次 retry，间隔指数退避（60s / 180s / 600s）；或在 CF Worker 端加 retry（先 dispatch，10 分钟后查状态，failure 就再 dispatch 一次）。

---

## 2026-06-05 (订正) — 真正根因：CF cron 星期约定 `1-5`=周日~周四，不是周一~周五

**这条订正推翻了下面「第 3 次漏拍 / flaky」的诊断。** 用户一句「是不是从周日就开始推送的问题」点破。

**铁证**（拉 3 周 run 历史，按 UTC 星期几分类）：
- **周日 5/24、5/31 的 12:30 都触发了**（那两天 GH schedule 还没加回来，只有 CF Worker 能触发 → 铁定是 CF cron）
- **周五 5/22、5/29、6/5 全没触发**
- 周一~周四全部正常触发

→ CF Workers 的 cron 星期字段把 `1-5` 解释成 **1=周日, 2=周一, ..., 5=周四**（即 1=Sun 的约定），所以 `30 12 * * 1-5` 实际跑的是**周日~周四，周五根本没被排进去**。这是**确定性的 off-by-one，不是随机丢 tick**。之前说的「flaky、n=3 周五巧合」全错。

**修复**：CF cron `1-5` → `2-6`（在 CF 约定下 = 周一~周五），`wrangler.toml` 已改。GitHub Actions 用标准 cron（0=Sun），所以 GH 备份保持 `1-5` = 正确的周一~周五，两个 provider 现在都对齐周一~周五。

**部署受阻**：wrangler OAuth 会话过期（5/19 登录，约 7 天失效），`wrangler deploy` 报 `Authentication error 10000`。需用户重新 `npx wrangler login` 后再 `npx wrangler deploy`。**但周五覆盖已由 GH 原生 schedule 备份兜住**（commit c060b1c，标准约定，周五会触发只是迟到 1-3h），所以即使 CF 暂未重新部署，下周五也不会漏，只是可能晚点。

**教训**：连续 3 个「周五漏拍」我一直当成 CF flaky 随机丢 tick，加冗余 cron / redeploy 治标。其实只要早点把 run 历史**按星期几列出来**就会看到「周日在跑、周五不跑」这个确定性信号。**面对疑似随机的周期性故障，先把事件按周期维度（星期/小时）摊开看分布，再下"随机"结论。** 用户的观察比我快。

**两个连带 bug（CF Friday bug 的产物，同日修复）**：
1. **6/5 全待锁**（用户「无数据」）：周五盘中 `*/15` 刷新 cron 也是 `1-5`（=周日~周四），周五不触发 → 没有盘中刷新来锁 6/5 入场价。手动 `gh workflow run "Refresh Tracking"`（run 27035299567，市场已开盘）→ 6/5 锁定 33/33（META 已止损）。CF redeploy 后周五自动刷新恢复。
2. **META `$nan 参考`**：yfinance 在 META 选股日返回 NaN last_close，NaN 在 Python truthy 绕过判空，且 `NaN` 写进 JSON 非法。加 `_finite_or_none()` 在 build_cohort 源头 + 显示层双重 coerce（commit bd2f2c7）。
3. **5/31 僵尸 cohort**：CF 周日误触发生成了 5/31（周日）cohort，但周日不开盘 → entry=open(5/31) 永远锁不上 → 33 个永久「待锁」污染 dashboard。加**周末护栏**（`run()` 周六日直接跳过生成，redeploy 前也防这周日 6/7 再造僵尸 + 周日推送）+ 删除 5/31（NAV 不变，僵尸不计入权益）（commit fd004be）。

---

## 2026-06-05 — CF cron 第 3 次漏拍（又周五）→ 加 GH 原生 schedule 备份 + 当日幂等

**Commit**: c060b1c

**症状**：用户「今天怎么又不推送了」。6/5（周五）12:30 生成 tick **和** 盘中 `*/15` 兜底 tick **全部没触发**（13:12 时今天 0 个 run）。这是第 3 次漏拍，**5/22 / 5/29 / 6/5 全是周五**（n=3，pattern 已硬）。

**关键认知**：v2/v3 的 `*/15` 兜底这次也失效，因为 CF 在这些周五是**整个 cron 服务对该 worker 全 drop**——再堆 CF cron 没用（都依赖同一个 flaky 的 CF 调度器）。必须加**独立触发源**。

**修复**：
1. 手动 `gh workflow run` 补今天推送（run 27016972934，Telegram 4/4 OK，cohort 6/5 记录）。
2. **重新启用 GitHub 原生 `schedule:`**（`30 12 * * 1-5`，2026-05-19 曾因延迟移除）作为**独立备份 provider**——它延迟 1-3h 但不依赖 CF，CF 全 drop 时仍能兜底（迟到总比没有强）。
3. **main.py 加当日幂等护栏**（`run()` step 0）：先 load cohorts，若今天 cohort 的 `generated_at` 已是今天 → 跳过选股 + Telegram，只 refresh_tracking。这样 CF 和 GH 两个源谁先触发谁发 Telegram，后触发的只刷新 → **无重复推送**。

**验证**：CI 再触发 run 27017228548 → 日志 "already generated today; skip pick-gen + Telegram, refresh only"，无 Telegram 发送，仅刷新 NAV ✓。

**最终触发架构**（3 重冗余，全幂等）：
- CF Worker cron 12:30（主，准时）
- CF Worker cron `*/15 13-20`（盘中刷新 + 兜底漏拍的 12:30）
- GH 原生 schedule 12:30（独立 provider 备份，延迟但 CF 全挂时兜底）

`/tmp/trigger_key.txt` 在 Mac 重启后丢失（手动 curl worker `?key=` 失效），但 `gh workflow run` 不需要 key，仍是最快手动补救方式。

---

## 2026-05-29 — 盘中 15 分钟刷新行情（修复「今日 cohort 无数据」）

**Commit**: b5b93a7 (main.py + refresh.yml) + CF Worker version 26646f69

**动机**：用户「能做成定时刷行情新数据吗？每隔15分钟刷新一下？」。今日 cohort 一直显示「待锁」无数据，因为入场价 = 推送当日开盘价，而推送是盘前（12:30 UTC，开盘前 1 小时），当时 yfinance 还没有当日 Open。

**实现**：
- `main.py` 加 `MODE=refresh` 路径（`refresh_only()` → `refresh_tracking()`）：只 `update_all_cohorts` + 重算 NAV + 重生成 dashboard，**不选股、不发 Telegram**。`update_all_cohorts` 本来就幂等，盘中重跑就是重新拉最新价（当日开盘后能锁定当日 cohort 入场价）。
- 新 workflow `.github/workflows/refresh.yml`：`MODE=refresh python main.py` + commit，`concurrency: cancel-in-progress` 防堆积，timeout 8min。
- CF Worker v3（version 26646f69）：cron 改 `["30 12 * * 1-5", "*/15 13-20 * * 1-5"]`。scheduled() 智能分流：今天**没有**成功 daily-picks run → dispatch `daily_picks.yml`（生成，也顺带兜底漏拍的 12:30）；**已有** → dispatch `refresh.yml`（盘中刷新）。

**效果**：开盘后第一个 */15 tick 就给当日 cohort 锁入场价 + 显示实时 P/L，整个交易时段每 15 分钟更新一次 NAV/dashboard。验证：5/29 cohort 盘中 refresh 后 33/33 locked（TPR entry 145.66），refresh.yml run 26646880944 success。

**副作用提醒**：盘中每 15 分钟一次 commit（~26 次/交易日），repo 历史会有较多 "Auto: intraday refresh" commit。可接受（数据仓库）；嫌多可改 30 分钟。

---

## 2026-05-29 — 运维：CF cron 第 2 次漏拍（又是周五）

**Commit**: 无代码改动（运维）

**症状**：用户「今天没有推送」。5/29 12:30 UTC cron 应触发但 0 个 run（最近 run 停在 5/28）。

**修复**：curl `/?key=...` 手动补 → run [26639154673](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/26639154673)（Telegram 4/4 OK，cohort 2026-05-29 记录，commit a6cebb5）+ `wrangler deploy` 重注册 cron（版本 547b99da）。

**Pattern 警觉**：CF cron 漏拍 5/22 + 5/29 **两次都是周五**。n=2 证据弱，可能 CF 周五负载高 / 纯巧合。

**当天根治（方案 2，已上线）**：CF Worker 升级 v2（version 80761686）：
- **冗余 cron**：`["30 12 * * 1-5", "0 13 * * 1-5", "0 14 * * 1-5"]` — 12:30 主 + 13:00/14:00 备份（都在盘前到开盘后半小时内）
- **幂等护栏**：每个 tick dispatch 前先 `GET .../runs?per_page=20`，若今天(UTC)已有 queued/in_progress/success 的 run 就 no-op。失败的 run 不算 → 备份会重试。API 出错时 fail-open（照常 dispatch，宁可重复不可漏）
- **效果**：主 tick 正常→备份跳过（无重复 Telegram）；主 tick 漏→备份补；今天 run 失败→备份重试
- **手动端点**：`?key=` 仍强制 dispatch（显式意图）；加 `?guard=1` 可走幂等检查
- **验证**：今天已有成功 run，curl `?key=...&guard=1` 返回 "today already handled; skipped"，未创建新 run ✓

---

## 2026-05-22 — v6 评分重平衡：罚顶部 + 奖回调

**Commit**: 见本次推送

**症状/动机**：用户原话「这推的大部分都接近新高的。我怕给我挂山顶上了」。审计当前评分桶发现结构性偏差：

| 信号 | 顶部追涨满分 | 回调买入满分 |
|---|---|---|
| ret_1d ≥5% | +12 | 0 |
| ret_5d ≥10% | +10 | 0 |
| near_high ≥0.95 | +8 | 0 |
| dist_52w ≥0.95 | +8 | 0 |
| ma200_pos ≥1.10 | +5 | 0 |
| rsi 65-75 | +2 | 0 |
| rsi <30 | 0 | +3 |
| MACD 2 | +6 | +6 |
| **合计** | **+51** | **+10** |

差 5 倍，`rsi>75` 只扣 −2 完全压不住，系统天生追涨。

**改动**：`momentum_score()` 和 `technical_score()` 桶大改：

1. **罚顶部**
   - `near_high ≥0.98` → **−2**（原 +8）
   - `dist_52w ≥0.98` → **−2**（原 +8）
   - `rsi >80` → **−15**（原 −2）
   - `rsi >75` → **−8**（原 −2）
   - `ret_1d ≥8%` → **−3**（原 +12）
   - `ret_1d 5-8%` → **+2**（原 +12）
   - `ma200_pos ≥1.30` → **0**（原 +5，过度拉伸不奖）

2. **奖回调买点**
   - `near_high 0.88-0.92` → **+7**（新增甜区，距月高 8-12%）
   - `near_high 0.82-0.88` → **+4**（新增）
   - `dist_52w 0.85-0.92` → **+6**（原 +4）
   - 新增 `quality_dip` 复合：`ma200_pos≥1.05` + `near_high 0.80-0.92` + `rsi 35-65` → **+6**

3. **RSI 中段补桶**
   - `rsi 40-50` → **+4**（原 0）
   - `rsi 30-40` → **+2**（原 0）
   - `rsi <25` → **+8**（原 +3）

4. **其他**
   - `near_high <0.70` → **−2**（新增惩罚跌太深）
   - `ma200_pos 0.95-1.00` → **+1**（新增，可能入场点）
   - `dist_52w 0.70-0.85` → **0**（明确中性区）

**预测验证**：今天 5/22 Top 1 「RL」打分模拟：
- v5 现状：+57（动量 +40 + 技术 +17）
- v6 新版：+16（动量 +27 + 技术 +-11）→ 应该掉出 Top 10

**实测**：run [26302404485](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/26302404485) 1m+ 跑完，Telegram 4/4 OK。
- 昨日 v5 Top 10：RL/IBM/SNDK/FSLR/TPR/WSM/DXCM/LLY/DAL/GPN
- 今日 v6 Top 10：TPR/ANET/CPAY/JBL/CHRW/**RL**/INCY/LLY/DECK/APA
- **7/10 是新面孔**（IBM、SNDK、FSLR、WSM、DXCM、DAL、GPN 全出局，都是顶部追涨型）
- RL 从 #1 掉到 #6（实测 score 73，含 heat 20，去 heat 后约 +53，仍高于 +16 估计 — 因为 ret_5d=+16.1% 仍触发 +10、macd=2 +6、ma200=1.11 +5 这些没改）
- RSI 范围从昨天的 65-80 → 今天 37-70；52w 距离从 ≥0.95 → 0.82-0.97
- 新引入：ANET（rsi 37 弱势区）、DECK（距 52w 高 18%）、APA（rsi 38）= 都是经典回调买点

**回退方案**：如果新版本踢掉太多真正强势趋势票，调整 `dist_52w` 的 0.98 桶从 −2 改回 0 或微正。

---

## 2026-05-22 — 运维：CF cron 静默漏一拍，手动补 + redeploy

**Commit**: 无代码改动（运维操作）

**症状**：今天 12:30 UTC 的 CF cron 应触发但**没出现任何 workflow_dispatch run**。当 13:34 UTC 用户问"怎么还没推送"时已过点 1 小时。对比昨天 5/21 12:31:00 UTC cron 完美触发，配置零改动。

**诊断**：
- Worker URL 活，curl `/` 返回 403（路径正常）
- 手动带 key curl 返回 `dispatch -> HTTP 204` → Worker 代码、`GH_TOKEN`、workflow_dispatch 端点全 OK
- `wrangler deployments list` 显示 Worker 自 5/20 20:03 起没动过

**根因**：CF Workers cron 触发器偶发漏 tick（CF 已知问题，无法预测，频次很低）。这次 CF 后端没把 scheduled event dispatch 出来，等同跳过这一次。

**修复**：
1. 手动 curl `/?key=...` 补一次触发 → run [26290954012](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/26290954012) 1m 多跑完，Telegram 4/4 OK
2. `npx wrangler deploy` 重发同份代码 → 新版本 `0b8a3f35`，cron `30 12 * * 1-5` 重新注册

**经验**：如果未来用户再说"今天没推送"，第一步永远是：
- 看 `gh run list` 最近一次 run 时间戳
- 如果今天 12:30 UTC 后没新 run → CF cron 漏拍
- 修复路径：curl `/?key=$(cat /tmp/trigger_key.txt)` 补当天 + `cd cf-worker-trigger && npx wrangler deploy` 刷新注册

---

## 2026-05-22 — 技术信号补完：52w/200d/RSI/MACD

**Commit**: [`ce336a4`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/ce336a4) Add technical signals: 52w distance, 200d MA, RSI(14), MACD

**动机**：免费爬虫 day 1 起就死，原热度柱永远 0；昨天补了短期动量后排名活了，但还想加中期技术面以补回爬虫失去的"52周新高"那类信号。

**改动**：
1. yfinance 取数从 `period="1mo"` 升到 `period="1y"`（同一次 HTTP 调用，仅返回体变大），覆盖 200 日均线和 52 周高低位需要的窗口
2. `_enrich_sync` 新增 4 字段：
   - `dist_52w` = last_close / 52w high（0..1，越接近 1 越强）
   - `ma200_pos` = last_close / 200日MA（>1 = 在均线上方）
   - `rsi14` = 标准 Wilder RSI（SMA 平滑近似）
   - `macd_state` = −2(死叉刚发) / −1(下行中) / +1(上行中) / +2(金叉刚发，5 日内)
3. 新增 `technical_score()`，桶式打分 −10 到 +22
4. `apply_boosts()` 现在叠 3 柱：fund + mom + tech
5. `mom_tag()` 改成 `signal_tag()`，把动量 + 技术合一行显示，避免推送行数翻倍
6. `make_reason()`、print log、`save_run_log` 都加上 `tech_score` 字段

**验证**：run [26269859731](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/26269859731)。新 Top 10：RL/CPAY/JBL/WSM/TPR/**ARM**/DAL/LLY/**MET**/DXCM。ARM 和 MET 是技术信号筛出来的新面孔（ARM 在 200 日均线上方 104%，MET 触 52 周新高）。Telegram 4 条全发 OK，总字符数跟昨天差不多（合并 tag 后没膨胀）。

**总分构成**（现状）：
- `heat_score` 0~? — 爬虫提供，目前永远 20 因为只有 "Default universe"
- `fund_score` −9 ~ +35 — 基本面 P/E + PEG + ROE + 营收/盈利增长
- `mom_score` −8 ~ +40 — 1日/5日涨幅、量比、距月高
- `tech_score` −10 ~ +22 — 距 52w 高、200日均位置、RSI、MACD 信号

---

## 2026-05-21 — 动量信号上线

**Commit**: [`5d4e8b0`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/5d4e8b0) Add yfinance-derived momentum signals so picks vary day-to-day

**症状**：5/19 和 5/20 推送的 Top 10 一模一样、顺序完全一致 — AVGO/CPAY/INCY/JBL/LVS/META/TPR/ALNY/APP/LLY。

**根因**：免费爬虫从未工作（详见 2026-04-19 段），所有 516 universe ticker 拿到 `heat_score=20`（统一来自 `len(sources)=1`），只剩基本面分排序 → 基本面季报频率 → 排名冻结。

**修复**：`_enrich_sync` 从已有的 `t.history(period="1mo")` 多算 4 个动量信号（0 额外网络）：
- `ret_1d`：昨日 close vs 前日 close
- `ret_5d`：5 日涨幅
- `vol_ratio`：昨日成交量 / 20 日均量
- `near_high`：last_close / 月内最高

`momentum_score()` 桶式打分（−8 到 +40），加入 `apply_boosts()` 与基本面并列。`mom_tag()` 在推送里显示。`change%` 列改为优先用 scraper 的 live change，无则回退 `ret_1d`。

**验证**：run [26254803493](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/26254803493) — 新 Top 10：RL/IBM/SNDK/FSLR/TPR/WSM/DXCM/LLY/DAL/GPN，8/10 是新名字。Top 2（RL, IBM）拿满 mom +40。Telegram 现在拆 4 条（每只多一行 mom_tag）。

---

## 2026-05-20 — CF cron 首次自动触发失败 + 修复

**Commit**: 无代码改动（运维操作）

**症状**：5/20 12:30 UTC 应该是 CF Worker cron 首次自动触发，但 0 个 workflow_dispatch run 被创建。CF Dashboard Observability 显示 1 Error。

**根因**：5/19 用户手动跑 `npx wrangler secret put GH_TOKEN` 时交互式粘贴 PAT，剪贴板或输入法层在某处搞坏了 token 的字节。wrangler 不验证 secret 内容，所以这个错误是静默的。Worker 持坏 token 直到 cron 触发，被 GitHub 401，scheduled handler 抛错。

**修复**：从 Claude Code shell 借用用户磁盘上 `wrangler login` 留下的 OAuth 会话，用 `printf '%s' "<PAT>" | npx wrangler secret put GH_TOKEN` 经 stdin 重灌（绕过所有 GUI 剪贴板）。`TRIGGER_KEY` 同样设了（随机 64-hex 存到 `/tmp/trigger_key.txt`）。

**验证**：手动 curl `https://daily-picks-trigger.libai76853.workers.dev/?key=$KEY` 返回 `dispatch -> HTTP 204`，触发 run [26186834874](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/26186834874)，1m20s 跑完，Telegram 3/3 OK。

**经验教训**：给 wrangler / 任何接受 secret 的 CLI 灌值时，**永远经 stdin pipe（`printf '%s' "value"` 或 `cat file`），绝不交互粘贴**。同一类剪贴板/输入法 bug 2 天内已击中 2 个不同表面。

---

## 2026-05-19 — Cloudflare Worker 替代 GH cron

**Commit**: [`d9b8782`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/d9b8782) Drop GitHub schedule trigger; CF Worker is now the cron source

**症状**：原 GH Actions `schedule:` cron 的实际触发延迟从 04-30 的 +2h04m 涨到 05-18 的 +3h32m，推送从「盘前 8:30 ET」漂移到「开盘后 11-12 AM ET」，错过实战窗口。

**根因**：GH Actions 免费 tier 的 `schedule` 事件是低优先级共享 runner，全网负载升高时被排队。官方文档已声明此限制。

**修复**：
1. 新建 Cloudflare Worker `daily-picks-trigger`，cron `30 12 * * 1-5` 调用 GitHub API 的 `workflow_dispatch` endpoint
2. 创建 fine-grained PAT `cf-worker-daily-picks-trigger`，权限仅 `Actions: read+write` on this repo，到期 2026-08-17
3. 部署后删除 GH workflow 的 `schedule:` trigger，避免双触发
4. 文件结构 `cf-worker-trigger/` 内：`wrangler.toml` / `src/index.js` / `DEPLOY.md`

**验证**：run [26109579335](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/26109579335) 1m01s 全绿。

---

## 2026-05-14 — Universe 扩到 S&P500+NDX100，TOP_N 33，Telegram 拆分

**Commit**:
- [`25d51e5`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/25d51e5) Expand fallback universe to S&P 500 + Nasdaq 100; bump TOP_N to 33
- [`92c9630`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/92c9630) Handle Wikipedia 403 + Telegram 4096-char limit

**改动**：
1. Fallback universe 从硬编码 50 只扩到从 Wikipedia 抓 S&P500 + Nasdaq100 → 去重 516 ticker
2. 抓不到时（Wikipedia 在 CI 上也 403）读 repo 内 commit 的 `universe.json`（516 ticker 静态快照）
3. 兜底用过时 universe 时 `enrich_pool` 自动扩到全集 516（否则 heat 都相同，前 50 是乱选）
4. `TOP_N` 10 → 33，`MAX_PER_SECTOR` 2 → 3（11 个 GICS × 3 = 33 严格容量）
5. Telegram 4096 字符限制：`format_messages()` 按字符预算拆成多条，每条带 `(idx/total)` header；前 33 票拆 3 条
6. CI workflow timeout 10min → 15min（500+ ticker 富化更慢）

**验证**：run [25877720807](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/25877720807) 跑通，Telegram 3 条全发 OK。

---

## 2026-04-20 — 兜底机制 + yfinance 富化

**Commit**:
- [`9732b49`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/9732b49) Upgrade picker: yfinance enrichment, hard filter, sector diversity, ATR targets, fundamentals, cache, run log
- [`b1767c4`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/b1767c4) Add fallback universe when free scrapers return <5 candidates

**改动**：
1. 加 yfinance `Ticker.info` + `history(period="1mo")` 富化：市值、平均量、行业、ATR(14)、PE/PEG/ROE/营收增长/盈利增长
2. `hard_filter` 用富化数据剔小盘（< $500M）、薄流动性（< $5M 日成交额）、低价股（< $5）
3. `pick_diverse` 行业上限分散
4. ATR 自适应入场点 / 止损 / 目标（Stop = entry − 1.5×ATR, Target = entry + 3×ATR）
5. `fundamental_score` 桶式打分（−9 到 +35）作为热度分之上的加权
6. 当 scraper 返回 < 5 候选时启用硬编码 50 大盘股 fallback universe
7. yfinance 富化结果按当日缓存（`cache/enrich_YYYY-MM-DD.json`）避免同日重跑

---

## 2026-04-19 — Day 1 部署 + 爬虫从未工作

**Commit**: [`5d5516e`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/5d5516e) Initial commit: daily US stock picker

**部署**：GitHub Actions cron `30 12 * * 1-5` (12:30 UTC Mon-Fri) → `python main.py` → Telegram 推送。

**爬虫初始状态（从未工作）**：
- Finviz 4 个 screener（涨幅榜、Gap-Up、异动量、52周新高）：HTTP 200 + 空表
- Nasdaq 盘前 API：空 rows
- StockTwits trending：空集
- Reddit 5 个 sub × 2 listing：0 mention

最早 run [24617831384](https://github.com/libai76853-cmyk/us-stock-morning-10picks/actions/runs/24617831384) 日志：
```
Total rows: 0, Nasdaq pre: 0, ST trending: 0
Unique candidates: 0
```

**结论**：4 个免费数据源在 GH Actions runner IP 段全部被反爬，从 day 1 就靠 fallback 兜底。详见根目录 `daily.MD` 顶部恢复方案讨论。

---

## 模板（未来改动追加在此之上）

```markdown
## YYYY-MM-DD — 一句话标题

**Commit**: [`<short>`](https://github.com/libai76853-cmyk/us-stock-morning-10picks/commit/<short>) 完整 subject

**症状 / 动机**：（如果是 fix）观察到的现象；（如果是 feature）为什么要加

**根因 / 设计**：技术解释

**改动**：
- 文件 / 函数级别的清单

**验证**：run [<id>](https://github.com/.../runs/<id>) 结果

**后续 / TODO**：（如有）
```
