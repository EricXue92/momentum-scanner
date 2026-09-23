[English](README.md) | **繁體中文** | [简体中文](README.zh-CN.md)

# 每日選股掃描流水線 (Daily Stock Screener Pipeline)

一套定時運行的美股 + 港股動量掃描器。每天按 O'Neil / Kell / Kullamägi 的方法篩出做多與做空候選,給一份固定 ETF 名單做 3 個月相對強度排名,並導出 TradingView、Webull、Futu(富途牛牛)自選列表。

## 目錄

- [概覽](#概覽)
- [快速開始](#快速開始)
- [美股篩選器](#美股篩選器)
  - [共用閘門](#共用閘門) · [各組 RS 閘](#各組-rs-閘) · [Longs](#longs) · [Leaders](#leaders) · [條件 RS 組](#條件-rs-組) · [美股 Shorts](#美股-shorts) · [美股 IPO](#美股-ipo) · [EOD Repeat](#eod-repeat) · [ETF 強度排名](#etf-強度排名) · [美股 Morning Gap](#美股-morning-gap)
- [港股篩選器](#港股篩選器)
  - [港股基線](#港股基線) · [港股長線組](#港股長線組) · [港股 Shorts](#港股-shorts) · [港股 IPO](#港股-ipo) · [港股 Morning Gap](#港股-morning-gap)
- [RS 數據](#rs-數據)
  - [雲端 RS 表](#雲端-rs-表) · [RS-line 標註](#rs-line-標註) · [RS-line 審計與每日 Top 10](#rs-line-審計與每日-top-10)
- [去重與裁剪](#去重與裁剪)
- [輸出文件](#輸出文件)
- [同步與通知](#同步與通知)
  - [Futu 同步](#futu-同步) · [TradingView 同步](#tradingview-同步) · [ntfy 推送](#ntfy-推送)
- [LLM 報告](#llm-報告)
  - [CANSLIM 報告](#canslim-報告) · [盤前 catalyst 報告](#盤前-catalyst-報告)
- [自動化](#自動化)
- [配置](#配置)
- [依賴](#依賴)
- [參考資料](#參考資料)

## 概覽

**每天跑什麼(時間均為 HKT):**

| 任務             | 時間                         | 產出                                                                               |
| ---------------- | ---------------------------- | ---------------------------------------------------------------------------------- |
| 美股 EOD         | 週二至週六 10:00             | Longs(6 組)、Leaders、條件 RS 組、Shorts、IPO、Repeat、**ETF 強度排名**、RS Top 10 |
| 港股 EOD         | 週一至週五 20:00             | EarningsGap / HighVolume / GapUp / Leaders / 條件 RS 組、Shorts、IPO、RS Top 10    |
| 美股 Morning Gap | 09:30 ET 開盤前後共 9 次掃描 | 盤前 gapper(−20/−10/−5 分鐘)與經成交量確認的盤後 gapper(+5…+30 分鐘),ntfy 推送     |
| 港股 Morning Gap | 09:30 HKT 開盤後 6 次掃描    | 盤後 gapper(+10…+60 分鐘),ntfy 推送                                                |

**數據源:**

| 數據源                   | 用途                                                                                                          |
| ------------------------ | ------------------------------------------------------------------------------------------------------------- |
| Finviz                   | 美股選股 discovery(所有掃描都加 `ind_stocksonly`,不含 ETF/ETN)                                                |
| yfinance                 | 日線——dollar volume、ADR%、Rel Vol、SMA、漲幅;港股 k 線 + HSI 歷史;ETF 排名                                   |
| Futu OpenD               | Morning Gap 快照(美股 + 港股)、精確市值、HSI 當日漲跌、自選分組同步                                           |
| GitHub Actions → `data/` | RS 百分位表(美股 3M、港股 12M+3M)與港股 metrics frame——家用 IP 跑 yfinance 會被限流,所以放雲端計算,本地只拉取 |
| Fred6725/rs-log          | 美股 12M IBD 式 RS 百分位                                                                                     |
| HKEX 股票名單            | 港股主板 universe(約 2,400 隻)                                                                                |

結果寫成帶日期的 `.txt` 自選列表(TradingView 用逗號分隔,Webull 用換行分隔),並自動同步到 Futu 分組;TradingView 列表同步為可選項。LLM 研究報告可按需手動生成([每日自動生成已關閉](#llm-報告))。

## 快速開始

```bash
uv sync                                              # 安裝
uv run main.py --mode us-eod                         # 美股 EOD (Longs/Leaders/RS/Shorts/IPO/Repeat + ETF 排名)
uv run main.py --mode hk-eod                         # 港股 EOD (長線組 + Shorts + IPO)
uv run main.py --mode morning-gap                    # 美股缺口掃描 (不在 ET 窗口內會乾淨退出)
uv run main.py --mode hk-morning-gap                 # 港股缺口掃描 (僅盤後)
uv run main.py --mode etf-rs                         # 單獨重跑 ETF 強度排名
uv run main.py --mode rs-line-audit --market both    # 按 RS-line 趨勢給 master 打分;裁剪前會詢問
uv run main.py --mode report --market us             # 為當天美股個股生成 CANSLIM 報告 (手動;需 API key)
uv run pytest tests/ -v                              # 測試
```

`--mode eod` 仍可同時跑美股 + 港股,但定時任務用分市場的模式——10:00 HKT 時港股日線尚未走完。

## 美股篩選器

### 共用閘門

Finviz 選股之後,基於 yfinance 日線套用。閾值在 `[settings]`。

| 閘門          | 閾值                                                            | 適用範圍                                     |
| ------------- | --------------------------------------------------------------- | -------------------------------------------- |
| Dollar Volume | 價 × 20 日均量 ≥ $100M(Shorts 為 ≥ $50M)                       | Longs、Leaders、RS、Shorts、IPO、Morning Gap |
| ADR%          | 最近 20 根完整 bar 的 mean(`(High − Low) / Close`) × 100 ≥ 4.0% | 同上                                         |

ADR%(Kullamägi 式)衡量一隻股票**當下**的波動幅度;它取代了過去的 Finviz `beta > 1.5` 過濾——後者容易誤殺正活躍的中大盤票。

### 各組 RS 閘

口徑:**事件組看長期強度(12M ≥ 90),其餘看近期強度(3M ≥ 90)。** 每組一個獨立旋鈕,設 `0` 即關閉該層。

| 分組                                                              | 12M 閘                             | 3M 閘                           |
| ----------------------------------------------------------------- | ---------------------------------- | ------------------------------- |
| Longs: EarningsGap / HighVolume / GapUp / NewHigh52W / TopGainers | `min_rs_percentile_longs` = **90** | —                               |
| Longs: TheSetup                                                   | 關(組內 `min_rs_percentile = 0`)   | —                               |
| Leaders                                                           | `min_rs_percentile` = 0(關)        | `min_rs_percentile_3m` = **90** |
| 條件 RS 組                                                        | `min_rs_percentile_rs` = 0(關)     | **90**                          |
| 美股 Shorts                                                       | `min_rs_percentile_shorts` = 0(關) | **90**                          |
| 美股 IPO(歷史 ≥ 64 天)                                            | —                                  | **90**                          |
| Morning Gap、ETF 排名                                             | —                                  | —                               |

- RS 表裡**查不到**的 ticker 保留,不丟棄。
- RS 表拉取失敗時先回退最多 3 天的緩存,再不行就不過閘直接放行並告警——絕不硬失敗。
- `min_rs_percentile_rs` / `_shorts` **不配置**時會繼承 Longs 的值;想保持關閉必須顯式寫 `0`。

### Longs

Oliver Kell 的動量/突破 setup。6 組互斥——靠前的組優先,每隻 ticker 每天最多進一個 Longs 文件。共同條件:Small Cap+、Avg Vol > 500K、站上 SMA50 與 SMA200,再加共用閘門。

| 優先級 | 分組          | 額外過濾                                                                                          |
| ------ | ------------- | ------------------------------------------------------------------------------------------------- |
| 0      | `TheSetup`    | Price > $10、Gap Up 5%+、Rel Vol ≥ 3× 20 日均量(yfinance)。**不過 RS 閘**——放量大缺口本身就是信號 |
| 1      | `EarningsGap` | Price > $20、Earnings Today、Rel Vol > 1.5、Gap Up 5%+                                            |
| 2      | `HighVolume`  | Price > $20、Day Up、Rel Vol ≥ 3× 20 日均量(yfinance)                                             |
| 3      | `GapUp`       | Price > $20、Gap Up 3%+                                                                           |
| 4      | `NewHigh52W`  | Price > $20、52 週新高                                                                            |
| 5      | `TopGainers`  | Price > $20、Finviz 信號 Top Gainers                                                              |

### Leaders

長期趨勢領頭羊:Small Cap+、Avg Vol > 500K、Price > $20、站上 SMA50 與 SMA200、共用閘門、RS 3M ≥ 90。5 個漲幅窗口合併寫入同一個 `Leaders.txt`:

| 4 週   | 13 週  | 26 週   | YTD     | 52 週   |
| ------ | ------ | ------- | ------- | ------- |
| ≥ +30% | ≥ +50% | ≥ +100% | ≥ +100% | ≥ +150% |

### 條件 RS 組

弱市裡扛得住的股票。**僅當 SPY 與 QQQ 當日都跌 ≥ 1.0% 時才運行。** 過濾:Small Cap+、Avg Vol > 500K、Price > $20、Day Up、站上 SMA50 與 SMA200、共用閘門、RS 3M ≥ 90。

### 美股 Shorts

Kullamägi 的拋物線衝頂做空 setup。每天重新檢出(不參與任何去重)。

1. **Finviz:** 價格高於 SMA20 20%+、站上 SMA50、Avg Vol > 1M、價格 > $5、市值 > $50M → 再用 **RS 3M ≥ 90** 縮小名單。
2. **yfinance + Futu 市值**,依次過:

| 過濾                | 閾值                                                                               |
| ------------------- | ---------------------------------------------------------------------------------- |
| 漲幅                | 2、3 或 4 週內上漲:**50%+**(市值 ≥ $10B)/ **200%+**($2B–$10B)/ **300%+**($50M–$2B) |
| Dollar Volume、ADR% | ≥ $50M(`[shorts].min_dollar_volume`,比全局 $100M 寬鬆)、≥ 4.0%                   |
| 連續上漲天數        | ≥ 3 天(不含當天未走完的 bar)                                                       |

市值取自 Futu 快照(精確值;Finviz 的 `"1.23B"` 這類字符串在分級邊界附近容易分錯檔),取不到再回落 Finviz。

### 美股 IPO

自動收集的 sidecar:已通過 Longs/Leaders/RS 的 Finviz 篩選、卻因歷史太短被 yfinance 丟掉的候選。閘門隨歷史長度逐級生效,所以上市 30 天的新股也能浮出,而上市 200 天的則要過幾乎完整的基線。港股 IPO 的 ladder 完全相同,只是閾值不同:

| 閘門                   | 美股             | 港股               | 生效條件 |
| ---------------------- | ---------------- | ------------------ | -------- |
| 歷史長度               | ≥ 20 個交易日    | ≥ 20 個交易日      | 始終     |
| 市值                   | ≥ $300M          | ≥ HK$300M          | 始終     |
| 價格                   | ≥ $20            | ≥ HK$20            | 始終     |
| 日均量 / Dollar Volume | ≥ 500K / ≥ $100M | ≥ 500K / ≥ HK$100M | ≥ 20 天  |
| ADR%                   | ≥ 4.0%           | ≥ 3.0%             | ≥ 20 天  |
| 站上 SMA50             | ✓                | ✓                  | ≥ 50 天  |
| RS 3M                  | ≥ 90(對 SPY)     | ≥ 90(對 HSI)       | ≥ 64 天  |
| 站上 SMA200            | ✓                | ✓                  | ≥ 200 天 |

- 美股新股不在 RS universe 裡,所以 3M 分數在本地計算,再排進雲端表的 `raw_score` 分佈裡取百分位。
- 出現在 12M RS 表裡的 ticker 必然有 ≥ 12 個月曆史,不可能是新股——會被移出 IPO 桶(只是 yfinance 臨時缺數據)。
- 獨立 master `eod_seen_IPO.txt`,所以新股"畢業"後仍能進入它應屬的分組。輸出:`<date>_IPO.txt`,Futu 分組 `IPO`。

### EOD Repeat

已在跨日 master 裡、**今天又命中事件組**(TheSetup / EarningsGap / HighVolume / GapUp / TopGainers)的老票,彙總到 `<date>_Repeat.txt` 供複查。NewHigh52W 與 Leaders 不算——它們是持續狀態,會天天重複命中。對 master 只讀;各組自己的文件仍保持"只出新票"的含義。Futu/TV 映射默認註釋掉(要先手建 `Repeat` 分組)。配置:`[eod_repeat]`。

### ETF 強度排名

每天一份行業 / 主題輪動視圖:一份約 65 隻的固定 ETF 名單(美股行業與主題、商品、槓桿產品、全球市場),按 3 個月相對強度排名。

- **打分:** 與個股相同的 3M RS 公式(`0.5·R21 + 0.3·R42 + 0.2·R63`,相對 SPY)。百分位是**在 ETF 名單內部**排的,不是對個股 universe。
- **何時跑:** 每次 `us-eod` 末尾的 soft 步驟(失敗不影響 EOD);也可用 `--mode etf-rs` 單獨重跑。本地計算——一次 yfinance 批量下載,不走雲端。
- **輸出:** `output/TV/US/<date>_ETF_rs.txt`,每行一隻 ETF,**最強的在最上面**,格式 `TICKER - 中文名 | 前五大持倉`:

  ```
  NRGU - 油气 3 倍做多 | VLO、MPC、PSX、CVX、DVN (ETN 挂钩指数前五成分股)
  ARKG - 基因组革命 | TXG、TWST、TEM、CRSP、PSNL
  USO - 原油 | 不适用: 原油期货及现金 / 国债抵押品
  ```

- **給人看的,不是 TradingView 導入文件**——它是 `TV/US/` 裡唯一非逗號分隔的 `.txt`。帶分數的完整表(排名 / 分數 / 百分位)在 EOD 日誌裡。
- **維護:** 增刪改 `[etf_rs.tickers]`(`TICKER = "中文名"`)即可調整名單。持倉來自靜態、手工維護的 `[etf_rs.holdings]` 表(不走 API 刷新;沒配的票不顯示持倉段)。
- **同名即同一品種:** 中文名完全相同的 ticker 只顯示最強的一隻(曾經的 GDXU / NUGT 即是一例;當前表裡已無同名對,規則保留);被隱藏的會列在日誌裡。
- 僅為排名快照:同日重跑直接覆蓋;不去重、不鏡像 Webull、不同步 Futu/TV。

### 美股 Morning Gap

每天 9 次掃描:**盤前** −20/−10/−5 分鐘 → `MorningGapPre{20,10,5}.txt`;**盤後** +5…+30 分鐘 → `MorningGap{5..30}.txt`。每次掃描各寫各的快照;沒掃到就不寫文件。不過 RS 閘。

**Phase 1 — Futu 快照 discovery**(NASDAQ / NYSE / AMEX 普通股):

| 過濾       | 閾值                                          |
| ---------- | --------------------------------------------- |
| 市值、價格 | ≥ $300M、≥ $20                                |
| Gap(盤前)  | `pre_change_rate` ≥ 5%,且盤前有成交           |
| Gap(盤後)  | `(last_price − prev_close) / prev_close` ≥ 5% |

**Phase 2 — yfinance + Futu 成交量:**

| 過濾                | 閾值                                                                 | 盤前 | 盤後 |
| ------------------- | -------------------------------------------------------------------- | ---- | ---- |
| 20 日均量           | ≥ 500K 股/天                                                         | ✓    | ✓    |
| 盤前成交量          | Futu `pre_volume` ≥ 5% × 20 日均量——剔除只靠幾百股撐出來的薄盤假 gap | ✓    | —    |
| Dollar Volume       | ≥ $100M                                                              | ✓    | ✓    |
| ADR%                | ≥ 4.0%;gap ≥ 10% 時放寬到 3.0%                                       | ✓    | ✓    |
| SMA50 / SMA200 趨勢 | **實時價**站上兩條均線;gap ≥ 10% 只豁免 SMA50——SMA200 永不豁免       | ✓    | ✓    |
| 盤中累計量          | 09:30 ET 起的累計成交量 ≥ 20 日均量(機構買入的特徵)                  | —    | ✓    |

均線本身始終用已完成的日線計算;只有比較基準用實時價。所有旋鈕都在 `[morning_gap]`(`min_pre_volume_ratio`、`sma_use_live_price`、`sma_bypass_gap_percent`、`adr_bypass_*`),隻影響 Morning Gap。需要 FutuOpenD 在線並有美股 Lv1 行情——沒有 Finviz 兜底。

## 港股篩選器

方法與美股一致,閾值用 HKD,universe = HKEX 主板。輸出為 TradingView 的 `HKEX:NNN` 格式,去掉前導零。

### 港股基線

5 個長線組共用(`[hk_settings]`):

| 閘門                 | 閾值                                             | 說明                     |
| -------------------- | ------------------------------------------------ | ------------------------ |
| 市值                 | ≥ HK$300M                                        | 取自 Futu 快照           |
| 日均量               | ≥ 500K 股/天(20 日)                              |                          |
| Dollar Volume        | ≥ HK$100M(20 日)                                 |                          |
| ADR%                 | ≥ 3.0%                                           | 港股藍籌波動率結構性偏低 |
| 價格                 | ≥ HK$20                                          |                          |
| 站上 SMA50 與 SMA200 | 兩條都要                                         |                          |
| RS(對 HSI)           | 事件組 **12M ≥ 90**; Leaders / RS 組 **3M ≥ 90** | 與美股的分工一致         |

### 港股長線組

按優先級排序;每隻 ticker 每天最多進一個文件。

| 優先級 | 分組           | 額外閘門                                                            |
| ------ | -------------- | ------------------------------------------------------------------- |
| 1      | HK EarningsGap | gap ≥ 3% + Rel Vol ≥ 3(用形態代替——港股沒有財報日曆)                |
| 2      | HK HighVolume  | Rel Vol ≥ 3                                                         |
| 3      | HK GapUp       | gap ≥ 3%                                                            |
| 4      | HK Leaders     | 任一:4 週 +30% / 13 週 +50% / 26 週 +100% / YTD +100% / 52 週 +150% |
| 5      | HK RS          | 當日收紅;**僅當 HSI 當日跌 ≥ 1.0% 時才運行**(`hsi_rs_trigger`)      |

- **數據:** metrics frame 與 RS 表從雲端拉取(`data/hk_metrics/`、`data/hk_rs/`);雲端取不到時回退到本地 yfinance 抓取。
- **數據日規則:** 只有 20:00 這一檔用當天收盤;更早的運行會裁掉當天未走完的 bar,並跳過 HSI 觸發的 RS 組。週末補跑映射到上週五。
- **OpenD:** 市值和 HSI 觸發來自 Futu。OpenD 不在線時流程照常跑完,但市值閘會把所有票篩掉。

### 港股 Shorts

與美股 Shorts 相同,換成 HKD:RS 3M ≥ 90(對 HSI,先篩 universe)、市值 ≥ HK$50M、日均量 ≥ 1M 股/天、dollar volume ≥ HK$50M、ADR% ≥ 4.0%、漲幅按市值分級 50% / 200% / 300%(≥ HK$10B / HK$2B–10B / HK$50M–2B)、連續上漲 ≥ 3 天。配置:`[hk_shorts]`。

### 港股 IPO

日線收盤數不足 253 根的主板 ticker(不夠算 12M RS)。與美股相同的按歷史分級 ladder——見[美股 IPO](#美股-ipo)的表。歷史 ≥ 64 天卻不在雲端 3M 表裡的票會被丟棄。獨立 master `eod_seen_HKIPO.txt`;輸出 `<date>_IPO.txt`,Futu 分組 `HKIPO`。

### 港股 Morning Gap

僅盤後(Futu 沒有港股盤前數據):09:30 HKT 開盤後 +10…+60 分鐘共 6 次掃描 → `HKMorningGap{10..60}.txt`。Gap ≥ 5%,基線同 `[hk_settings]`(ADR% ≥ 3.0%),實時價趨勢閘與 SMA50 豁免同美股;累計量閘用 Futu 快照的當日成交量。配置:`[hk_morning_gap]`。

## RS 數據

### 雲端 RS 表

| 表       | 公式                                               | 基準 | 發佈位置                                                                            |
| -------- | -------------------------------------------------- | ---- | ----------------------------------------------------------------------------------- |
| 美股 12M | `0.4·P3 + 0.2·P6 + 0.2·P9 + 0.2·P12`               | SPY  | [Fred6725/rs-log](https://github.com/Fred6725/relative-strength)(工作日 ~01:30 UTC) |
| 美股 3M  | `0.5·R21 + 0.3·R42 + 0.2·R63`,universe 約 6,100 隻 | SPY  | `data/us_rs_3m/<date>.csv`(GitHub Actions)                                          |
| 港股     | 兩條公式都算,在港股主板內部排名                    | HSI  | `data/hk_rs/<date>.csv`(GitHub Actions)                                             |

本地流水線只拉取這些 CSV(緩存在 `output/state/`,拉不到就回退最多 3 天)。GitHub 自帶的 cron 不可靠,所以由 launchd 在每次 EOD 前 75 分鐘主動觸發 workflow——見[自動化](#自動化)。

### RS-line 標註

雲端 CSV 還帶 `rs_below_ma` / `rs_days_below_ma` / `rs_frac_below_ma` 三列:TraderLion 式 **RS line**(價 ÷ 基準)與它自己的 EMA21 的比較。EOD 日誌會標註 RS line 持續位於均線下方的長線側入選票。**僅寫日誌**——不影響 `.txt` 輸出和去重。配置:`[rs_line]`。

### RS-line 審計與每日 Top 10

`uv run main.py --mode rs-line-audit [--market us|hk|both] [--dry-run|--yes]` 按 RS-line 趨勢給跨日 master 裡的每隻票打分,把報告和 `_drop` / `_keep_ranked` sidecar 寫到 `output/rs-audit/`。

- **裁剪是手動的:** 命令會先詢問 y/N,確認後才把要裁的票從 `eod_seen_{US,HK}.txt` 移除(先備份為 `.bak.<stamp>`),被裁的票日後可重新入選。`--yes` 跳過詢問;`--dry-run` 絕不動 master。
- **每日 Top 10:** 每個 EOD wrapper 都以 soft 步驟追加跑一次 `--dry-run` 審計,把最強的 `[rs_line].top_n`(10)隻票寫到 `output/TV/US/rs_us_<date>.txt`(可直接導入 TradingView)和 `output/hk_rs_<date>.txt`。為空則不寫;同日重跑直接覆蓋。

## 去重與裁剪

1. **Longs 內部**——靠前的組優先(`TheSetup > EarningsGap > HighVolume > GapUp > NewHigh52W > TopGainers`)。
2. **跨組**——Longs 優先於 Leaders(港股:當日內 `EarningsGap > HighVolume > GapUp > Leaders > RS`)。
3. **跨日 master**——`output/state/eod_seen_{US,HK,IPO,HKIPO}.txt`。每隻票只在首次出現時輸出一次,之後的運行只出**新**票。各市場互相獨立。刪除該文件即重置。

**從不與 master 比對**(要的就是重複檢出):Shorts、條件 RS 組、Morning Gap,以及排名快照(ETF、RS Top 10)。

有兩個裁剪器會縮減美股 master,讓走弱的票日後可以重新入選;兩者都會先備份:

- **SMA50 自動裁剪**——每次 `us-eod` 一開始:收盤價連續 2 個已完成交易日低於 SMA50,**且**最新收盤低於前一日收盤(仍在下跌;線下反彈的票保留)→ 移除。軟失敗:yfinance 整體失敗就跳過裁剪;數據缺失/歷史太短的票保留。配置:`[sma50_prune]`。
- **RS-line 審計**——手動,見[上文](#rs-line-審計與每日-top-10)。

再次命中事件組的老票另行彙總在 [EOD Repeat](#eod-repeat)。

## 輸出文件

```
output/
├── TV/                        # 逗號分隔,用於 TradingView "Import list..."
│   ├── US/<date>_{TheSetup,EarningsGap,HighVolume,GapUp,NewHigh52W,TopGainers,Leaders,RS,Shorts,IPO,Repeat}.txt
│   ├── US/<date>_{MorningGapPre{20,10,5},MorningGap{5..30}}.txt
│   ├── US/<date>_ETF_rs.txt   # ETF 強度排名——給人看的,不是導入列表
│   ├── US/rs_us_<date>.txt    # 每日 RS 最強 Top 10 (美股)
│   └── HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,RS,Shorts,IPO,HKMorningGap{10..60}}.txt
├── Webull/{US,HK}/<date>_*.txt   # 換行分隔的鏡像,用於 Webull "Upload as File"
├── hk_rs_<date>.txt           # 每日 RS 最強 Top 10 (港股)
├── rs-audit/                  # rs-line 審計報告 + _drop / _keep_ranked sidecar
├── Reports/                   # HTML 報告 (手動運行): PostMarket/<date>_us.html、PreMarket/<date>_us_premarket.html
├── state/                     # eod_seen_* master、RS / metrics 緩存、Morning Gap 當日 seen、EDGAR 緩存
└── launchd_*.log              # 各計劃槽的日誌
```

- **只有帶日期的文件**——沒有 "latest" 副本。結果為空就**不寫文件**,重跑也絕不刪除之前的文件;0 結果的掃描以日誌為準。
- **Ticker 格式**——美股:`NASDAQ:AAPL` / `NYSE:WMT` / `AMEX:GLD`。港股:`HKEX:NNN`,**不帶前導零**(`HKEX:700`、`HKEX:9988`)——TradingView 會靜默拒絕 `HKEX:0700`。
- **Webull** 必須換行分隔;它的上傳功能會靜默截斷逗號列表。
- **導入**——TradingView:Watchlist → "Import list..." → 選 `output/TV/` 下的文件。Webull:Watchlist → "Upload as File" → 選 `output/Webull/` 下的對應文件。
- **保留期**(自動清理,軟失敗):TV / Webull 5 天 · rs-audit 5 天 · RS Top 10 快照 4 天 · Reports 7 天 · state 緩存 2–4 天。master(`eod_seen_*`)、`ntfy_last_seen.txt`、`edgar_cache/` 和日誌永遠不會被清理。

## 同步與通知

### Futu 同步

每次寫完自選列表後觸發(`[futu]`)。軟失敗:出錯只記 warning,絕不阻塞 `.txt` 輸出;空結果絕不清空分組。

1. 啟動 [FutuOpenD](https://openapi.futunn.com/futu-api-doc/intro/intro.html) 並登錄(默認 `127.0.0.1:11111`)。
2. 在 Futu 客戶端裡手動建好 18 個自定義分組(API 不能新建分組):
   - 美股:`TheSetup`、`EarningsGap`、`HighVolume`、`GapUp`、`NewHigh52W`、`TopGainers`、`Leaders`、`Shorts`、`RS`、`IPO`
   - 港股:`HKShorts`、`HKEarningsGap`、`HKHighVolume`、`HKGapUp`、`HKLeaders`、`HKRS`、`HKIPO`、`HKMorningGap`

所有分組都是 append-only——滿了就在客戶端手動清空(Futu 上限:500/組,活躍交易戶 2,000)。美股 Morning Gap 的票同步進 `EarningsGap`。

### TradingView 同步

可選,**默認關閉**(`[tv_sync]`)。走 TradingView 的非官方 REST API,用你的 `sessionid` cookie 認證:環境變量 `TV_SESSIONID` / `TV_SESSIONID_SIGN`,或 `~/.config/momentum-scanner/tv_cookie.json`。19 個列表要先手動建好(名字區分大小寫、一字不差:18 個 Futu 分組名,外加一個獨立的 `MorningGap`);找不到的名字會告警並跳過。軟失敗約定同 Futu。

### ntfy 推送

配置 `[notify]`,並在 [ntfy](https://ntfy.sh) app 裡訂閱該 topic;Mac 上另有一個常駐 launchd 訂閱器把同一 topic 橋接到 macOS 通知中心。

| 通知                   | 觸發時機                                             |
| ---------------------- | ---------------------------------------------------- |
| 新 gapper              | Morning Gap 掃描發現當天同階段更早掃描裡沒出現過的票 |
| **PROMOTED**(高優先級) | 盤前 gapper 首次通過盤後累計量閘                     |
| **SKIPPED**(高優先級)  | 計劃內的掃描因網絡始終沒起來而退出                   |
| RS workflow 失敗       | launchd 觸發雲端 RS workflow 失敗                    |
| Catalyst Report Ready  | 盤前 catalyst 報告寫完(僅當該報告啟用時)             |

## LLM 報告

> **每日 LLM 自動生成自 2026-09-17 起已關閉。** `run_eod.sh` 裡的報告步驟已註釋掉,`[morning_gap_catalyst].enabled = false`。代碼保留,可手動運行。

### CANSLIM 報告

`uv run main.py --mode report --market us [--date YYYY-MM-DD]` 讀取當天長線側的 `.txt` 文件(不含 Shorts 與 Morning Gap),按分組優先級最多取 30 隻,生成自包含的 `output/Reports/PostMarket/<date>_us.html`。`--market hk` 同樣可用。

- **結構化字段:** 優先 SEC EDGAR companyfacts,yfinance 兜底——市值、EPS / 營收及 YoY 軌跡、PE、ROE、機構持倉、財報日、RS 百分位。
- **證據包預取**(`[report.evidence]`):先為每隻票抓好 yfinance 新聞 / 分析師共識 / 財報日曆和近期 EDGAR 公告,然後**只調一次不帶工具的 LLM**;證據不足時才允許聯網搜索。
- **語言:** 數據字段用英文,定性分析用簡體中文。
- **軟失敗:** 缺 key 就跳過報告;`.txt` 輸出不受影響。

| 後端(`[report] backend`)   | 聯網搜索          | 默認模型                                           | Key(`.env`)                                                                   |
| -------------------------- | ----------------- | -------------------------------------------------- | ----------------------------------------------------------------------------- |
| `deepseek`(出廠默認)       | Tavily            | `deepseek-v4-pro`                                  | `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`                                         |
| `anthropic`                | 原生 `web_search` | `claude-sonnet-4-6`                                | `ANTHROPIC_API_KEY`                                                           |
| `kimi` / `glm` / `minimax` | Tavily            | `kimi-k2-turbo-preview` / `glm-4.6` / `MiniMax-M2` | `MOONSHOT_API_KEY` / `ZHIPUAI_API_KEY` / `MINIMAX_API_KEY` + `TAVILY_API_KEY` |

所有後端都走 Anthropic SDK(非 Anthropic 廠商經各自的 Anthropic 兼容端點)。把 `.env.example` 複製為 `.env` 填入 key。

### 盤前 catalyst 報告

啟用時(`[morning_gap_catalyst]`),盤前掃描發現新的美股 gapper 就會拉起一個**獨立子進程**——絕不阻塞掃描本身——固定使用 DeepSeek + Tavily,且只讀該次掃描的 JSON 快照。輸出:`output/Reports/PreMarket/<date>_us_premarket.html`,在 −20/−10/−5 三次掃描間持續重渲染,寫完後推送 "Catalyst Report Ready" ntfy 通知。

## 自動化

macOS launchd + pmset;時間均為 HKT。plist 放在 `~/Library/LaunchAgents/`(源文件在 `scripts/`)。

| 計劃槽           | 觸發                                  | 運行                            | Plist(`com.xue.finviz-to-tv.*`) |
| ---------------- | ------------------------------------- | ------------------------------- | ------------------------------- |
| 美股 EOD         | 週二至週六 10:00                      | `scripts/run_eod.sh`            | `plist`                         |
| 港股 EOD         | 週一至週五 20:00                      | `scripts/run_hk_eod.sh`         | `hk-eod.plist`                  |
| 美股 Morning Gap | 每週 90 條(9 個 offset × EDT/EST)     | `--mode morning-gap`            | `morning-gap.plist`             |
| 港股 Morning Gap | 週一至週五 × 6 個 offset(09:40–10:30) | `--mode hk-morning-gap`         | `hk-morning-gap.plist`          |
| 美股 RS 觸發     | 週二至週六 08:45                      | `gh workflow run`               | `us-rs-3m-trigger.plist`        |
| 港股 RS 觸發     | 週一至週五 18:45                      | `gh workflow run`               | `hk-rs-trigger.plist`           |
| ntfy 訂閱器      | 常駐(KeepAlive)                       | ntfy → 通知中心                 | `ntfy-subscriber.plist`         |
| 喚醒重排         | 週日 18:00(root LaunchDaemon)         | `schedule_morning_gap_wakes.py` | `schedule-wakes.plist`          |

- 每個 EOD wrapper 先跑 EOD 掃描(watchdog:美股 30 分鐘 / 港股 45 分鐘),再以 soft 步驟跑 `--dry-run` rs-line 審計;退出碼只反映 EOD 掃描。
- Morning Gap **每次觸發都自檢時間窗口,窗口外乾淨退出**,所以多觸發或漏喚醒都無害。
- RS 觸發器在每次 EOD 前 75 分鐘主動觸發雲端 workflow,因為 GitHub 的定時 cron 可能晚幾小時甚至直接不跑;workflow 的 commit 步驟冪等,重複觸發無害。
- 10:00 落在美股收盤(EDT 與 EST 都覆蓋)和上游 12M RS 提交之後;20:00 距港股收盤留了 4 小時讓數據穩定。

```bash
sudo pmset repeat wakeorpoweron TWRFS 09:59:00       # 為美股槽喚醒 (macOS 26+ 關鍵字是 "wakeorpoweron")
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.plist
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.hk-eod.plist
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.morning-gap.plist
sudo uv run scripts/schedule_morning_gap_wakes.py    # 一次性排 Morning Gap 喚醒 (首次安裝時跑;之後 LaunchDaemon 每週重排)
```

## 配置

所有過濾條件、閾值、分組映射和 ETF 名單都在 [`config.toml`](config.toml);API key 放在 `.env`。架構說明與不變量:[`CLAUDE.md`](CLAUDE.md)。

## 依賴

Python ≥ 3.12 — [finviz](https://github.com/mariostoev/finviz)、[yfinance](https://github.com/ranaroussi/yfinance)、[futu-api](https://pypi.org/project/futu-api/)、[curl-cffi](https://pypi.org/project/curl-cffi/)、[openpyxl](https://openpyxl.readthedocs.io/)、[anthropic](https://pypi.org/project/anthropic/)(報告)、[httpx](https://www.python-httpx.org/)(Tavily + TV 同步)、[markdown](https://pypi.org/project/Markdown/)。開發:pytest + pytest-asyncio。

## 參考資料

這裡只列出了一些關鍵資源,並不完整——還有不少參考過的材料沒有列出。

**書籍:**

- _How to Make Money in Stocks_ — William O'Neil(CANSLIM 與 IBD RS 體系的源頭)
- _Victory in Stock Trading: Strategy and Tactics of the 2020 U.S. Investing Champion_ — Oliver Kell
- _Trade Like a Stock Market Wizard_ — Mark Minervini
- _Think & Trade Like a Champion_ — Mark Minervini
- _A Complete Guide to Volume Price Analysis_ — Anna Coulling
- _The Power of Japanese Candlestick Charts_ — Fred K.H. Tam
- _The Trader's Handbook: Winning Habits and Routines of Successful Traders_ — Richard Moglen, Nick Schmidt, et al.
- _Market Wizards: The Next Generation: The World's Top Young Traders Reveal How They Beat the Market_ — Jack D. Schwager, George F. Coyle

**網站與頻道:**

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
