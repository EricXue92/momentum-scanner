[English](README.md) | [繁體中文](README.zh-TW.md) | **简体中文**

# 每日选股扫描流水线 (Daily Stock Screener Pipeline)

一套定时运行的美股 + 港股动量扫描器。每天按 O'Neil / Kell / Kullamägi 的方法筛出做多与做空候选,给一份固定 ETF 名单做 3 个月相对强度排名,并导出 TradingView、Webull、Futu(富途牛牛)自选列表。

## 目录

- [概览](#概览)
- [快速开始](#快速开始)
- [美股筛选器](#美股筛选器)
  - [共用闸门](#共用闸门) · [各组 RS 闸](#各组-rs-闸) · [Longs](#longs) · [Leaders](#leaders) · [条件 RS 组](#条件-rs-组) · [美股 Shorts](#美股-shorts) · [美股 IPO](#美股-ipo) · [EOD Repeat](#eod-repeat) · [ETF 强度排名](#etf-强度排名) · [美股 Morning Gap](#美股-morning-gap)
- [港股筛选器](#港股筛选器)
  - [港股基线](#港股基线) · [港股长线组](#港股长线组) · [港股 Shorts](#港股-shorts) · [港股 IPO](#港股-ipo) · [港股 Morning Gap](#港股-morning-gap)
- [RS 数据](#rs-数据)
  - [云端 RS 表](#云端-rs-表) · [RS-line 标注](#rs-line-标注) · [RS-line 审计与每日 Top 10](#rs-line-审计与每日-top-10)
- [去重与裁剪](#去重与裁剪)
- [输出文件](#输出文件)
- [同步与通知](#同步与通知)
  - [Futu 同步](#futu-同步) · [TradingView 同步](#tradingview-同步) · [ntfy 推送](#ntfy-推送)
- [LLM 报告](#llm-报告)
  - [CANSLIM 报告](#canslim-报告) · [盘前 catalyst 报告](#盘前-catalyst-报告)
- [自动化](#自动化)
- [配置](#配置)
- [依赖](#依赖)
- [参考资料](#参考资料)

## 概览

**每天跑什么(时间均为 HKT):**

| 任务             | 时间                         | 产出                                                                               |
| ---------------- | ---------------------------- | ---------------------------------------------------------------------------------- |
| 美股 EOD         | 周二至周六 10:00             | Longs(6 组)、Leaders、条件 RS 组、Shorts、IPO、Repeat、**ETF 强度排名**、RS Top 10 |
| 港股 EOD         | 周一至周五 20:00             | EarningsGap / HighVolume / GapUp / Leaders / 条件 RS 组、Shorts、IPO、RS Top 10    |
| 美股 Morning Gap | 09:30 ET 开盘前后共 9 次扫描 | 盘前 gapper(−20/−10/−5 分钟)与经成交量确认的盘后 gapper(+5…+30 分钟),ntfy 推送     |
| 港股 Morning Gap | 09:30 HKT 开盘后 6 次扫描    | 盘后 gapper(+10…+60 分钟),ntfy 推送                                                |

**数据源:**

| 数据源                   | 用途                                                                                                          |
| ------------------------ | ------------------------------------------------------------------------------------------------------------- |
| Finviz                   | 美股选股 discovery(所有扫描都加 `ind_stocksonly`,不含 ETF/ETN)                                                |
| yfinance                 | 日线——dollar volume、ADR%、Rel Vol、SMA、涨幅;港股 k 线 + HSI 历史;ETF 排名                                   |
| Futu OpenD               | Morning Gap 快照(美股 + 港股)、精确市值、HSI 当日涨跌、自选分组同步                                           |
| GitHub Actions → `data/` | RS 百分位表(美股 3M、港股 12M+3M)与港股 metrics frame——家用 IP 跑 yfinance 会被限流,所以放云端计算,本地只拉取 |
| Fred6725/rs-log          | 美股 12M IBD 式 RS 百分位                                                                                     |
| HKEX 股票名单            | 港股主板 universe(约 2,400 只)                                                                                |

结果写成带日期的 `.txt` 自选列表(TradingView 用逗号分隔,Webull 用换行分隔),并自动同步到 Futu 分组;TradingView 列表同步为可选项。LLM 研究报告可按需手动生成([每日自动生成已关闭](#llm-报告))。

## 快速开始

```bash
uv sync                                              # 安装
uv run main.py --mode us-eod                         # 美股 EOD (Longs/Leaders/RS/Shorts/IPO/Repeat + ETF 排名)
uv run main.py --mode hk-eod                         # 港股 EOD (长线组 + Shorts + IPO)
uv run main.py --mode morning-gap                    # 美股缺口扫描 (不在 ET 窗口内会干净退出)
uv run main.py --mode hk-morning-gap                 # 港股缺口扫描 (仅盘后)
uv run main.py --mode etf-rs                         # 单独重跑 ETF 强度排名
uv run main.py --mode rs-line-audit --market both    # 按 RS-line 趋势给 master 打分;裁剪前会询问
uv run main.py --mode report --market us             # 为当天美股个股生成 CANSLIM 报告 (手动;需 API key)
uv run pytest tests/ -v                              # 测试
```

`--mode eod` 仍可同时跑美股 + 港股,但定时任务用分市场的模式——10:00 HKT 时港股日线尚未走完。

## 美股筛选器

### 共用闸门

Finviz 选股之后,基于 yfinance 日线套用。阈值在 `[settings]`。

| 闸门          | 阈值                                                            | 适用范围                                     |
| ------------- | --------------------------------------------------------------- | -------------------------------------------- |
| Dollar Volume | 价 × 20 日均量 ≥ $100M                                          | Longs、Leaders、RS、Shorts、IPO、Morning Gap |
| ADR%          | 最近 20 根完整 bar 的 mean(`(High − Low) / Close`) × 100 ≥ 4.0% | 同上                                         |

ADR%(Kullamägi 式)衡量一只股票**当下**的波动幅度;它取代了过去的 Finviz `beta > 1.5` 过滤——后者容易误杀正活跃的中大盘票。

### 各组 RS 闸

口径:**事件组看长期强度(12M ≥ 90),其余看近期强度(3M ≥ 90)。** 每组一个独立旋钮,设 `0` 即关闭该层。

| 分组                                                              | 12M 闸                             | 3M 闸                           |
| ----------------------------------------------------------------- | ---------------------------------- | ------------------------------- |
| Longs: EarningsGap / HighVolume / GapUp / NewHigh52W / TopGainers | `min_rs_percentile_longs` = **90** | —                               |
| Longs: TheSetup                                                   | 关(组内 `min_rs_percentile = 0`)   | —                               |
| Leaders                                                           | `min_rs_percentile` = 0(关)        | `min_rs_percentile_3m` = **90** |
| 条件 RS 组                                                        | `min_rs_percentile_rs` = 0(关)     | **90**                          |
| 美股 Shorts                                                       | `min_rs_percentile_shorts` = 0(关) | **90**                          |
| 美股 IPO(历史 ≥ 64 天)                                            | —                                  | **90**                          |
| Morning Gap、ETF 排名                                             | —                                  | —                               |

- RS 表里**查不到**的 ticker 保留,不丢弃。
- RS 表拉取失败时先回退最多 3 天的缓存,再不行就不过闸直接放行并告警——绝不硬失败。
- `min_rs_percentile_rs` / `_shorts` **不配置**时会继承 Longs 的值;想保持关闭必须显式写 `0`。

### Longs

Oliver Kell 的动量/突破 setup。6 组互斥——靠前的组优先,每只 ticker 每天最多进一个 Longs 文件。共同条件:Small Cap+、Avg Vol > 500K、站上 SMA50 与 SMA200,再加共用闸门。

| 优先级 | 分组          | 额外过滤                                                                                          |
| ------ | ------------- | ------------------------------------------------------------------------------------------------- |
| 0      | `TheSetup`    | Price > $10、Gap Up 5%+、Rel Vol ≥ 3× 20 日均量(yfinance)。**不过 RS 闸**——放量大缺口本身就是信号 |
| 1      | `EarningsGap` | Price > $20、Earnings Today、Rel Vol > 1.5、Gap Up 5%+                                            |
| 2      | `HighVolume`  | Price > $20、Day Up、Rel Vol ≥ 3× 20 日均量(yfinance)                                             |
| 3      | `GapUp`       | Price > $20、Gap Up 3%+                                                                           |
| 4      | `NewHigh52W`  | Price > $20、52 周新高                                                                            |
| 5      | `TopGainers`  | Price > $20、Finviz 信号 Top Gainers                                                              |

### Leaders

长期趋势领头羊:Small Cap+、Avg Vol > 500K、Price > $20、站上 SMA50 与 SMA200、共用闸门、RS 3M ≥ 90。5 个涨幅窗口合并写入同一个 `Leaders.txt`:

| 4 周   | 13 周  | 26 周   | YTD     | 52 周   |
| ------ | ------ | ------- | ------- | ------- |
| ≥ +30% | ≥ +50% | ≥ +100% | ≥ +100% | ≥ +150% |

### 条件 RS 组

弱市里扛得住的股票。**仅当 SPY 与 QQQ 当日都跌 ≥ 1.0% 时才运行。** 过滤:Small Cap+、Avg Vol > 500K、Price > $20、Day Up、站上 SMA50 与 SMA200、共用闸门、RS 3M ≥ 90。

### 美股 Shorts

Kullamägi 的抛物线冲顶做空 setup。每天重新检出(不参与任何去重)。

1. **Finviz:** 价格高于 SMA20 20%+、站上 SMA50、Avg Vol > 1M、市值 > $50M → 再用 **RS 3M ≥ 90** 缩小名单。
2. **yfinance + Futu 市值**,依次过:

| 过滤                | 阈值                                                                               |
| ------------------- | ---------------------------------------------------------------------------------- |
| 涨幅                | 2、3 或 4 周内上涨:**50%+**(市值 ≥ $10B)/ **200%+**($2B–$10B)/ **300%+**($50M–$2B) |
| Dollar Volume、ADR% | ≥ $100M、≥ 4.0%                                                                    |
| 连续上涨天数        | ≥ 3 天(不含当天未走完的 bar)                                                       |

市值取自 Futu 快照(精确值;Finviz 的 `"1.23B"` 这类字符串在分级边界附近容易分错档),取不到再回落 Finviz。

### 美股 IPO

自动收集的 sidecar:已通过 Longs/Leaders/RS 的 Finviz 筛选、却因历史太短被 yfinance 丢掉的候选。闸门随历史长度逐级生效,所以上市 30 天的新股也能浮出,而上市 200 天的则要过几乎完整的基线。港股 IPO 的 ladder 完全相同,只是阈值不同:

| 闸门                   | 美股             | 港股               | 生效条件 |
| ---------------------- | ---------------- | ------------------ | -------- |
| 历史长度               | ≥ 20 个交易日    | ≥ 20 个交易日      | 始终     |
| 市值                   | ≥ $300M          | ≥ HK$300M          | 始终     |
| 价格                   | ≥ $20            | ≥ HK$20            | 始终     |
| 日均量 / Dollar Volume | ≥ 500K / ≥ $100M | ≥ 500K / ≥ HK$100M | ≥ 20 天  |
| ADR%                   | ≥ 4.0%           | ≥ 3.0%             | ≥ 20 天  |
| 站上 SMA50             | ✓                | ✓                  | ≥ 50 天  |
| RS 3M                  | ≥ 90(对 SPY)     | ≥ 90(对 HSI)       | ≥ 64 天  |
| 站上 SMA200            | ✓                | ✓                  | ≥ 200 天 |

- 美股新股不在 RS universe 里,所以 3M 分数在本地计算,再排进云端表的 `raw_score` 分布里取百分位。
- 出现在 12M RS 表里的 ticker 必然有 ≥ 12 个月历史,不可能是新股——会被移出 IPO 桶(只是 yfinance 临时缺数据)。
- 独立 master `eod_seen_IPO.txt`,所以新股"毕业"后仍能进入它应属的分组。输出:`<date>_IPO.txt`,Futu 分组 `IPO`。

### EOD Repeat

已在跨日 master 里、**今天又命中事件组**(TheSetup / EarningsGap / HighVolume / GapUp / TopGainers)的老票,汇总到 `<date>_Repeat.txt` 供复查。NewHigh52W 与 Leaders 不算——它们是持续状态,会天天重复命中。对 master 只读;各组自己的文件仍保持"只出新票"的含义。Futu/TV 映射默认注释掉(要先手建 `Repeat` 分组)。配置:`[eod_repeat]`。

### ETF 强度排名

每天一份行业 / 主题轮动视图:一份约 65 只的固定 ETF 名单(美股行业与主题、商品、杠杆产品、全球市场),按 3 个月相对强度排名。

- **打分:** 与个股相同的 3M RS 公式(`0.5·R21 + 0.3·R42 + 0.2·R63`,相对 SPY)。百分位是**在 ETF 名单内部**排的,不是对个股 universe。
- **何时跑:** 每次 `us-eod` 末尾的 soft 步骤(失败不影响 EOD);也可用 `--mode etf-rs` 单独重跑。本地计算——一次 yfinance 批量下载,不走云端。
- **输出:** `output/TV/US/<date>_ETF_rs.txt`,每行一只 ETF,**最强的在最上面**,格式 `TICKER - 中文名 | 前五大持仓`:

  ```
  NRGU - 油气 3 倍做多 | VLO、MPC、PSX、CVX、DVN (ETN 挂钩指数前五成分股)
  ARKG - 基因组革命 | TXG、TWST、TEM、CRSP、PSNL
  USO - 原油 | 不适用: 原油期货及现金 / 国债抵押品
  ```

- **给人看的,不是 TradingView 导入文件**——它是 `TV/US/` 里唯一非逗号分隔的 `.txt`。带分数的完整表(排名 / 分数 / 百分位)在 EOD 日志里。
- **维护:** 增删改 `[etf_rs.tickers]`(`TICKER = "中文名"`)即可调整名单。持仓来自静态、手工维护的 `[etf_rs.holdings]` 表(不走 API 刷新;没配的票不显示持仓段)。
- **同名即同一品种:** 中文名完全相同的 ticker 只显示最强的一只(曾经的 GDXU / NUGT 即是一例;当前表里已无同名对,规则保留);被隐藏的会列在日志里。
- 仅为排名快照:同日重跑直接覆盖;不去重、不镜像 Webull、不同步 Futu/TV。

### 美股 Morning Gap

每天 9 次扫描:**盘前** −20/−10/−5 分钟 → `MorningGapPre{20,10,5}.txt`;**盘后** +5…+30 分钟 → `MorningGap{5..30}.txt`。每次扫描各写各的快照;没扫到就不写文件。不过 RS 闸。

**Phase 1 — Futu 快照 discovery**(NASDAQ / NYSE / AMEX 普通股):

| 过滤       | 阈值                                          |
| ---------- | --------------------------------------------- |
| 市值、价格 | ≥ $300M、≥ $20                                |
| Gap(盘前)  | `pre_change_rate` ≥ 5%,且盘前有成交           |
| Gap(盘后)  | `(last_price − prev_close) / prev_close` ≥ 5% |

**Phase 2 — yfinance + Futu 成交量:**

| 过滤                | 阈值                                                                 | 盘前 | 盘后 |
| ------------------- | -------------------------------------------------------------------- | ---- | ---- |
| 20 日均量           | ≥ 500K 股/天                                                         | ✓    | ✓    |
| 盘前成交量          | Futu `pre_volume` ≥ 5% × 20 日均量——剔除只靠几百股撑出来的薄盘假 gap | ✓    | —    |
| Dollar Volume       | ≥ $100M                                                              | ✓    | ✓    |
| ADR%                | ≥ 4.0%;gap ≥ 10% 时放宽到 3.0%                                       | ✓    | ✓    |
| SMA50 / SMA200 趋势 | **实时价**站上两条均线;gap ≥ 10% 只豁免 SMA50——SMA200 永不豁免       | ✓    | ✓    |
| 盘中累计量          | 09:30 ET 起的累计成交量 ≥ 20 日均量(机构买入的特征)                  | —    | ✓    |

均线本身始终用已完成的日线计算;只有比较基准用实时价。所有旋钮都在 `[morning_gap]`(`min_pre_volume_ratio`、`sma_use_live_price`、`sma_bypass_gap_percent`、`adr_bypass_*`),只影响 Morning Gap。需要 FutuOpenD 在线并有美股 Lv1 行情——没有 Finviz 兜底。

## 港股筛选器

方法与美股一致,阈值用 HKD,universe = HKEX 主板。输出为 TradingView 的 `HKEX:NNN` 格式,去掉前导零。

### 港股基线

5 个长线组共用(`[hk_settings]`):

| 闸门                 | 阈值                                             | 说明                     |
| -------------------- | ------------------------------------------------ | ------------------------ |
| 市值                 | ≥ HK$300M                                        | 取自 Futu 快照           |
| 日均量               | ≥ 500K 股/天(20 日)                              |                          |
| Dollar Volume        | ≥ HK$100M(20 日)                                 |                          |
| ADR%                 | ≥ 3.0%                                           | 港股蓝筹波动率结构性偏低 |
| 价格                 | ≥ HK$20                                          |                          |
| 站上 SMA50 与 SMA200 | 两条都要                                         |                          |
| RS(对 HSI)           | 事件组 **12M ≥ 90**; Leaders / RS 组 **3M ≥ 90** | 与美股的分工一致         |

### 港股长线组

按优先级排序;每只 ticker 每天最多进一个文件。

| 优先级 | 分组           | 额外闸门                                                            |
| ------ | -------------- | ------------------------------------------------------------------- |
| 1      | HK EarningsGap | gap ≥ 3% + Rel Vol ≥ 3(用形态代替——港股没有财报日历)                |
| 2      | HK HighVolume  | Rel Vol ≥ 3                                                         |
| 3      | HK GapUp       | gap ≥ 3%                                                            |
| 4      | HK Leaders     | 任一:4 周 +30% / 13 周 +50% / 26 周 +100% / YTD +100% / 52 周 +150% |
| 5      | HK RS          | 当日收红;**仅当 HSI 当日跌 ≥ 1.0% 时才运行**(`hsi_rs_trigger`)      |

- **数据:** metrics frame 与 RS 表从云端拉取(`data/hk_metrics/`、`data/hk_rs/`);云端取不到时回退到本地 yfinance 抓取。
- **数据日规则:** 只有 20:00 这一档用当天收盘;更早的运行会裁掉当天未走完的 bar,并跳过 HSI 触发的 RS 组。周末补跑映射到上周五。
- **OpenD:** 市值和 HSI 触发来自 Futu。OpenD 不在线时流程照常跑完,但市值闸会把所有票筛掉。

### 港股 Shorts

与美股 Shorts 相同,换成 HKD:RS 3M ≥ 90(对 HSI,先筛 universe)、市值 ≥ HK$50M、日均量 ≥ 1M 股/天、dollar volume ≥ HK$100M、ADR% ≥ 4.0%、涨幅按市值分级 50% / 200% / 300%(≥ HK$10B / HK$2B–10B / HK$50M–2B)、连续上涨 ≥ 3 天。配置:`[hk_shorts]`。

### 港股 IPO

日线收盘数不足 253 根的主板 ticker(不够算 12M RS)。与美股相同的按历史分级 ladder——见[美股 IPO](#美股-ipo)的表。历史 ≥ 64 天却不在云端 3M 表里的票会被丢弃。独立 master `eod_seen_HKIPO.txt`;输出 `<date>_IPO.txt`,Futu 分组 `HKIPO`。

### 港股 Morning Gap

仅盘后(Futu 没有港股盘前数据):09:30 HKT 开盘后 +10…+60 分钟共 6 次扫描 → `HKMorningGap{10..60}.txt`。Gap ≥ 5%,基线同 `[hk_settings]`(ADR% ≥ 3.0%),实时价趋势闸与 SMA50 豁免同美股;累计量闸用 Futu 快照的当日成交量。配置:`[hk_morning_gap]`。

## RS 数据

### 云端 RS 表

| 表       | 公式                                               | 基准 | 发布位置                                                                            |
| -------- | -------------------------------------------------- | ---- | ----------------------------------------------------------------------------------- |
| 美股 12M | `0.4·P3 + 0.2·P6 + 0.2·P9 + 0.2·P12`               | SPY  | [Fred6725/rs-log](https://github.com/Fred6725/relative-strength)(工作日 ~01:30 UTC) |
| 美股 3M  | `0.5·R21 + 0.3·R42 + 0.2·R63`,universe 约 6,100 只 | SPY  | `data/us_rs_3m/<date>.csv`(GitHub Actions)                                          |
| 港股     | 两条公式都算,在港股主板内部排名                    | HSI  | `data/hk_rs/<date>.csv`(GitHub Actions)                                             |

本地流水线只拉取这些 CSV(缓存在 `output/state/`,拉不到就回退最多 3 天)。GitHub 自带的 cron 不可靠,所以由 launchd 在每次 EOD 前 75 分钟主动触发 workflow——见[自动化](#自动化)。

### RS-line 标注

云端 CSV 还带 `rs_below_ma` / `rs_days_below_ma` / `rs_frac_below_ma` 三列:TraderLion 式 **RS line**(价 ÷ 基准)与它自己的 EMA21 的比较。EOD 日志会标注 RS line 持续位于均线下方的长线侧入选票。**仅写日志**——不影响 `.txt` 输出和去重。配置:`[rs_line]`。

### RS-line 审计与每日 Top 10

`uv run main.py --mode rs-line-audit [--market us|hk|both] [--dry-run|--yes]` 按 RS-line 趋势给跨日 master 里的每只票打分,把报告和 `_drop` / `_keep_ranked` sidecar 写到 `output/rs-audit/`。

- **裁剪是手动的:** 命令会先询问 y/N,确认后才把要裁的票从 `eod_seen_{US,HK}.txt` 移除(先备份为 `.bak.<stamp>`),被裁的票日后可重新入选。`--yes` 跳过询问;`--dry-run` 绝不动 master。
- **每日 Top 10:** 每个 EOD wrapper 都以 soft 步骤追加跑一次 `--dry-run` 审计,把最强的 `[rs_line].top_n`(10)只票写到 `output/TV/US/rs_us_<date>.txt`(可直接导入 TradingView)和 `output/hk_rs_<date>.txt`。为空则不写;同日重跑直接覆盖。

## 去重与裁剪

1. **Longs 内部**——靠前的组优先(`TheSetup > EarningsGap > HighVolume > GapUp > NewHigh52W > TopGainers`)。
2. **跨组**——Longs 优先于 Leaders(港股:当日内 `EarningsGap > HighVolume > GapUp > Leaders > RS`)。
3. **跨日 master**——`output/state/eod_seen_{US,HK,IPO,HKIPO}.txt`。每只票只在首次出现时输出一次,之后的运行只出**新**票。各市场互相独立。删除该文件即重置。

**从不与 master 比对**(要的就是重复检出):Shorts、条件 RS 组、Morning Gap,以及排名快照(ETF、RS Top 10)。

有两个裁剪器会缩减美股 master,让走弱的票日后可以重新入选;两者都会先备份:

- **SMA50 自动裁剪**——每次 `us-eod` 一开始:收盘价连续 2 个已完成交易日低于 SMA50,**且**最新收盘低于前一日收盘(仍在下跌;线下反弹的票保留)→ 移除。软失败:yfinance 整体失败就跳过裁剪;数据缺失/历史太短的票保留。配置:`[sma50_prune]`。
- **RS-line 审计**——手动,见[上文](#rs-line-审计与每日-top-10)。

再次命中事件组的老票另行汇总在 [EOD Repeat](#eod-repeat)。

## 输出文件

```
output/
├── TV/                        # 逗号分隔,用于 TradingView "Import list..."
│   ├── US/<date>_{TheSetup,EarningsGap,HighVolume,GapUp,NewHigh52W,TopGainers,Leaders,RS,Shorts,IPO,Repeat}.txt
│   ├── US/<date>_{MorningGapPre{20,10,5},MorningGap{5..30}}.txt
│   ├── US/<date>_ETF_rs.txt   # ETF 强度排名——给人看的,不是导入列表
│   ├── US/rs_us_<date>.txt    # 每日 RS 最强 Top 10 (美股)
│   └── HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,RS,Shorts,IPO,HKMorningGap{10..60}}.txt
├── Webull/{US,HK}/<date>_*.txt   # 换行分隔的镜像,用于 Webull "Upload as File"
├── hk_rs_<date>.txt           # 每日 RS 最强 Top 10 (港股)
├── rs-audit/                  # rs-line 审计报告 + _drop / _keep_ranked sidecar
├── Reports/                   # HTML 报告 (手动运行): PostMarket/<date>_us.html、PreMarket/<date>_us_premarket.html
├── state/                     # eod_seen_* master、RS / metrics 缓存、Morning Gap 当日 seen、EDGAR 缓存
└── launchd_*.log              # 各计划槽的日志
```

- **只有带日期的文件**——没有 "latest" 副本。结果为空就**不写文件**,重跑也绝不删除之前的文件;0 结果的扫描以日志为准。
- **Ticker 格式**——美股:`NASDAQ:AAPL` / `NYSE:WMT` / `AMEX:GLD`。港股:`HKEX:NNN`,**不带前导零**(`HKEX:700`、`HKEX:9988`)——TradingView 会静默拒绝 `HKEX:0700`。
- **Webull** 必须换行分隔;它的上传功能会静默截断逗号列表。
- **导入**——TradingView:Watchlist → "Import list..." → 选 `output/TV/` 下的文件。Webull:Watchlist → "Upload as File" → 选 `output/Webull/` 下的对应文件。
- **保留期**(自动清理,软失败):TV / Webull 5 天 · rs-audit 5 天 · RS Top 10 快照 4 天 · Reports 7 天 · state 缓存 2–4 天。master(`eod_seen_*`)、`ntfy_last_seen.txt`、`edgar_cache/` 和日志永远不会被清理。

## 同步与通知

### Futu 同步

每次写完自选列表后触发(`[futu]`)。软失败:出错只记 warning,绝不阻塞 `.txt` 输出;空结果绝不清空分组。

1. 启动 [FutuOpenD](https://openapi.futunn.com/futu-api-doc/intro/intro.html) 并登录(默认 `127.0.0.1:11111`)。
2. 在 Futu 客户端里手动建好 18 个自定义分组(API 不能新建分组):
   - 美股:`TheSetup`、`EarningsGap`、`HighVolume`、`GapUp`、`NewHigh52W`、`TopGainers`、`Leaders`、`Shorts`、`RS`、`IPO`
   - 港股:`HKShorts`、`HKEarningsGap`、`HKHighVolume`、`HKGapUp`、`HKLeaders`、`HKRS`、`HKIPO`、`HKMorningGap`

所有分组都是 append-only——满了就在客户端手动清空(Futu 上限:500/组,活跃交易户 2,000)。美股 Morning Gap 的票同步进 `EarningsGap`。

### TradingView 同步

可选,**默认关闭**(`[tv_sync]`)。走 TradingView 的非官方 REST API,用你的 `sessionid` cookie 认证:环境变量 `TV_SESSIONID` / `TV_SESSIONID_SIGN`,或 `~/.config/momentum-scanner/tv_cookie.json`。19 个列表要先手动建好(名字区分大小写、一字不差:18 个 Futu 分组名,外加一个独立的 `MorningGap`);找不到的名字会告警并跳过。软失败约定同 Futu。

### ntfy 推送

配置 `[notify]`,并在 [ntfy](https://ntfy.sh) app 里订阅该 topic;Mac 上另有一个常驻 launchd 订阅器把同一 topic 桥接到 macOS 通知中心。

| 通知                   | 触发时机                                             |
| ---------------------- | ---------------------------------------------------- |
| 新 gapper              | Morning Gap 扫描发现当天同阶段更早扫描里没出现过的票 |
| **PROMOTED**(高优先级) | 盘前 gapper 首次通过盘后累计量闸                     |
| **SKIPPED**(高优先级)  | 计划内的扫描因网络始终没起来而退出                   |
| RS workflow 失败       | launchd 触发云端 RS workflow 失败                    |
| Catalyst Report Ready  | 盘前 catalyst 报告写完(仅当该报告启用时)             |

## LLM 报告

> **每日 LLM 自动生成自 2026-09-17 起已关闭。** `run_eod.sh` 里的报告步骤已注释掉,`[morning_gap_catalyst].enabled = false`。代码保留,可手动运行。

### CANSLIM 报告

`uv run main.py --mode report --market us [--date YYYY-MM-DD]` 读取当天长线侧的 `.txt` 文件(不含 Shorts 与 Morning Gap),按分组优先级最多取 30 只,生成自包含的 `output/Reports/PostMarket/<date>_us.html`。`--market hk` 同样可用。

- **结构化字段:** 优先 SEC EDGAR companyfacts,yfinance 兜底——市值、EPS / 营收及 YoY 轨迹、PE、ROE、机构持仓、财报日、RS 百分位。
- **证据包预取**(`[report.evidence]`):先为每只票抓好 yfinance 新闻 / 分析师共识 / 财报日历和近期 EDGAR 公告,然后**只调一次不带工具的 LLM**;证据不足时才允许联网搜索。
- **语言:** 数据字段用英文,定性分析用简体中文。
- **软失败:** 缺 key 就跳过报告;`.txt` 输出不受影响。

| 后端(`[report] backend`)   | 联网搜索          | 默认模型                                           | Key(`.env`)                                                                   |
| -------------------------- | ----------------- | -------------------------------------------------- | ----------------------------------------------------------------------------- |
| `deepseek`(出厂默认)       | Tavily            | `deepseek-v4-pro`                                  | `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`                                         |
| `anthropic`                | 原生 `web_search` | `claude-sonnet-4-6`                                | `ANTHROPIC_API_KEY`                                                           |
| `kimi` / `glm` / `minimax` | Tavily            | `kimi-k2-turbo-preview` / `glm-4.6` / `MiniMax-M2` | `MOONSHOT_API_KEY` / `ZHIPUAI_API_KEY` / `MINIMAX_API_KEY` + `TAVILY_API_KEY` |

所有后端都走 Anthropic SDK(非 Anthropic 厂商经各自的 Anthropic 兼容端点)。把 `.env.example` 复制为 `.env` 填入 key。

### 盘前 catalyst 报告

启用时(`[morning_gap_catalyst]`),盘前扫描发现新的美股 gapper 就会拉起一个**独立子进程**——绝不阻塞扫描本身——固定使用 DeepSeek + Tavily,且只读该次扫描的 JSON 快照。输出:`output/Reports/PreMarket/<date>_us_premarket.html`,在 −20/−10/−5 三次扫描间持续重渲染,写完后推送 "Catalyst Report Ready" ntfy 通知。

## 自动化

macOS launchd + pmset;时间均为 HKT。plist 放在 `~/Library/LaunchAgents/`(源文件在 `scripts/`)。

| 计划槽           | 触发                                  | 运行                            | Plist(`com.xue.finviz-to-tv.*`) |
| ---------------- | ------------------------------------- | ------------------------------- | ------------------------------- |
| 美股 EOD         | 周二至周六 10:00                      | `scripts/run_eod.sh`            | `plist`                         |
| 港股 EOD         | 周一至周五 20:00                      | `scripts/run_hk_eod.sh`         | `hk-eod.plist`                  |
| 美股 Morning Gap | 每周 90 条(9 个 offset × EDT/EST)     | `--mode morning-gap`            | `morning-gap.plist`             |
| 港股 Morning Gap | 周一至周五 × 6 个 offset(09:40–10:30) | `--mode hk-morning-gap`         | `hk-morning-gap.plist`          |
| 美股 RS 触发     | 周二至周六 08:45                      | `gh workflow run`               | `us-rs-3m-trigger.plist`        |
| 港股 RS 触发     | 周一至周五 18:45                      | `gh workflow run`               | `hk-rs-trigger.plist`           |
| ntfy 订阅器      | 常驻(KeepAlive)                       | ntfy → 通知中心                 | `ntfy-subscriber.plist`         |
| 唤醒重排         | 周日 18:00(root LaunchDaemon)         | `schedule_morning_gap_wakes.py` | `schedule-wakes.plist`          |

- 每个 EOD wrapper 先跑 EOD 扫描(watchdog:美股 30 分钟 / 港股 45 分钟),再以 soft 步骤跑 `--dry-run` rs-line 审计;退出码只反映 EOD 扫描。
- Morning Gap **每次触发都自检时间窗口,窗口外干净退出**,所以多触发或漏唤醒都无害。
- RS 触发器在每次 EOD 前 75 分钟主动触发云端 workflow,因为 GitHub 的定时 cron 可能晚几小时甚至直接不跑;workflow 的 commit 步骤幂等,重复触发无害。
- 10:00 落在美股收盘(EDT 与 EST 都覆盖)和上游 12M RS 提交之后;20:00 距港股收盘留了 4 小时让数据稳定。

```bash
sudo pmset repeat wakeorpoweron TWRFS 09:59:00       # 为美股槽唤醒 (macOS 26+ 关键字是 "wakeorpoweron")
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.plist
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.hk-eod.plist
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.morning-gap.plist
sudo uv run scripts/schedule_morning_gap_wakes.py    # 一次性排 Morning Gap 唤醒 (首次安装时跑;之后 LaunchDaemon 每周重排)
```

## 配置

所有过滤条件、阈值、分组映射和 ETF 名单都在 [`config.toml`](config.toml);API key 放在 `.env`。架构说明与不变量:[`CLAUDE.md`](CLAUDE.md)。

## 依赖

Python ≥ 3.12 — [finviz](https://github.com/mariostoev/finviz)、[yfinance](https://github.com/ranaroussi/yfinance)、[futu-api](https://pypi.org/project/futu-api/)、[curl-cffi](https://pypi.org/project/curl-cffi/)、[openpyxl](https://openpyxl.readthedocs.io/)、[anthropic](https://pypi.org/project/anthropic/)(报告)、[httpx](https://www.python-httpx.org/)(Tavily + TV 同步)、[markdown](https://pypi.org/project/Markdown/)。开发:pytest + pytest-asyncio。

## 参考资料

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
