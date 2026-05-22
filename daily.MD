# 每日选股 changelog

每次代码改动按日倒序记录。最上面是最新。

格式：日期 | commit | 摘要 + 影响 + 验证。

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
