[English](README.md) | [繁體中文](README.zh-TW.md) | **简体中文**

# 每日选股扫描流水线 (Daily Stock Screener Pipeline)

**一句话:每天定时用 Finviz、yfinance、Futu 快照和 GitHub Actions 云端 RS 表,按 O'Neil / Kell / Kullamägi 的动量体系自动扫描美股与港股的做多/做空候选,输出 TradingView / Webull / Futu 自选列表,并调用 LLM 生成 CANSLIM 简报。**

一套多数据源的动量(momentum)与做空(short)选股扫描器:美股用 Finviz 选股,盘中缺口取自 Futu 快照。结果导出为可直接导入 TradingView / Webull 的自选列表,并通过 OpenAPI 自动同步到 Futu(富途牛牛)的自定义分组;也可选同步到 TradingView 列表(走其非官方 REST API)。此外每天还调用 LLM API(如 Claude、DeepSeek 等)生成一份 CANSLIM 风格的研究简报。选股方法主要参考 William O'Neil、Oliver Kell 与 Kristjan Kullamägi。

> **状态(2026-08-02):** 美股、港股均已上线。美股数据来自 Finviz 与 yfinance,外加一张 12M IBD RS CSV 和一张 3M RS 表;港股用 yfinance 取 k 线与 HSI 历史(最早的 Futu-only 方案已弃用——Futu 免费/Lv1 档只能覆盖主板约 12% 的 12 个月历史)。如今 Futu 在港股侧只负责市值和条件 RS 触发所需的 HSI 实时日涨幅快照,在美股侧负责盘中缺口 discovery 与 Shorts 市值快照,外加两个市场的自选分组同步。
>
> **百分位 RS 表(美股 3M、港股 12M+3M)和港股长线侧 metrics frame 每天在 GitHub Actions 上算好,以 CSV 发布到 `data/`;本地流水线只负责拉取**——因为家用 IP 上的 yfinance 计算跑到一半就会被限流。RS 闸两市结构对称:**事件组(美股 Longs 5 组、港股 EarningsGap/HighVolume/GapUp)走 12M ≥ 90 单闸,其余长线侧(两市 Leaders / 条件 RS 组、美股 Shorts)走 3M ≥ 90 单闸**;每组一个独立旋钮,双闸可按组随时加回。历史不足 12 个月的新股则走**按历史深度分级的 IPO ladder**。
>
> 港股流水线有自己独立的 20:00 HKT 计划槽,美股则跑在 10:00 HKT,两者各写各自的分市场日志。美股 EOD 跑完后,wrapper 脚本会再跑一次 `--mode report --market us`,为当天新发现的长线侧个股生成 CANSLIM 简报(独立 HTML);港股不再排程生成报告。报告后端可选:默认 DeepSeek V4 + Tavily,备选 Anthropic `web_search`;也支持 Kimi / GLM / MiniMax(+ Tavily),走各自的 Anthropic 兼容端点。

## 筛选器 (Screeners)

所有基于 Finviz 的扫描都加 `ind_stocksonly` 排除 ETF/ETN;Morning Gap 用 Futu `stock_type=STOCK`,天然只含个股。

### 全局闸门 (long-side)

在 Finviz 选股之后、任何昂贵的 yfinance 计算之前先行套用。阈值均可在 `[settings]` 中配置。

| 闸门                                            | 作用范围                                | 阈值                                                           | 数据源                                                                                                                                                                                                                                                 |
| ----------------------------------------------- | --------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **IBD RS Percentile 12M (事件组)**              | Longs 5 组                              | ≥ 90(top 10%;表中缺失的 ticker 保留)                           | [Fred6725/rs-log](https://github.com/Fred6725/relative-strength),`RS = 0.4·P3 + 0.2·P6 + 0.2·P9 + 0.2·P12` 对 SPY 归一,工作日 ~01:30 UTC 刷新                                                                                                          |
| **IBD RS Percentile 3M (Leaders/RS/US Shorts)** | Leaders + RS 组 + US Shorts(不含 Longs) | ≥ 90(top 10%;缺失 ticker 保留)                                 | `RS_3M = 0.5·R21 + 0.3·R42 + 0.2·R63` 对 SPY,universe = Fred6725 ticker 列表(~6100),**云端在 GitHub Actions 上计算**并发布到 `data/us_rs_3m/<date>.csv`(带 `raw_score` 供 IPO 的 out-of-universe 查排名);`us_rs_3m.py` 负责拉取(拉不到就往回退 ≤ 3 天) |
| **Dollar Volume**                               | Longs + Leaders                         | 价 × 20 日均量 ≥ $100M                                         | yfinance 日线                                                                                                                                                                                                                                          |
| **ADR%**                                        | Longs + Leaders                         | mean(`(High − Low) / Close`) × 100,取最近 20 根完整 bar ≥ 4.0% | yfinance 日线                                                                                                                                                                                                                                          |

**RS 作用范围(每组一个独立旋钮,当前生效值):**

| 分组                                                            | 12M 闸                                               | 3M 闸                                                                |
| --------------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------- |
| Longs 5 组 (EarningsGap/HighVolume/GapUp/NewHigh52W/TopGainers) | `min_rs_percentile_longs` = **90**                   | —(设计上无此层)                                                      |
| Leaders                                                         | `min_rs_percentile` = 0(关)                          | `min_rs_percentile_3m` = **90**                                      |
| 条件 RS 组                                                      | `min_rs_percentile_rs` = 0(关;缺省继承 longs 键)     | `min_rs_percentile_3m` = **90**                                      |
| US Shorts                                                       | `min_rs_percentile_shorts` = 0(关;缺省继承 longs 键) | `min_rs_percentile_3m` = **90**                                      |
| US IPO ladder (≥ 64 天)                                         | —                                                    | `min_rs_percentile_3m`(用 `np.searchsorted` 对 Fred6725 `raw_score`) |

当前口径:**事件组看长期强度(12M ≥ 90),其余长线侧看近期强度(3M ≥ 90)**。12M ≥ 90 表示"长期领头羊",3M ≥ 90 表示"近期仍在领跑"。**Longs 5 组只用 12M 是有意为之**——它们本身的事件过滤已经足够强(EarningsGap / RVol 放量 / GapUp / 52W 新高 / Top Gainer),再叠一层 3M 会把 universe 收得过窄。每个旋钮都能单独调,设为 `0` 即关闭该层;`_rs` / `_shorts` 两个键不配置时缺省继承 `min_rs_percentile_longs`(把 12M ∩ 3M 双闸随时可以按组加回来)。把 `min_rs_percentile_3m` 设为 `0` 就能关掉整个 3M 层(连云端 CSV 也不再拉取)。HK Shorts 走 3M ≥ 90 单闸(`[hk_shorts].min_rs_percentile_3m`,缺省继承 `min_rs_percentile_longs_3m`,在 yfinance 批量下载前先筛 universe);Morning Gap 不做 RS 闸。

ADR% 取代了过去的 Finviz `beta > 1.5` 过滤:beta 反映的是多年来与大盘的相关性,容易误杀那些眼下正活跃、真正 in-play 的中大盘催化剂票;而 ADR%(Kullamägi 式)直接衡量一只股票当下的波动幅度。

### Longs(5 个策略,互斥)

Oliver Kell 的动量/突破 setup。按优先级排序,靠前的策略优先命中,每只 ticker 每天最多进一个 Longs 文件。

| 优先级 | 策略          | Finviz 过滤                                                                                              |
| ------ | ------------- | -------------------------------------------------------------------------------------------------------- |
| 1      | `EarningsGap` | Small Cap+, Earnings Today, Avg Vol > 500K, Price > $20, Rel Vol > 1.5, Gap Up 5%+, Above SMA50 & SMA200 |
| 2      | `HighVolume`  | Small Cap+, Avg Vol > 500K, Price > $20, Day Up, Above SMA50 & SMA200 + yfinance Rel Vol ≥ 3× 20 日均量  |
| 3      | `GapUp`       | Small Cap+, Avg Vol > 500K, Price > $20, Gap Up 3%+, Above SMA50 & SMA200                                |
| 4      | `NewHigh52W`  | Small Cap+, Avg Vol > 500K, Price > $20, New 52W High, Above SMA50 & SMA200                              |
| 5      | `TopGainers`  | Small Cap+, Avg Vol > 500K, Price > $20, Above SMA50 & SMA200, Signal: Top Gainers                       |

这 5 组同样要过全局的 Dollar Volume / ADR% 闸以及 IBD RS 12M ≥ 90。**Longs 不加 3M 层**——事件过滤本身选的就是新鲜动量。

### EOD Repeat(美股,只读 sidecar)

已在跨日 master 里、今天又命中 4 个*事件类* Longs 子组之一的老票(`[eod_repeat] keys` = EarningsGap/HighVolume/GapUp/TopGainers;NewHigh52W 和 Leaders 刻意排除——它们是持续状态,会天天复燃刷屏),汇总写入 `<date>_Repeat.txt` 供每日复查。对 master 只读:不写回,各组自身的 `.txt` 仍保持"只出新票"的语义。Futu/TV 同步映射默认注释掉(`Repeat` 分组/列表须先手动建好),启用前同步是 no-op。

### Leaders(5 个策略,合并)

站上 SMA50 与 SMA200 的长期趋势领头羊。五个策略共用同一套基础过滤,只在 perf 窗口上有所不同。

**基础过滤:** Small Cap+, Avg Vol > 500K, Price > $20, Above SMA50, Above SMA200,外加全局闸(**RS 走 3M ≥ 90 单闸**,12M 层 `min_rs_percentile` 当前设 0 关闭)。

| 策略              | Performance 阈值  |
| ----------------- | ----------------- |
| Leaders 4W +30%   | 4 周 perf ≥ 30%   |
| Leaders 13W +50%  | 13 周 perf ≥ 50%  |
| Leaders 26W +100% | 26 周 perf ≥ 100% |
| Leaders YTD +100% | YTD perf ≥ 100%   |
| Leaders 52W +150% | 52 周 perf ≥ 150% |

### US Shorts

Kullamägi 抛物线 blow-off setup。分两阶段:先用 Finviz Ownership 预过滤,再在一次共享下载上做 yfinance 后处理。

**Phase 1 — Finviz Ownership:** SMA20 +20%, Above SMA50, Avg Vol > 1M(Finviz 3 个月均量), Cap > $50M(2026-08-24 从 $300M 下调);随后套 **IBD RS 3M ≥ 90**(12M 层 `min_rs_percentile_shorts` 当前设 0 关闭),在进 yfinance batch 之前先筛掉一批。

**Phase 2 — yfinance + Futu 市值快照,顺序:performance → dollar volume → ADR% → 连续上涨天数。**

| 过滤                               | 阈值                                     | 数据源                                               |
| ---------------------------------- | ---------------------------------------- | ---------------------------------------------------- |
| 市值(perf 分桶用)                  | 实时 USD 值                              | Futu 快照 `total_market_val` → Finviz Ownership 兜底 |
| Dollar Volume                      | ≥ $100M(20 日均量)                       | yfinance                                             |
| ADR%                               | ≥ 4.0%(20 日)                            | yfinance                                             |
| Performance — Large Cap (≥ $10B)   | 2、3 或 4 周内 Up 50%+                   | yfinance                                             |
| Performance — Mid Cap ($2B–$10B)   | 2、3 或 4 周内 Up 200%+                  | yfinance                                             |
| Performance — Small Cap ($50M–$2B) | 2、3 或 4 周内 Up 300%+                  | yfinance                                             |
| 连续上涨天数                       | ≥ 3 个绿天(若开市则排除今日未完成的 bar) | yfinance                                             |

市值取自 Futu 的精确数值,而不是 Finviz 那种 `"6.96M"`/`"1.23B"` 的粗略字符串——后者在 $2B / $10B 分界附近很容易分错桶。

### RS — Relative Strength(条件触发)

Oliver Kell 的相对强度打法,专挑弱市里扛住的股票。**只在 SPY 和 QQQ 当日都跌 ≥ 1.0% 时才运行**(`check_market_down`,阈值写在代码里)。

过滤:Small Cap+, Avg Vol > 500K, Price > $20, Day Up, Above SMA50 & SMA200, Dollar Volume ≥ $100M, ADR% ≥ 4.0%, **IBD RS 3M ≥ 90**(12M 层 `min_rs_percentile_rs` 当前设 0 关闭)。

### RS-line 趋势标注(仅日志)

云端脚本会把 `rs_below_ma` / `rs_days_below_ma` / `rs_frac_below_ma` 三列写进 `data/{us_rs_3m,hk_rs}/<date>.csv`(TraderLion 式 **RS line** = 价 ÷ 基准,再与它自己的 EMA21 比较)。EOD 日志据此*标注*那些 RS line 持续处于均线下方(走弱)的长线侧 survivors。这一步**只写日志**,不影响 `.txt` 输出,也不进 dedup;跨日 master 的手动裁剪走 `--mode rs-line-audit`。配置见 `[rs_line]`。

**每日 RS 最强快照:** 每个 EOD wrapper(`run_eod.sh` / `run_hk_eod.sh`)结尾会以 soft 步骤追加跑一次 `--dry-run` audit,按 RS 线趋势给跨日 master 打分,并把最强的 `[rs_line].top_n`(10)只写入 `output/TV/US/rs_us_<date>.txt`(美股,10:00 slot 之后;直接落在 TradingView 文件夹,方便导入)和 `output/hk_rs_<date>.txt`(港股,20:00 slot 之后)。带日期、逗号分隔、结果为空不写文件、同日重跑覆盖;定时跑绝不裁剪 master。保留期:快照与 audit 报告及 sidecar 统一 4 天窗口(今天 + 前 3 天)。

### IPO(自动收集的 sidecar)

收集那些通过了某个 Longs/Leaders/RS 的 Finviz 筛选、却因日线历史不足被 yfinance 丢弃的长线候选——典型就是最近几个月刚上市的新股。这批候选再过一道按历史深度分级的 ladder(实现于 `us_ipo.filter_us_ipo_candidates`,与 HK 的 `filter_hk_ipo_candidates` 对应),这样上市第 30 天的新股仍能浮现,而上市第 200 天的则要通过几乎完整的长线基线:

| 闸门         | 阈值                             | 条件                                          |
| ------------ | -------------------------------- | --------------------------------------------- |
| min history  | ≥ 20 交易日                      | 总是(前 19 天成交量噪声太大,直接剔除)         |
| cap          | ≥ $300M                          | 总是(cap 来自 screener pass 时抓的 Finviz 值) |
| price        | ≥ $20                            | 总是                                          |
| avg vol      | ≥ 500K 股/天                     | 仅当 ≥ 20 天                                  |
| $vol         | ≥ $100M                          | 仅当 ≥ 20 天                                  |
| ADR%         | ≥ 4.0%                           | 仅当 ≥ 20 天                                  |
| above SMA50  | —                                | 仅当 ≥ 50 天                                  |
| above SMA200 | —                                | 仅当 ≥ 200 天                                 |
| 3M RS        | ≥ 90(对 Fred6725 raw_score 分布) | 仅当 ≥ 64 天                                  |

阈值对齐 US Longs 基线,历史攒满后即可无缝晋升。3M RS 闸的处理比较特殊:IPO 候选(上市不足 120 天)不在 Fred6725 universe 里,所以 ladder 先在本地算出它的分数,再用 `np.searchsorted` 放到 Fred6725 `raw_score` 分布里排名——相当于问"这只新股要是今天加入 universe,会排在什么位置"。`min_rs_percentile_3m = 0` 时整个 RS 闸跳过。

- 输出:`output/TV/US/<date>_IPO.txt`,加 Webull 镜像与 Futu 分组 `IPO`。
- 跨日 master:`output/state/eod_seen_IPO.txt`,独立于 `eod_seen_US.txt`——这样一只新股攒够历史后,会在第一个合格日直接落进它本该属于的长线侧分组。
- 一道保护:出现在 12M Fred6725 RS 表里的 ticker 必有 ≥ 12 个月历史,不可能是新股;这类丢弃只是 yfinance 的瞬时缺口,会在 ladder 之前先从 IPO 桶里剔除。

### HK Shorts

方法学与 US Shorts 一致,数据源换成 HKEX 股票列表(约 2,400 只主板股)加 yfinance,阈值全部用 HKD 原生值:**3M RS ≥ 90 单闸**(vs HSI,对齐 US Shorts;在 yfinance 批量下载前先筛 universe,表中缺失的票保留),cap ≥ HKD 50M(2026-08-24 从 300M 下调,与美股 $50M 下限同步),avg vol ≥ 1M 股/天(做空侧独有的下限,长线侧是 500K),dollar volume ≥ HKD 100M(对齐美股 $100M),ADR% ≥ 4.0%,按 HKD 10B / 2B / 50M 三档市值分别对应 perf 50/200/300%,连续上涨 ≥ 3 天;输出 `HKEX:NNN` 格式并去掉前导零。已于 2026-05-06 重新启用。

### HK 长线侧:EarningsGap / HighVolume / GapUp / Leaders / RS

五个策略,数据源为 **yfinance**(k 线 + HSI)加 **Futu**(市值 + HSI 实时日涨幅)。最初是 Futu-only 方案,但 Futu 免费/Lv1 档只能覆盖主板约 12% 的 12 个月历史,IBD 12 个月 RS 算法几乎无票可排,于是长线 k 线改走 yfinance——它对几乎每只 ticker 都能稳定提供 2 年以上数据。方法学对齐 US 的 Longs/Leaders/RS,阈值用 HKD 原生值,universe 为 HKEX 主板股(约 2,400 只)。输出 `output/TV/HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,RS}.txt`,采用 `HKEX:NNN` 的 TradingView 格式(去前导零,否则 TV 会静默拒绝 `HKEX:0148` 这类写法)。

**统一基线 (`[hk_settings]`):**

| 闸门                 | 阈值                | 备注                                                                                                                                                                                          |
| -------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Market Cap           | ≥ HK$300M           | 对小盘友好;HK 流动性约比美股薄 10×                                                                                                                                                            |
| Avg Volume           | ≥ 500K 股/天(20 日) | 对齐 US `sh_avgvol_o500`                                                                                                                                                                      |
| Dollar Volume        | ≥ HK$100M(20 日)    | 对齐美股 $100M;与 HK Shorts 持平                                                                                                                                                              |
| ADR%                 | ≥ 3.0%(20 日)       | 从 4.0% 调低;HK 蓝筹波动率结构性偏低                                                                                                                                                          |
| Last Price           | ≥ HK$20             | HK 原生(`min_price`)                                                                                                                                                                          |
| Above SMA50 & SMA200 | 两者                | 对齐 US `ta_sma50_pa` + `ta_sma200_pa`,套在每个长线侧过滤上                                                                                                                                   |
| RS Percentile        | 按组分工(vs HSI)    | **事件组(EarningsGap/HighVolume/GapUp)12M ≥ 90;Leaders/RS 组 3M ≥ 90**(`min_rs_percentile_longs` / `min_rs_percentile_longs_3m`,设 0 关闭对应层);对齐美股结构;IBD 算法 vs HSI,非 Fred6725 CSV |

**各策略闸门**(按优先级排序,靠前的优先命中,每只 ticker 每天最多进一个 HK 长线侧文件)。五个策略都继承上面的统一基线(现已含 SMA50 & SMA200 趋势过滤),所以下表列的是叠加在基线之上的附加闸门:

| 优先级 | 策略           | 附加闸门                                                                                                       |
| ------ | -------------- | -------------------------------------------------------------------------------------------------------------- |
| 1      | HK EarningsGap | gap ≥ 3% + RVol ≥ 3(形态代理——HK 无财报日历)                                                                   |
| 2      | HK HighVolume  | RVol ≥ 3                                                                                                       |
| 3      | HK GapUp       | gap ≥ 3%                                                                                                       |
| 4      | HK Leaders     | 满足任一(4w +30 / 13w +50 / 26w +100 / YTD +100 / 52w +150)                                                    |
| 5      | HK RS          | 当日收红(last > prev_close,≙ 美股 `ta_perf_dup`);**条件触发**——仅当 HSI 日涨幅 ≤ −1.0%(`hsi_rs_trigger`)时运行 |

**HK RS 算法**:沿用美股那套 `0.4·R3 + 0.2·R6 + 0.2·R9 + 0.2·R12` 加权季度收益公式(再叠一层 3M),基准换成 HSI(`^HSI`),百分位在 HK 主板 universe 内排名。**计算放在 GitHub Actions 云端**,把 12M、3M 和 RS-line 三部分合成一张 CSV 发布到 `data/hk_rs/<date>.csv`;`hk_rs.py` 负责拉取并拆分,拉不到就往前回退最多 3 天。HK 长线侧的 **metrics frame** 同样云端发布到 `data/hk_metrics/`,由 `hk_metrics.build_hk_metrics_cloud` 拉取,云端取不到时再回退到本地实时 yfinance 抓取。**周末补跑**会把数据日映射到上周五(`hk_effective_data_day`)——周五收盘已结算,直接命中周五的云端 metrics/RS CSV 全覆盖运行,不裁剪 K 线也不跳过 HSI 条件 RS 组;平日假期没有日历数据,仍走 404 → 本地回退。

**OpenD 软依赖**:HK 长线侧的 k 线与 HSI 历史都来自 yfinance,所以 OpenD 挂掉不会清空 .txt 文件。OpenD 不在线时,市值取不到会变 NaN(cap ≥ HK$300M 的基线会把所有票筛掉),条件 RS 的 HSI 触发快照和 Futu 同步都跳过,但排序与写文件本身照常跑完;OpenD 在线时则完整填充。每个策略写进各自的 append-only Futu 分组(`HKEarningsGap`、`HKHighVolume`、`HKGapUp`、`HKLeaders`、`HKRS`),这些分组须在首次运行前于 Futu PC 客户端手动建好。

### HK IPO(自动收集的 sidecar)

与 US IPO sidecar 对应。收集 HKEX 主板 universe 里 yfinance 有返回、但日线收盘不足 253 行(不够做 IBD 12 个月 RS 计算)的 ticker——几乎都是刚进 yfinance、还没攒满 12 个月数据的 HK 新股。

- **基线按历史深度分级、逐档启用。** 每道闸门只在 ticker 攒够数据后才生效——过了 20 交易日底线(与 US sidecar 对齐)的新股仍能较早浮现,上市 200 天的则要通过几乎完整的长线基线:

  | 闸门         | 阈值         | 条件                                                               |
  | ------------ | ------------ | ------------------------------------------------------------------ |
  | min history  | ≥ 20 交易日  | 总是(头 19 天成交量噪声太大)                                       |
  | cap          | ≥ HK$300M    | 总是                                                               |
  | price        | ≥ HK$20      | 总是                                                               |
  | avg vol      | ≥ 500K 股/天 | 仅当 ≥ 20 交易日                                                   |
  | $vol         | ≥ HK$100M    | 仅当 ≥ 20 交易日                                                   |
  | ADR%         | ≥ 3.0%       | 仅当 ≥ 20 交易日                                                   |
  | above SMA50  | —            | 仅当 ≥ 50 交易日                                                   |
  | above SMA200 | —            | 仅当 ≥ 200 交易日                                                  |
  | 3M RS        | ≥ 90(vs HSI) | 仅当 ≥ 64 交易日(12M 按定义跳过;≥ 64 天却不在云端表里的票会被丢弃) |

  各档阈值直接读 `[hk_settings]`,与 HK 长线侧基线保持一致,历史攒满 253 行时可无缝晋升。

- **输出:** `output/TV/HK/<date>_IPO.txt`,镜像到 Webull。
- **独立跨日 master:** `output/state/eod_seen_HKIPO.txt`。一旦新股攒到 ≥ 253 行,就从 IPO 桶里退出,在第一个合格日落进本该属于的长线侧分组(长线侧 master `eod_seen_HK.txt` 与之分开,互不污染)。
- **Futu 分组:** append-only `HKIPO`——首次运行前须在 Futu PC 客户端手动建好。

### Morning Gap(盘前 + 盘中,每日 9 次扫描)

两阶段盘中缺口扫描器。每轮扫描各写各的按 offset 留档文件(0 结果的扫描不生成文件):**盘前(开盘前 20/10/5 分钟)**写 `MorningGapPre{20,10,5}.txt`;**盘后(开盘后 5/10/15/20/25/30 分钟)**写 `MorningGap{5..30}.txt`,并多加一层盘中累计量闸门,专挑开盘头 30 分钟成交量就已追平 20 日均日量的股票——按 Kullamägi 的判断,这是催化剂驱动、机构进场的信号。

**Phase 1 — Futu 快照 discovery(取代 Finviz `ta_topgainers`——后者按常规盘 perf 排名、错过盘前 gapper):**

| 过滤       | 阈值                                                | 数据源                     |
| ---------- | --------------------------------------------------- | -------------------------- |
| Universe   | NASDAQ / NYSE / AMEX, listed, `stock_type = STOCK`  | Futu `get_stock_basicinfo` |
| Market Cap | ≥ $300M                                             | `total_market_val`         |
| Price      | ≥ $20                                               | `last_price`               |
| Gap(盘前)  | `pre_change_rate` ≥ 5%(且 `pre_volume > 0`)         | `pre_change_rate`          |
| Gap(盘后)  | `(last_price − prev_close) / prev_close × 100` ≥ 5% | 由快照推导                 |

**Phase 2 — yfinance 后处理 + Futu 盘中量:**

| 过滤                | 阈值                                                                              | 盘前 | 盘后 |
| ------------------- | --------------------------------------------------------------------------------- | ---- | ---- |
| Dollar Volume       | ≥ $100M(20 日均量)                                                                | ✓    | ✓    |
| ADR%                | ≥ 4.0%(20 日);gap ≥ 10% 时门槛放宽到 3.0%                                         | ✓    | ✓    |
| SMA50 / SMA200 趋势 | **实时价**(盘前价 / `last_price`)站上两者;gap ≥ 10% 仅豁免 SMA50——SMA200 永不豁免 | ✓    | ✓    |
| 20 日 Avg Volume    | ≥ 500K 股/天                                                                      | ✓    | ✓    |
| 盘中累计量          | 自 9:30 ET 起的 RTH 累计量 ≥ 20 日均日量                                          | —    | ✓    |

趋势闸的比较基准是实时价(`sma_use_live_price`,设 `false` 恢复旧的昨收基准);SMA50/SMA200 均线本身始终由已完成日线计算。SMA50 豁免(`sma_bypass_gap_percent`)和 ADR% 放宽(`adr_bypass_gap_percent` / `adr_bypass_min_percent`)都是按市场配置、仅作用于 morning-gap 的旋钮——EOD 闸门不受影响。

**HK 变体(`hk-morning-gap`)**:仅盘后(Futu 无港股盘前数据)——09:30 HKT 开盘后 +10…+60 分钟共 6 轮扫描,各写 `HKMorningGap{10..60}.txt`。基线对齐 `[hk_settings]`(gap ≥ 5%、ADR% ≥ 3.0%),同样使用实时价趋势闸和 SMA50 豁免;累计量闸门用 Futu 快照的当日成交量(无 yfinance 1 分钟兜底)。ADR 旁路代码已接线但配置未启用——HK 基线本来就是 3.0%。

需要 FutuOpenD 在线,并具备 US Lv1 BBO 实时报价权限;否则 Phase 1 discovery 和盘后量过滤都会返回空,且没有 Finviz 兜底。每轮扫描一旦发现*新*票(当天更早的扫描里没出现过),就推一条 ntfy 通知。

## 每日 CANSLIM 报告

美股 EOD 跑完后,`--mode report --market us` 会读取当天带日期的长线侧 `.txt` 文件,按分组优先级排序、每个市场上限 30 只,再调用所配置的 LLM 后端,为每只 ticker 生成 CANSLIM 风格的基本面加展望简报。输出为自包含的 `output/Reports/PostMarket/<date>_{us,hk}.html`(CSS 内联、无外部依赖,双击即可在任意浏览器打开),不再生成 Markdown。只有美股报告在排程里(`run_eod.sh`);`run_hk_eod.sh` 已跳过 report 步骤,`--market hk` 仅供手动调用。

**后端(`[report] backend`,大小写不敏感;全部走 Anthropic Python SDK):**

| 后端                                      | Web 上下文                             | 模型                                                                                                        | 密钥                                                                          |
| ----------------------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `deepseek`(**shipped 默认**)              | 手动 tool-loop → Tavily 搜索           | `deepseek-v4-pro`(Anthropic 兼容端点)                                                                       | `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`                                         |
| `anthropic`(`backend` 未配置时的代码默认) | 原生 `web_search_20250305` server tool | `claude-sonnet-4-6`                                                                                         | `ANTHROPIC_API_KEY`                                                           |
| `kimi` / `glm` / `minimax`                | 手动 tool-loop → Tavily 搜索           | `kimi-k2-turbo-preview` / `glm-4.6` / `MiniMax-M2`(各厂商的 Anthropic 兼容端点,可经 `[report.<name>]` 覆盖) | `MOONSHOT_API_KEY` / `ZHIPUAI_API_KEY` / `MINIMAX_API_KEY` + `TAVILY_API_KEY` |

| 方面              | 细节                                                                                                                                                                                                                                                                                                      |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **输入 (US)**     | 8 个带日期文件:`EarningsGap`, `HighVolume`, `Leaders`, `GapUp`, `NewHigh52W`, `IPO`, `TopGainers`, `RS`                                                                                                                                                                                                   |
| **输入 (HK)**     | 6 个带日期文件:`EarningsGap`, `HighVolume`, `Leaders`, `GapUp`, `IPO`, `RS`(无 NewHigh52W / TopGainers)                                                                                                                                                                                                   |
| **上限 & 优先级** | 30 只/市场(`MAX_TICKERS_PER_REPORT`);`EarningsGap > HighVolume > Leaders > GapUp > NewHigh52W > IPO > TopGainers > RS`。溢出的列在 "Truncated" 尾部小节。                                                                                                                                                 |
| **结构化字段**    | US 基本面 **SEC EDGAR companyfacts 优先**、yfinance 逐字段兜底(缓存于 `output/state/edgar_cache/`);HK 直接用 yfinance。字段:Market Cap, Price, EPS(最新季 + YoY), Revenue(最新季 + YoY), **5 年年度 YoY + 最近 4 季 YoY 轨迹**(两者), PE, ROE, Inst. Hold %, 最近财报日。RS 百分位取自缓存的 IBD/HSI 表。 |
| **定性小节**      | 模型生成,每只 ticker 最多 2 次 web 搜索(`web_search_max_uses` / `max_search_calls`):公司速览, 基本面/财报, 竞争力, 政策/政府支持, 新产品/催化剂, 风险点, 综合判断。                                                                                                                                       |
| **双语**          | 快照字段保持英文/数字;定性分析用简体中文。                                                                                                                                                                                                                                                                |
| **软失败**        | 与 Futu-sync 契约一致——wrapper 退出码只反映 EOD 步。缺后端密钥(各后端所需见上表)→ 该步跳过并 warning,`.txt` 产物不受影响。4xx 配置错误快速失败,给出独立的 `[配置错误]` 占位;5xx/429/超时重试一次后回退到 `[分析失败]`。                                                                                   |
| **排除**          | US Shorts, HK Shorts, Morning Gap——技术/盘中打法,基本面不驱动入场。                                                                                                                                                                                                                                       |
| **成本区间**      | DeepSeek + Tavily ~$0.5/天/市场(默认,便宜约 80%);Anthropic 原生 `web_search` ~$1–2/天/市场。                                                                                                                                                                                                              |

**配置:** 把后端密钥写进 `.env`(可从 `.env.example` 拷一份,里面按后端列全了所需密钥):默认的 DeepSeek 后端需要 `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`,Anthropic 后端需要 `ANTHROPIC_API_KEY`,Kimi/GLM/MiniMax 后端需要对应厂商密钥 + `TAVILY_API_KEY`。wrapper 脚本(`scripts/run_eod.sh` / `scripts/run_hk_eod.sh`)会在报告步之前 `source .env`;交互式运行时 `report/state.py` 也会自动从项目根加载 `.env`。

### 盘前 catalyst 报告

一份**独立**的短报告。当盘前扫描发现新的 US gapper 时,由 morning-gap 路径 **spawn 出一个 detached 子进程**来生成(`[morning_gap_catalyst]`),绝不能阻塞 morning-gap 主进程。无论 `[report] backend` 配成什么,它都**固定用 DeepSeek + Tavily**,且只读 JSON 快照 sidecar(不碰 Futu / yfinance)。输出为 `output/Reports/PreMarket/<date>_us_premarket.html`,盘前任一扫描(-20/-10/-5)发现新票都会触发,多次扫描间基于 `output/state/` 下的 markdown 累积文件重新渲染(单次上限 `max_tickers_per_run`,默认 10;每只 ticker 最多搜索 `max_search_calls` = 3 次)。写完后再推一条 "Catalyst Report Ready" 的 ntfy 通知,附上报告路径。

## Dedup(去重)

- **Longs 内部** — 5 个策略互斥(优先级 `EarningsGap > HighVolume > GapUp > NewHigh52W > TopGainers`)。
- **跨组** — 长线侧优先级 `Longs > Leaders > RS`。
- **跨日 master** — `output/state/eod_seen_{US,HK,IPO,HKIPO}.txt`。每只 ticker 首次出现时进且仅进一个长线侧分组;后续运行只发*新*票。两个市场互相独立;IPO/HKIPO 各有自己的 master,这样一只晋升的票之后能出现在它本该属于的分组。删文件即重置。
- **SMA50 自动清理(仅美股)** — 每次 `us-eod` 开头、加载 master 之前,master 里凡是收盘价**连续 2 个完整交易日低于 SMA50**(`[sma50_prune] consecutive_days`)**且最新收盘低于前一日收盘**(仍在下跌——线下反弹的票保留)的票会被自动从 `eod_seen_US.txt` 移除,之后可在未来的 EOD 重新合格、重新出现。移除前先备份(`eod_seen_US.txt.bak.<stamp>`)。软失败:yfinance 整体失败则跳过本次清理;历史缺失/过短的票保留不动。
- **RS-line audit 裁剪(手动)** — `--mode rs-line-audit` 按 RS 线趋势给 master 打分并 y/N 确认后裁剪(见 RS-line 章节)。每日定时跑的是 `--dry-run`,绝不动 master。
- **不进跨日 master**:Shorts, Morning Gap。对它们来说重新检测才有意义。
- **EOD Repeat(美股)** — 会被 master 压掉、但今天又命中事件组的老票,汇总浮现在 `<date>_Repeat.txt`(对 master 只读;见 EOD Repeat 一节)。

## 输出 (Output)

```
output/
├── TV/                        # 逗号分隔,供 TradingView "Import list..."
│   ├── US/<date>_{EarningsGap,HighVolume,GapUp,NewHigh52W,TopGainers,Leaders,Shorts,RS,IPO,Repeat,MorningGapPre{20,10,5},MorningGap{5..30}}.txt
│   ├── US/rs_us_<date>.txt    # 每日 RS 最强 top-10 快照,美股(来自定时 rs-line audit)
│   └── HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,Shorts,RS,IPO,HKMorningGap{10..60}}.txt
├── Webull/                    # 换行分隔镜像,供 Webull "Upload as File"
│   ├── US/<date>_*.txt
│   └── HK/<date>_*.txt
├── Reports/                   # 只有 HTML;保留 7 天
│   ├── PostMarket/<date>_us.html      # 每日 CANSLIM 简报(仅美股)
│   └── PreMarket/<date>_us_premarket.html   # 盘前 catalyst 报告
├── hk_rs_<date>.txt           # 每日 RS 最强 top-10 快照,港股
├── rs_line_audit_{US,HK}_<date>{,_drop,_keep_ranked}.txt   # audit 报告 + sidecar
└── state/                     # 跨日 "seen" master、RS 表缓存、morning-gap 每日 seen、EDGAR 缓存
    ├── eod_seen_US.txt        # US 长线侧 master(5 Longs 组 + Leaders + RS)
    ├── eod_seen_HK.txt        # HK 长线侧 master(EarningsGap/HighVolume/GapUp/Leaders/RS)
    ├── eod_seen_IPO.txt       # US IPO sidecar(独立——就绪时晋升进 US 分组)
    ├── eod_seen_HKIPO.txt     # HK IPO sidecar(独立——就绪时晋升进 HK 分组)
    ├── morning_gap_seen_{pre,post}_<date>.txt   # US MorningGap 每日去重(盘前/盘后,每日自动重置)
    ├── hk_morning_gap_seen_post_<date>.txt      # HK MorningGap 每日去重(仅盘后,与 US 独立)
    ├── ntfy_last_seen.txt           # ntfy 订阅器的断点续传进度(Unix 时间戳)
    ├── rs_rating_<date>.csv         # US 12M IBD RS 百分位缓存(来自 Fred6725/rs-log)
    ├── rs_rating_3m_<date>.csv      # US 3M RS 云端 CSV 的本地缓存(raw_score + 百分位, vs SPY)
    ├── hk_rs_rating_<date>.csv      # HK RS 云端 CSV 的本地缓存(12M + 3M, vs HSI)
    ├── hk_metrics_<date>.csv        # HK 长线 metrics 帧本地缓存(云端 data/hk_metrics/)
    ├── premarket_catalyst_<date>.md # Reports/PreMarket/ 背后的 markdown 累积文件(-20/-10/-5 间追加;保留 2 天)
    └── edgar_cache/                 # SEC EDGAR companyfacts 缓存(CANSLIM 报告用)
```

每次运行都会为每个分组写一个全新的带日期 `.txt`;结果为空时**不写文件**(不留 0 字节文件),Futu 同步也会**跳过**,以免在休市日清掉已有分组。

**TradingView ticker 格式:** US 分组用 `NASDAQ:AAPL` / `NYSE:WMT` / `AMEX:GLD`(Finviz 派生)。HK 分组用 `HKEX:NNN` 并**去掉前导零**——TradingView 会静默拒绝 `HKEX:0148` 这种,必须是 `HKEX:148`。≥ 1000 的代码(4 位)原样写:`HKEX:1810`(小米)、`HKEX:9988`(阿里)。< 1000 的代码去掉补位:`HKEX:148`(建滔集团)、`HKEX:522`(ASMPT)、`HKEX:700`(腾讯)。

## Futu 自动同步

在 `config.toml` 里配 `[futu]`。同步 hook 会在每次自选写成功后触发,失败只记 warning,绝不阻塞 `.txt` 输出。

**一次性设置:**

1. 启动 [FutuOpenD](https://openapi.futunn.com/futu-api-doc/intro/intro.html),登录(默认 `127.0.0.1:11111`)。
2. 在 Futu PC 客户端手动建这些自定义分组(API 只能改已存在的分组,不能新建):
   `EarningsGap`, `HighVolume`, `GapUp`, `NewHigh52W`, `TopGainers`, `Leaders`, `Shorts`, `RS`, `IPO`(US)。

多数 EOD 分组是 append-only——太满时在客户端手动清空(Futu 上限:非交易户 500/组,活跃交易户 2000)。

## TradingView 自动同步(可选,`tv_sync.py`)

`[tv_sync]`(默认 **`enabled = false`**)把同一批自选同步到 TradingView 列表,走其**非官方 REST API**,用 `sessionid` cookie 认证。凭证读取顺序:先看环境变量(`TV_SESSIONID`、`TV_SESSIONID_SIGN`),再看 `~/.config/momentum-scanner/tv_cookie.json`。18 个列表须先在 TV 网页手动建好(名字区分大小写、精确匹配),找不到的名字会记 warning 并跳过。软失败契约与 Futu 一致——cookie 过期也绝不阻塞 `.txt` 输出。Append-only 语义走 TV 自己的 `[tv_sync].append_only_lists` 键(与 `[futu].append_only_groups` 同语义;建议两边保持一致,避免行为分裂);注意 TV 把 `MorningGap` 保留为独立列表,而 Futu 那边并入了 `EarningsGap`。

## 推送通知 (ntfy)

Morning-gap 扫描通过 [ntfy.sh](https://ntfy.sh) 推送四类通知:

- **常规**——本轮出现**新**票(当天同阶段更早的扫描里没见过的)时推一条,正文列出全部入选票;
- **PROMOTED(高优先级)**——盘前见过的 gapper 在盘后首次通过累计量闸门(盘前缺口被 RTH 成交量确认)时单独推一条;
- **Catalyst Report Ready**——盘前 catalyst 报告写完后推一条,附 `output/Reports/PreMarket/` 下的 HTML 路径;
- **SKIPPED(高优先级)**——定时扫描因网络迟迟不通(net-ready 闸)而干净退出、错过窗口时推一条,避免窗口丢失得无声无息。

RS workflow 自触发器(见"自动化"一节)的失败告警也复用同一 topic,所有 pipeline 告警落在同一频道。

在 `config.toml` 里配 `[notify]`,并在 ntfy 的 iOS/Android app 里订阅对应 topic。Mac 本机还有一个常驻 launchd 订阅器,把同一 topic 的消息桥接到 macOS 通知中心(见“自动化”一节)。

## 安装 (Setup)

```bash
uv sync                                              # 安装
uv run main.py --mode us-eod                         # US EOD (Longs/Leaders/Shorts/RS/IPO)
uv run main.py --mode hk-eod                         # HK EOD (Shorts + Longs/Leaders/RS)
uv run main.py --mode morning-gap                    # US 盘中缺口扫描(自动检测窗口,窗口外干净退出)
uv run main.py --mode hk-morning-gap                 # HK 盘中缺口扫描(仅盘后)
uv run main.py --mode report --market us             # 为今日 US 票生成 CANSLIM 简报 → Reports/PostMarket/(需后端密钥)
uv run main.py --mode report --market us --date YYYY-MM-DD   # 回填某一天(--market hk 也能跑,但不在排程内)
uv run main.py --mode rs-line-audit --market both    # 按 RS-line 趋势给跨日 master 打分,提示裁剪(手动)
```

> 不带后缀的 `--mode eod` 仍会 US、HK 一起跑,但计划槽用分市场的 `us-eod` / `hk-eod`(10:00 HKT 时 HK 的当日 bar 还没收完)。

## 自动化(macOS launchd + pmset)

两个每日 EOD 槽(按收盘时间拆分)、两个盘中 morning-gap 扫描器、两个 RS-workflow 自触发,外加一个常驻 ntfy 订阅器和一个每周唤醒重排,各自把日志写到 `output/` 下:

| 槽             | 触发                                 | Mode             | Plist                                         |
| -------------- | ------------------------------------ | ---------------- | --------------------------------------------- |
| US EOD         | Tue–Sat 10:00 HKT                    | `us-eod`         | `com.xue.finviz-to-tv.plist`                  |
| HK EOD         | Mon–Fri 20:00 HKT                    | `hk-eod`         | `com.xue.finviz-to-tv.hk-eod.plist`           |
| US Morning Gap | 90 条/周(ET-aware)                   | `morning-gap`    | `com.xue.finviz-to-tv.morning-gap.plist`      |
| HK Morning Gap | Mon–Fri × 6 offsets(9:40–10:30 HKT)  | `hk-morning-gap` | `com.xue.finviz-to-tv.hk-morning-gap.plist`   |
| US RS trigger  | Tue–Sat 08:45 HKT(`gh workflow run`) | —                | `com.xue.finviz-to-tv.us-rs-3m-trigger.plist` |
| HK RS trigger  | Mon–Fri 18:45 HKT(`gh workflow run`) | —                | `com.xue.finviz-to-tv.hk-rs-trigger.plist`    |
| ntfy 订阅器    | 常驻(KeepAlive)                      | —                | `com.xue.finviz-to-tv.ntfy-subscriber.plist`  |
| 唤醒重排       | 每周日 18:00(root LaunchDaemon)      | —                | `com.xue.finviz-to-tv.schedule-wakes.plist`   |

除唤醒重排装在 `/Library/LaunchDaemons/`(`pmset` 需要 root)外,其余 plist 都放在 `~/Library/LaunchAgents/`,源文件副本在 `scripts/`。ntfy 订阅器把 morning-gap topic 的每条消息桥接到 macOS 通知中心,并用 `output/state/ntfy_last_seen.txt` 记录进度,睡眠/重启后只补发漏掉的消息。两个 RS trigger 会在每个 EOD 前 **75 分钟**派发云端 RS/metrics workflow——因为 GitHub 自带的计划 cron 不可靠(观察到延迟数小时,甚至被跳过);好在 workflow 的 commit 步是幂等的,GH cron 与 launchd 双重触发也无害。

10:00 HKT 槽落在美股收盘之后(EDT、EST 两种夏令时都覆盖),也在每日上游 RS Rating commit 之后。20:00 HKT 槽在 HK 收盘(16:00 HKT)后留了 4 小时余量,等 k 线数据定稿。US 槽用 `--mode us-eod`,刻意跳过 HK——10:00 HKT 时 HK 才开市 30 分钟,当日 k 线 bar 还没收完。每个 EOD 步成功后,wrapper 脚本会以软失败方式调一次 `--mode report --market {us,hk}`,让当天的 CANSLIM 简报在同一窗口产出;这一步失败不影响 EOD 退出码。

```bash
# US 槽
sudo pmset repeat wakeorpoweron TWRFS 09:59:00
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.plist

# HK 槽(无 pmset——Mac 在 20:00 HKT 通常醒著;若睡著 launchd 在下次唤醒时触发)
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.hk-eod.plist

# Morning-gap(独立 plist,90 条日历项:Mon–Fri × 9 offsets × EDT/EST)
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.morning-gap.plist
sudo uv run scripts/schedule_morning_gap_wakes.py    # 排一次性唤醒(首次装机手动跑;之后由 schedule-wakes LaunchDaemon 每周日 18:00 自动重排)
```

morning-gap 脚本每次触发都会自校验 ET 时间,不在窗口内就直接干净退出。

## 导入 (Importing)

- **TradingView**:Watchlist → "Import list..." → 选最新的 `output/TV/{US,HK}/<date>_*.txt`。HK ticker 一律不带前导零(写 `HKEX:148` 而非 `HKEX:0148`),否则 TradingView 会静默拒绝。
- **Webull**:Watchlist → "Upload as File" → 从 `output/Webull/{US,HK}/` 选对应文件(换行分隔,逗号格式会被静默截断)。

## 配置 (Configuration)

所有 screener 过滤、阈值、Futu/ntfy 设置都在 `config.toml`。架构与贡献说明见 [`CLAUDE.md`](CLAUDE.md)。

## 依赖 (Dependencies)

Python ≥ 3.12(见 `pyproject.toml`)—— [finviz](https://github.com/mariostoev/finviz), [yfinance](https://github.com/ranaroussi/yfinance), [openpyxl](https://openpyxl.readthedocs.io/), [curl-cffi](https://pypi.org/project/curl-cffi/), [futu-api](https://pypi.org/project/futu-api/), [anthropic](https://pypi.org/project/anthropic/)(报告——所有后端都走它,非 Anthropic 厂商经各自的 Anthropic 兼容端点), [httpx](https://www.python-httpx.org/)(Tavily 搜索 + TV 同步), [markdown](https://pypi.org/project/Markdown/)。开发:pytest + pytest-asyncio。

## 参考资料 (References)

这里只列出了一些关键资源,并不完整——还有不少参考过的材料没有列出。

**书籍:**

- _How to Make Money in Stocks_ — William O'Neil(CANSLIM 与 IBD RS 体系的源头)
- _Victory in Stock Trading: Strategy and Tactics of the 2020 U.S. Investing Champion_ — Oliver Kell
- _Trade Like a Stock Market Wizard_ — Mark Minervini
- _Think & Trade Like a Champion_ — Mark Minervini
- _A Complete Guide to Volume Price Analysis_ — Anna Coulling
- _The Power of Japanese Candlestick Charts_ — Fred K.H. Tam
- _The Trader's Handbook: Winning Habits and Routines of Successful Traders_ — Richard Moglen, Nick Schmidt, et al.
- _Market Wizards: The Next Generation: The World's Top Young Traders Reveal How They Beat the Market_ — Jack D. Schwager, George F. Coyle

**网站与频道:**

- [Qullamaggie](https://qullamaggie.com/)
- [TraderLion](https://traderlion.com/)
- [Stockbee](https://stockbee.biz/)
- [Investor's Business Daily](https://www.youtube.com/@investorsbusinessdaily)(YouTuber)
- [Real Simple Ariel](https://www.youtube.com/@RealSimpleAriel)(YouTuber)
- [TheOneLanceB](https://www.youtube.com/@TheOneLanceB)(YouTuber)
- [TA Plot](https://www.youtube.com/@TAPlot)(YouTuber)
- [SMB Capital](https://www.youtube.com/@smbcapital)(YouTuber)
- [Qullamaggie](https://www.youtube.com/@Qullamaggie)(YouTuber)

**YouTube videos:**

- [The Simple Trading Setup That Made Lance Breitstein Millions](https://youtu.be/R215f4fj7V8)(TraderLion)
- [Trading Super-performance. Trade Like Market Wizard David Ryan](https://youtu.be/ZK5cnVQ2V3Q)(TraderLion)
- [How Hedge Fund Managers Trade Pullbacks — Exclusive with Charles Harris](https://youtu.be/ivL6E6Lc6gM)(TraderLion)
- [The Wedge Pop Trading Setup of Trading Champion Oliver Kell](https://youtu.be/m8F3KkBDtC0)(TraderLion)
- [The 10 Principles of Trading with Investing Champion Oliver Kell](https://youtu.be/ElocJ-b_NTs)(TraderLion)
- [How to Find and Trade the Next Tesla — Swing Trading Strategy](https://youtu.be/eu8onWJ5y34)(TraderLion)
- [The $1,000,000 Simple Trading System That Took 13 Years to Build](https://youtu.be/iu2gdI1cO88)(TraderLion)
- [Low Risk Stock Setups + PDF File](https://youtu.be/R5ScKXy1ytg)(TA Plot)
- [How To Pyramid Into Stocks (21 Stock Setup Examples + PDF File)](https://youtu.be/11h6iSQkzuA)(TA Plot)
- [Sitting Tight for the Right Low Risk Entry](https://youtu.be/Mt3iZ_Orv0g)(TA Plot)
- [How Do You Know It's Time to Get In a Stock? Analyzing Recent Trades.](https://youtu.be/hfwQUpEflEg)(TA Plot)

**Podcast:**

- [Stock Market Today With IBD](https://podcasts.apple.com/us/podcast/stock-market-today-with-ibd/id1685322096)
