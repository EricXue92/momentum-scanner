[English](README.md) | **繁體中文** | [简体中文](README.zh-CN.md)

# 每日選股掃描流水線 (Daily Stock Screener Pipeline)

**一句話:每天定時用 Finviz、yfinance、Futu 快照和 GitHub Actions 雲端 RS 表,按 O'Neil / Kell / Kullamägi 的動量體系自動掃描美股與港股的做多/做空候選,輸出 TradingView / Webull / Futu 自選列表,並調用 LLM 生成 CANSLIM 簡報。**

一套多數據源的動量(momentum)與做空(short)選股掃描器:美股用 Finviz 選股,盤中缺口取自 Futu 快照。結果導出為可直接導入 TradingView / Webull 的自選列表,並通過 OpenAPI 自動同步到 Futu(富途牛牛)的自定義分組;也可選同步到 TradingView 列表(走其非官方 REST API)。此外每天還調用 LLM API(如 Claude、DeepSeek 等)生成一份 CANSLIM 風格的研究簡報。選股方法主要參考 William O'Neil、Oliver Kell 與 Kristjan Kullamägi。

> **狀態(2026-08-02):** 美股、港股均已上線。美股數據來自 Finviz 與 yfinance,外加一張 12M IBD RS CSV 和一張 3M RS 表;港股用 yfinance 取 k 線與 HSI 歷史(最早的 Futu-only 方案已棄用——Futu 免費/Lv1 檔只能覆蓋主板約 12% 的 12 個月歷史)。如今 Futu 在港股側只負責市值和條件 RS 觸發所需的 HSI 實時日漲幅快照,在美股側負責盤中缺口 discovery 與 Shorts 市值快照,外加兩個市場的自選分組同步。
>
> **百分位 RS 表(美股 3M、港股 12M+3M)和港股長線側 metrics frame 每天在 GitHub Actions 上算好,以 CSV 發佈到 `data/`;本地流水線只負責拉取**——因為家用 IP 上的 yfinance 計算跑到一半就會被限流。RS 閘兩市結構對稱:**事件組(美股 Longs 5 組、港股 EarningsGap/HighVolume/GapUp)走 12M ≥ 90 單閘,其餘長線側(兩市 Leaders / 條件 RS 組、美股 Shorts)走 3M ≥ 90 單閘**;每組一個獨立旋鈕,雙閘可按組隨時加回。歷史不足 12 個月的新股則走**按歷史深度分級的 IPO ladder**。
>
> 港股流水線有自己獨立的 20:00 HKT 計劃槽,美股則跑在 10:00 HKT,兩者各寫各自的分市場日誌。美股 EOD 跑完後,wrapper 腳本會再跑一次 `--mode report --market us`,為當天新發現的長線側個股生成 CANSLIM 簡報(獨立 HTML);港股不再排程生成報告。報告後端可選:默認 DeepSeek V4 + Tavily,備選 Anthropic `web_search`;也支持 Kimi / GLM / MiniMax(+ Tavily),走各自的 Anthropic 兼容端點。

## 篩選器 (Screeners)

所有基於 Finviz 的掃描都加 `ind_stocksonly` 排除 ETF/ETN;Morning Gap 用 Futu `stock_type=STOCK`,天然只含個股。

### 全局閘門 (long-side)

在 Finviz 選股之後、任何昂貴的 yfinance 計算之前先行套用。閾值均可在 `[settings]` 中配置。

| 閘門                                            | 作用範圍                                | 閾值                                                           | 數據源                                                                                                                                                                                                                                                 |
| ----------------------------------------------- | --------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **IBD RS Percentile 12M (事件組)**              | Longs 5 組                              | ≥ 90(top 10%;表中缺失的 ticker 保留)                           | [Fred6725/rs-log](https://github.com/Fred6725/relative-strength),`RS = 0.4·P3 + 0.2·P6 + 0.2·P9 + 0.2·P12` 對 SPY 歸一,工作日 ~01:30 UTC 刷新                                                                                                          |
| **IBD RS Percentile 3M (Leaders/RS/US Shorts)** | Leaders + RS 組 + US Shorts(不含 Longs) | ≥ 90(top 10%;缺失 ticker 保留)                                 | `RS_3M = 0.5·R21 + 0.3·R42 + 0.2·R63` 對 SPY,universe = Fred6725 ticker 列表(~6100),**雲端在 GitHub Actions 上計算**並發佈到 `data/us_rs_3m/<date>.csv`(帶 `raw_score` 供 IPO 的 out-of-universe 查排名);`us_rs_3m.py` 負責拉取(拉不到就往回退 ≤ 3 天) |
| **Dollar Volume**                               | Longs + Leaders                         | 價 × 20 日均量 ≥ $100M                                         | yfinance 日線                                                                                                                                                                                                                                          |
| **ADR%**                                        | Longs + Leaders                         | mean(`(High − Low) / Close`) × 100,取最近 20 根完整 bar ≥ 4.0% | yfinance 日線                                                                                                                                                                                                                                          |

**RS 作用範圍(每組一個獨立旋鈕,當前生效值):**

| 分組                                                            | 12M 閘                                               | 3M 閘                                                                |
| --------------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------- |
| Longs 5 組 (EarningsGap/HighVolume/GapUp/NewHigh52W/TopGainers) | `min_rs_percentile_longs` = **90**                   | —(設計上無此層)                                                      |
| Leaders                                                         | `min_rs_percentile` = 0(關)                          | `min_rs_percentile_3m` = **90**                                      |
| 條件 RS 組                                                      | `min_rs_percentile_rs` = 0(關;缺省繼承 longs 鍵)     | `min_rs_percentile_3m` = **90**                                      |
| US Shorts                                                       | `min_rs_percentile_shorts` = 0(關;缺省繼承 longs 鍵) | `min_rs_percentile_3m` = **90**                                      |
| US IPO ladder (≥ 64 天)                                         | —                                                    | `min_rs_percentile_3m`(用 `np.searchsorted` 對 Fred6725 `raw_score`) |

當前口徑:**事件組看長期強度(12M ≥ 90),其餘長線側看近期強度(3M ≥ 90)**。12M ≥ 90 表示"長期領頭羊",3M ≥ 90 表示"近期仍在領跑"。**Longs 5 組只用 12M 是有意為之**——它們本身的事件過濾已經足夠強(EarningsGap / RVol 放量 / GapUp / 52W 新高 / Top Gainer),再疊一層 3M 會把 universe 收得過窄。每個旋鈕都能單獨調,設為 `0` 即關閉該層;`_rs` / `_shorts` 兩個鍵不配置時缺省繼承 `min_rs_percentile_longs`(把 12M ∩ 3M 雙閘隨時可以按組加回來)。把 `min_rs_percentile_3m` 設為 `0` 就能關掉整個 3M 層(連雲端 CSV 也不再拉取)。HK Shorts 走 3M ≥ 90 單閘(`[hk_shorts].min_rs_percentile_3m`,缺省繼承 `min_rs_percentile_longs_3m`,在 yfinance 批量下載前先篩 universe);Morning Gap 不做 RS 閘。

ADR% 取代了過去的 Finviz `beta > 1.5` 過濾:beta 反映的是多年來與大盤的相關性,容易誤殺那些眼下正活躍、真正 in-play 的中大盤催化劑票;而 ADR%(Kullamägi 式)直接衡量一隻股票當下的波動幅度。

### Longs(5 個策略,互斥)

Oliver Kell 的動量/突破 setup。按優先級排序,靠前的策略優先命中,每隻 ticker 每天最多進一個 Longs 文件。

| 優先級 | 策略          | Finviz 過濾                                                                                              |
| ------ | ------------- | -------------------------------------------------------------------------------------------------------- |
| 1      | `EarningsGap` | Small Cap+, Earnings Today, Avg Vol > 500K, Price > $20, Rel Vol > 1.5, Gap Up 5%+, Above SMA50 & SMA200 |
| 2      | `HighVolume`  | Small Cap+, Avg Vol > 500K, Price > $20, Day Up, Above SMA50 & SMA200 + yfinance Rel Vol ≥ 3× 20 日均量  |
| 3      | `GapUp`       | Small Cap+, Avg Vol > 500K, Price > $20, Gap Up 3%+, Above SMA50 & SMA200                                |
| 4      | `NewHigh52W`  | Small Cap+, Avg Vol > 500K, Price > $20, New 52W High, Above SMA50 & SMA200                              |
| 5      | `TopGainers`  | Small Cap+, Avg Vol > 500K, Price > $20, Above SMA50 & SMA200, Signal: Top Gainers                       |

這 5 組同樣要過全局的 Dollar Volume / ADR% 閘以及 IBD RS 12M ≥ 90。**Longs 不加 3M 層**——事件過濾本身選的就是新鮮動量。

### EOD Repeat(美股,只讀 sidecar)

已在跨日 master 裡、今天又命中 4 個*事件類* Longs 子組之一的老票(`[eod_repeat] keys` = EarningsGap/HighVolume/GapUp/TopGainers;NewHigh52W 和 Leaders 刻意排除——它們是持續狀態,會天天復燃刷屏),彙總寫入 `<date>_Repeat.txt` 供每日複查。對 master 只讀:不寫回,各組自身的 `.txt` 仍保持「只出新票」的語義。Futu/TV 同步映射默認註釋掉(`Repeat` 分組/列表須先手動建好),啟用前同步是 no-op。

### Leaders(5 個策略,合併)

站上 SMA50 與 SMA200 的長期趨勢領頭羊。五個策略共用同一套基礎過濾,只在 perf 窗口上有所不同。

**基礎過濾:** Small Cap+, Avg Vol > 500K, Price > $20, Above SMA50, Above SMA200,外加全局閘(**RS 走 3M ≥ 90 單閘**,12M 層 `min_rs_percentile` 當前設 0 關閉)。

| 策略              | Performance 閾值  |
| ----------------- | ----------------- |
| Leaders 4W +30%   | 4 周 perf ≥ 30%   |
| Leaders 13W +50%  | 13 周 perf ≥ 50%  |
| Leaders 26W +100% | 26 周 perf ≥ 100% |
| Leaders YTD +100% | YTD perf ≥ 100%   |
| Leaders 52W +150% | 52 周 perf ≥ 150% |

### US Shorts

Kullamägi 拋物線 blow-off setup。分兩階段:先用 Finviz Ownership 預過濾,再在一次共享下載上做 yfinance 後處理。

**Phase 1 — Finviz Ownership:** SMA20 +20%, Above SMA50, Avg Vol > 1M(Finviz 3 個月均量), Cap > $50M(2026-08-24 從 $300M 下調);隨後套 **IBD RS 3M ≥ 90**(12M 層 `min_rs_percentile_shorts` 當前設 0 關閉),在進 yfinance batch 之前先篩掉一批。

**Phase 2 — yfinance + Futu 市值快照,順序:performance → dollar volume → ADR% → 連續上漲天數。**

| 過濾                               | 閾值                                     | 數據源                                               |
| ---------------------------------- | ---------------------------------------- | ---------------------------------------------------- |
| 市值(perf 分桶用)                  | 實時 USD 值                              | Futu 快照 `total_market_val` → Finviz Ownership 兜底 |
| Dollar Volume                      | ≥ $100M(20 日均量)                       | yfinance                                             |
| ADR%                               | ≥ 4.0%(20 日)                            | yfinance                                             |
| Performance — Large Cap (≥ $10B)   | 2、3 或 4 周內 Up 50%+                   | yfinance                                             |
| Performance — Mid Cap ($2B–$10B)   | 2、3 或 4 周內 Up 200%+                  | yfinance                                             |
| Performance — Small Cap ($50M–$2B) | 2、3 或 4 周內 Up 300%+                  | yfinance                                             |
| 連續上漲天數                       | ≥ 3 個綠天(若開市則排除今日未完成的 bar) | yfinance                                             |

市值取自 Futu 的精確數值,而不是 Finviz 那種 `"6.96M"`/`"1.23B"` 的粗略字符串——後者在 $2B / $10B 分界附近很容易分錯桶。

### RS — Relative Strength(條件觸發)

Oliver Kell 的相對強度打法,專挑弱市裡扛住的股票。**只在 SPY 和 QQQ 當日都跌 ≥ 1.0% 時才運行**(`check_market_down`,閾值寫在代碼裡)。

過濾:Small Cap+, Avg Vol > 500K, Price > $20, Day Up, Above SMA50 & SMA200, Dollar Volume ≥ $100M, ADR% ≥ 4.0%, **IBD RS 3M ≥ 90**(12M 層 `min_rs_percentile_rs` 當前設 0 關閉)。

### RS-line 趨勢標註(僅日誌)

雲端腳本會把 `rs_below_ma` / `rs_days_below_ma` / `rs_frac_below_ma` 三列寫進 `data/{us_rs_3m,hk_rs}/<date>.csv`(TraderLion 式 **RS line** = 價 ÷ 基準,再與它自己的 EMA21 比較)。EOD 日誌據此*標註*那些 RS line 持續處於均線下方(走弱)的長線側 survivors。這一步**只寫日誌**,不影響 `.txt` 輸出,也不進 dedup;跨日 master 的手動裁剪走 `--mode rs-line-audit`。配置見 `[rs_line]`。

**每日 RS 最強快照:** 每個 EOD wrapper(`run_eod.sh` / `run_hk_eod.sh`)結尾會以 soft 步驟追加跑一次 `--dry-run` audit,按 RS 線趨勢給跨日 master 打分,並把最強的 `[rs_line].top_n`(10)隻寫入 `output/TV/US/rs_us_<date>.txt`(美股,10:00 slot 之後;直接落在 TradingView 資料夾,方便導入)和 `output/hk_rs_<date>.txt`(港股,20:00 slot 之後)。帶日期、逗號分隔、結果為空不寫文件、同日重跑覆蓋;定時跑絕不裁剪 master。保留期:快照與 audit 報告及 sidecar 統一 4 天窗口(今天 + 前 3 天)。

### IPO(自動收集的 sidecar)

收集那些通過了某個 Longs/Leaders/RS 的 Finviz 篩選、卻因日線歷史不足被 yfinance 丟棄的長線候選——典型就是最近幾個月剛上市的新股。這批候選再過一道按歷史深度分級的 ladder(實現於 `us_ipo.filter_us_ipo_candidates`,與 HK 的 `filter_hk_ipo_candidates` 對應),這樣上市第 30 天的新股仍能浮現,而上市第 200 天的則要通過幾乎完整的長線基線:

| 閘門         | 閾值                             | 條件                                          |
| ------------ | -------------------------------- | --------------------------------------------- |
| min history  | ≥ 20 交易日                      | 總是(前 19 天成交量噪聲太大,直接剔除)         |
| cap          | ≥ $300M                          | 總是(cap 來自 screener pass 時抓的 Finviz 值) |
| price        | ≥ $20                            | 總是                                          |
| avg vol      | ≥ 500K 股/天                     | 僅當 ≥ 20 天                                  |
| $vol         | ≥ $100M                          | 僅當 ≥ 20 天                                  |
| ADR%         | ≥ 4.0%                           | 僅當 ≥ 20 天                                  |
| above SMA50  | —                                | 僅當 ≥ 50 天                                  |
| above SMA200 | —                                | 僅當 ≥ 200 天                                 |
| 3M RS        | ≥ 90(對 Fred6725 raw_score 分佈) | 僅當 ≥ 64 天                                  |

閾值對齊 US Longs 基線,歷史攢滿後即可無縫晉升。3M RS 閘的處理比較特殊:IPO 候選(上市不足 120 天)不在 Fred6725 universe 裡,所以 ladder 先在本地算出它的分數,再用 `np.searchsorted` 放到 Fred6725 `raw_score` 分佈裡排名——相當於問"這隻新股要是今天加入 universe,會排在什麼位置"。`min_rs_percentile_3m = 0` 時整個 RS 閘跳過。

- 輸出:`output/TV/US/<date>_IPO.txt`,加 Webull 鏡像與 Futu 分組 `IPO`。
- 跨日 master:`output/state/eod_seen_IPO.txt`,獨立於 `eod_seen_US.txt`——這樣一隻新股攢夠歷史後,會在第一個合格日直接落進它本該屬於的長線側分組。
- 一道保護:出現在 12M Fred6725 RS 表裡的 ticker 必有 ≥ 12 個月歷史,不可能是新股;這類丟棄只是 yfinance 的瞬時缺口,會在 ladder 之前先從 IPO 桶裡剔除。

### HK Shorts

方法學與 US Shorts 一致,數據源換成 HKEX 股票列表(約 2,400 隻主板股)加 yfinance,閾值全部用 HKD 原生值:**3M RS ≥ 90 單閘**(vs HSI,對齊 US Shorts;在 yfinance 批量下載前先篩 universe,表中缺失的票保留),cap ≥ HKD 50M(2026-08-24 從 300M 下調,與美股 $50M 下限同步),avg vol ≥ 1M 股/天(做空側獨有的下限,長線側是 500K),dollar volume ≥ HKD 100M(對齊美股 $100M),ADR% ≥ 4.0%,按 HKD 10B / 2B / 50M 三檔市值分別對應 perf 50/200/300%,連續上漲 ≥ 3 天;輸出 `HKEX:NNN` 格式並去掉前導零。已於 2026-05-06 重新啟用。

### HK 長線側:EarningsGap / HighVolume / GapUp / Leaders / RS

五個策略,數據源為 **yfinance**(k 線 + HSI)加 **Futu**(市值 + HSI 實時日漲幅)。最初是 Futu-only 方案,但 Futu 免費/Lv1 檔只能覆蓋主板約 12% 的 12 個月歷史,IBD 12 個月 RS 算法幾乎無票可排,於是長線 k 線改走 yfinance——它對幾乎每隻 ticker 都能穩定提供 2 年以上數據。方法學對齊 US 的 Longs/Leaders/RS,閾值用 HKD 原生值,universe 為 HKEX 主板股(約 2,400 隻)。輸出 `output/TV/HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,RS}.txt`,採用 `HKEX:NNN` 的 TradingView 格式(去前導零,否則 TV 會靜默拒絕 `HKEX:0148` 這類寫法)。

**統一基線 (`[hk_settings]`):**

| 閘門                 | 閾值                | 備註                                                                                                                                                                                          |
| -------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Market Cap           | ≥ HK$300M           | 對小盤友好;HK 流動性約比美股薄 10×                                                                                                                                                            |
| Avg Volume           | ≥ 500K 股/天(20 日) | 對齊 US `sh_avgvol_o500`                                                                                                                                                                      |
| Dollar Volume        | ≥ HK$100M(20 日)    | 對齊美股 $100M;與 HK Shorts 持平                                                                                                                                                              |
| ADR%                 | ≥ 3.0%(20 日)       | 從 4.0% 調低;HK 藍籌波動率結構性偏低                                                                                                                                                          |
| Last Price           | ≥ HK$20             | HK 原生(`min_price`)                                                                                                                                                                          |
| Above SMA50 & SMA200 | 兩者                | 對齊 US `ta_sma50_pa` + `ta_sma200_pa`,套在每個長線側過濾上                                                                                                                                   |
| RS Percentile        | 按組分工(vs HSI)    | **事件組(EarningsGap/HighVolume/GapUp)12M ≥ 90;Leaders/RS 組 3M ≥ 90**(`min_rs_percentile_longs` / `min_rs_percentile_longs_3m`,設 0 關閉對應層);對齊美股結構;IBD 算法 vs HSI,非 Fred6725 CSV |

**各策略閘門**(按優先級排序,靠前的優先命中,每隻 ticker 每天最多進一個 HK 長線側文件)。五個策略都繼承上面的統一基線(現已含 SMA50 & SMA200 趨勢過濾),所以下表列的是疊加在基線之上的附加閘門:

| 優先級 | 策略           | 附加閘門                                                                                                       |
| ------ | -------------- | -------------------------------------------------------------------------------------------------------------- |
| 1      | HK EarningsGap | gap ≥ 3% + RVol ≥ 3(形態代理——HK 無財報日曆)                                                                   |
| 2      | HK HighVolume  | RVol ≥ 3                                                                                                       |
| 3      | HK GapUp       | gap ≥ 3%                                                                                                       |
| 4      | HK Leaders     | 滿足任一(4w +30 / 13w +50 / 26w +100 / YTD +100 / 52w +150)                                                    |
| 5      | HK RS          | 當日收紅(last > prev_close,≙ 美股 `ta_perf_dup`);**條件觸發**——僅當 HSI 日漲幅 ≤ −1.0%(`hsi_rs_trigger`)時運行 |

**HK RS 算法**:沿用美股那套 `0.4·R3 + 0.2·R6 + 0.2·R9 + 0.2·R12` 加權季度收益公式(再疊一層 3M),基準換成 HSI(`^HSI`),百分位在 HK 主板 universe 內排名。**計算放在 GitHub Actions 雲端**,把 12M、3M 和 RS-line 三部分合成一張 CSV 發佈到 `data/hk_rs/<date>.csv`;`hk_rs.py` 負責拉取並拆分,拉不到就往前回退最多 3 天。HK 長線側的 **metrics frame** 同樣雲端發佈到 `data/hk_metrics/`,由 `hk_metrics.build_hk_metrics_cloud` 拉取,雲端取不到時再回退到本地實時 yfinance 抓取。**週末補跑**會把數據日映射到上週五(`hk_effective_data_day`)——週五收盤已結算,直接命中週五的雲端 metrics/RS CSV 全覆蓋運行,不裁剪 K 線也不跳過 HSI 條件 RS 組;平日假期沒有日曆數據,仍走 404 → 本地回退。

**OpenD 軟依賴**:HK 長線側的 k 線與 HSI 歷史都來自 yfinance,所以 OpenD 掛掉不會清空 .txt 文件。OpenD 不在線時,市值取不到會變 NaN(cap ≥ HK$300M 的基線會把所有票篩掉),條件 RS 的 HSI 觸發快照和 Futu 同步都跳過,但排序與寫文件本身照常跑完;OpenD 在線時則完整填充。每個策略寫進各自的 append-only Futu 分組(`HKEarningsGap`、`HKHighVolume`、`HKGapUp`、`HKLeaders`、`HKRS`),這些分組須在首次運行前於 Futu PC 客戶端手動建好。

### HK IPO(自動收集的 sidecar)

與 US IPO sidecar 對應。收集 HKEX 主板 universe 裡 yfinance 有返回、但日線收盤不足 253 行(不夠做 IBD 12 個月 RS 計算)的 ticker——幾乎都是剛進 yfinance、還沒攢滿 12 個月數據的 HK 新股。

- **基線按歷史深度分級、逐檔啟用。** 每道閘門只在 ticker 攢夠數據後才生效——過了 20 交易日底線(與 US sidecar 對齊)的新股仍能較早浮現,上市 200 天的則要通過幾乎完整的長線基線:

  | 閘門         | 閾值         | 條件                                                               |
  | ------------ | ------------ | ------------------------------------------------------------------ |
  | min history  | ≥ 20 交易日  | 總是(頭 19 天成交量噪聲太大)                                       |
  | cap          | ≥ HK$300M    | 總是                                                               |
  | price        | ≥ HK$20      | 總是                                                               |
  | avg vol      | ≥ 500K 股/天 | 僅當 ≥ 20 交易日                                                   |
  | $vol         | ≥ HK$100M    | 僅當 ≥ 20 交易日                                                   |
  | ADR%         | ≥ 3.0%       | 僅當 ≥ 20 交易日                                                   |
  | above SMA50  | —            | 僅當 ≥ 50 交易日                                                   |
  | above SMA200 | —            | 僅當 ≥ 200 交易日                                                  |
  | 3M RS        | ≥ 90(vs HSI) | 僅當 ≥ 64 交易日(12M 按定義跳過;≥ 64 天卻不在雲端表裡的票會被丟棄) |

  各檔閾值直接讀 `[hk_settings]`,與 HK 長線側基線保持一致,歷史攢滿 253 行時可無縫晉升。

- **輸出:** `output/TV/HK/<date>_IPO.txt`,鏡像到 Webull。
- **獨立跨日 master:** `output/state/eod_seen_HKIPO.txt`。一旦新股攢到 ≥ 253 行,就從 IPO 桶裡退出,在第一個合格日落進本該屬於的長線側分組(長線側 master `eod_seen_HK.txt` 與之分開,互不汙染)。
- **Futu 分組:** append-only `HKIPO`——首次運行前須在 Futu PC 客戶端手動建好。

### Morning Gap(盤前 + 盤中,每日 9 次掃描)

兩階段盤中缺口掃描器。每輪掃描各寫各的按 offset 留檔文件(0 結果的掃描不生成文件):**盤前(開盤前 20/10/5 分鐘)**寫 `MorningGapPre{20,10,5}.txt`;**盤後(開盤後 5/10/15/20/25/30 分鐘)**寫 `MorningGap{5..30}.txt`,並多加一層盤中累計量閘門,專挑開盤頭 30 分鐘成交量就已追平 20 日均日量的股票——按 Kullamägi 的判斷,這是催化劑驅動、機構進場的信號。

**Phase 1 — Futu 快照 discovery(取代 Finviz `ta_topgainers`——後者按常規盤 perf 排名、錯過盤前 gapper):**

| 過濾       | 閾值                                                | 數據源                     |
| ---------- | --------------------------------------------------- | -------------------------- |
| Universe   | NASDAQ / NYSE / AMEX, listed, `stock_type = STOCK`  | Futu `get_stock_basicinfo` |
| Market Cap | ≥ $300M                                             | `total_market_val`         |
| Price      | ≥ $20                                               | `last_price`               |
| Gap(盤前)  | `pre_change_rate` ≥ 5%(且 `pre_volume > 0`)         | `pre_change_rate`          |
| Gap(盤後)  | `(last_price − prev_close) / prev_close × 100` ≥ 5% | 由快照推導                 |

**Phase 2 — yfinance 後處理 + Futu 盤中量:**

| 過濾                | 閾值                                                                              | 盤前 | 盤後 |
| ------------------- | --------------------------------------------------------------------------------- | ---- | ---- |
| Dollar Volume       | ≥ $100M(20 日均量)                                                                | ✓    | ✓    |
| ADR%                | ≥ 4.0%(20 日);gap ≥ 10% 時門檻放寬到 3.0%                                         | ✓    | ✓    |
| SMA50 / SMA200 趨勢 | **實時價**(盤前價 / `last_price`)站上兩者;gap ≥ 10% 僅豁免 SMA50——SMA200 永不豁免 | ✓    | ✓    |
| 20 日 Avg Volume    | ≥ 500K 股/天                                                                      | ✓    | ✓    |
| 盤中累計量          | 自 9:30 ET 起的 RTH 累計量 ≥ 20 日均日量                                          | —    | ✓    |

趨勢閘的比較基準是實時價(`sma_use_live_price`,設 `false` 恢復舊的昨收基準);SMA50/SMA200 均線本身始終由已完成日線計算。SMA50 豁免(`sma_bypass_gap_percent`)和 ADR% 放寬(`adr_bypass_gap_percent` / `adr_bypass_min_percent`)都是按市場配置、僅作用於 morning-gap 的旋鈕——EOD 閘門不受影響。

**HK 變體(`hk-morning-gap`)**:僅盤後(Futu 無港股盤前數據)——09:30 HKT 開盤後 +10…+60 分鐘共 6 輪掃描,各寫 `HKMorningGap{10..60}.txt`。基線對齊 `[hk_settings]`(gap ≥ 5%、ADR% ≥ 3.0%),同樣使用實時價趨勢閘和 SMA50 豁免;累計量閘門用 Futu 快照的當日成交量(無 yfinance 1 分鐘兜底)。ADR 旁路代碼已接線但配置未啟用——HK 基線本來就是 3.0%。

需要 FutuOpenD 在線,並具備 US Lv1 BBO 實時報價權限;否則 Phase 1 discovery 和盤後量過濾都會返回空,且沒有 Finviz 兜底。每輪掃描一旦發現*新*票(當天更早的掃描裡沒出現過),就推一條 ntfy 通知。

## 每日 CANSLIM 報告

美股 EOD 跑完後,`--mode report --market us` 會讀取當天帶日期的長線側 `.txt` 文件,按分組優先級排序、每個市場上限 30 只,再調用所配置的 LLM 後端,為每隻 ticker 生成 CANSLIM 風格的基本面加展望簡報。輸出為自包含的 `output/Reports/PostMarket/<date>_{us,hk}.html`(CSS 內聯、無外部依賴,雙擊即可在任意瀏覽器打開),不再生成 Markdown。只有美股報告在排程裡(`run_eod.sh`);`run_hk_eod.sh` 已跳過 report 步驟,`--market hk` 僅供手動調用。

**後端(`[report] backend`,大小寫不敏感;全部走 Anthropic Python SDK):**

| 後端                                      | Web 上下文                             | 模型                                                                                                        | 密鑰                                                                          |
| ----------------------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `deepseek`(**shipped 默認**)              | 手動 tool-loop → Tavily 搜索           | `deepseek-v4-pro`(Anthropic 兼容端點)                                                                       | `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`                                         |
| `anthropic`(`backend` 未配置時的代碼默認) | 原生 `web_search_20250305` server tool | `claude-sonnet-4-6`                                                                                         | `ANTHROPIC_API_KEY`                                                           |
| `kimi` / `glm` / `minimax`                | 手動 tool-loop → Tavily 搜索           | `kimi-k2-turbo-preview` / `glm-4.6` / `MiniMax-M2`(各廠商的 Anthropic 兼容端點,可經 `[report.<name>]` 覆蓋) | `MOONSHOT_API_KEY` / `ZHIPUAI_API_KEY` / `MINIMAX_API_KEY` + `TAVILY_API_KEY` |

| 方面              | 細節                                                                                                                                                                                                                                                                                                      |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **輸入 (US)**     | 8 個帶日期文件:`EarningsGap`, `HighVolume`, `Leaders`, `GapUp`, `NewHigh52W`, `IPO`, `TopGainers`, `RS`                                                                                                                                                                                                   |
| **輸入 (HK)**     | 6 個帶日期文件:`EarningsGap`, `HighVolume`, `Leaders`, `GapUp`, `IPO`, `RS`(無 NewHigh52W / TopGainers)                                                                                                                                                                                                   |
| **上限 & 優先級** | 30 只/市場(`MAX_TICKERS_PER_REPORT`);`EarningsGap > HighVolume > Leaders > GapUp > NewHigh52W > IPO > TopGainers > RS`。溢出的列在 "Truncated" 尾部小節。                                                                                                                                                 |
| **結構化字段**    | US 基本面 **SEC EDGAR companyfacts 優先**、yfinance 逐字段兜底(緩存於 `output/state/edgar_cache/`);HK 直接用 yfinance。字段:Market Cap, Price, EPS(最新季 + YoY), Revenue(最新季 + YoY), **5 年年度 YoY + 最近 4 季 YoY 軌跡**(兩者), PE, ROE, Inst. Hold %, 最近財報日。RS 百分位取自緩存的 IBD/HSI 表。 |
| **定性小節**      | 模型生成,每隻 ticker 最多 2 次 web 搜索(`web_search_max_uses` / `max_search_calls`):公司速覽, 基本面/財報, 競爭力, 政策/政府支持, 新產品/催化劑, 風險點, 綜合判斷。                                                                                                                                       |
| **雙語**          | 快照字段保持英文/數字;定性分析用簡體中文。                                                                                                                                                                                                                                                                |
| **軟失敗**        | 與 Futu-sync 契約一致——wrapper 退出碼只反映 EOD 步。缺後端密鑰(各後端所需見上表)→ 該步跳過並 warning,`.txt` 產物不受影響。4xx 配置錯誤快速失敗,給出獨立的 `[配置錯誤]` 佔位;5xx/429/超時重試一次後回退到 `[分析失敗]`。                                                                                   |
| **排除**          | US Shorts, HK Shorts, Morning Gap——技術/盤中打法,基本面不驅動入場。                                                                                                                                                                                                                                       |
| **成本區間**      | DeepSeek + Tavily ~$0.5/天/市場(默認,便宜約 80%);Anthropic 原生 `web_search` ~$1–2/天/市場。                                                                                                                                                                                                              |

**配置:** 把後端密鑰寫進 `.env`(可從 `.env.example` 拷一份,裡面按後端列全了所需密鑰):默認的 DeepSeek 後端需要 `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`,Anthropic 後端需要 `ANTHROPIC_API_KEY`,Kimi/GLM/MiniMax 後端需要對應廠商密鑰 + `TAVILY_API_KEY`。wrapper 腳本(`scripts/run_eod.sh` / `scripts/run_hk_eod.sh`)會在報告步之前 `source .env`;交互式運行時 `report/state.py` 也會自動從項目根加載 `.env`。

### 盤前 catalyst 報告

一份**獨立**的短報告。當盤前掃描發現新的 US gapper 時,由 morning-gap 路徑 **spawn 出一個 detached 子進程**來生成(`[morning_gap_catalyst]`),絕不能阻塞 morning-gap 主進程。無論 `[report] backend` 配成什麼,它都**固定用 DeepSeek + Tavily**,且只讀 JSON 快照 sidecar(不碰 Futu / yfinance)。輸出為 `output/Reports/PreMarket/<date>_us_premarket.html`,盤前任一掃描(-20/-10/-5)發現新票都會觸發,多次掃描間基於 `output/state/` 下的 markdown 累積文件重新渲染(單次上限 `max_tickers_per_run`,默認 10;每隻 ticker 最多搜索 `max_search_calls` = 3 次)。寫完後再推一條 "Catalyst Report Ready" 的 ntfy 通知,附上報告路徑。

## Dedup(去重)

- **Longs 內部** — 5 個策略互斥(優先級 `EarningsGap > HighVolume > GapUp > NewHigh52W > TopGainers`)。
- **跨組** — 長線側優先級 `Longs > Leaders > RS`。
- **跨日 master** — `output/state/eod_seen_{US,HK,IPO,HKIPO}.txt`。每隻 ticker 首次出現時進且僅進一個長線側分組;後續運行只發*新*票。兩個市場互相獨立;IPO/HKIPO 各有自己的 master,這樣一隻晉升的票之後能出現在它本該屬於的分組。刪文件即重置。
- **SMA50 自動清理(僅美股)** — 每次 `us-eod` 開頭、加載 master 之前,master 裡凡是收盤價**連續 2 個完整交易日低於 SMA50**(`[sma50_prune] consecutive_days`)**且最新收盤低於前一日收盤**(仍在下跌——線下反彈的票保留)的票會被自動從 `eod_seen_US.txt` 移除,之後可在未來的 EOD 重新合格、重新出現。移除前先備份(`eod_seen_US.txt.bak.<stamp>`)。軟失敗:yfinance 整體失敗則跳過本次清理;歷史缺失/過短的票保留不動。
- **RS-line audit 裁剪(手動)** — `--mode rs-line-audit` 按 RS 線趨勢給 master 打分並 y/N 確認後裁剪(見 RS-line 章節)。每日定時跑的是 `--dry-run`,絕不動 master。
- **不進跨日 master**:Shorts, Morning Gap。對它們來說重新檢測才有意義。
- **EOD Repeat(美股)** — 會被 master 壓掉、但今天又命中事件組的老票,彙總浮現在 `<date>_Repeat.txt`(對 master 只讀;見 EOD Repeat 一節)。

## 輸出 (Output)

```
output/
├── TV/                        # 逗號分隔,供 TradingView "Import list..."
│   ├── US/<date>_{EarningsGap,HighVolume,GapUp,NewHigh52W,TopGainers,Leaders,Shorts,RS,IPO,Repeat,MorningGapPre{20,10,5},MorningGap{5..30}}.txt
│   ├── US/rs_us_<date>.txt    # 每日 RS 最強 top-10 快照,美股(來自定時 rs-line audit)
│   └── HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,Shorts,RS,IPO,HKMorningGap{10..60}}.txt
├── Webull/                    # 換行分隔鏡像,供 Webull "Upload as File"
│   ├── US/<date>_*.txt
│   └── HK/<date>_*.txt
├── Reports/                   # 只有 HTML;保留 7 天
│   ├── PostMarket/<date>_us.html      # 每日 CANSLIM 簡報(僅美股)
│   └── PreMarket/<date>_us_premarket.html   # 盤前 catalyst 報告
├── hk_rs_<date>.txt           # 每日 RS 最強 top-10 快照,港股
├── rs_line_audit_{US,HK}_<date>{,_drop,_keep_ranked}.txt   # audit 報告 + sidecar
└── state/                     # 跨日 "seen" master、RS 表緩存、morning-gap 每日 seen、EDGAR 緩存
    ├── eod_seen_US.txt        # US 長線側 master(5 Longs 組 + Leaders + RS)
    ├── eod_seen_HK.txt        # HK 長線側 master(EarningsGap/HighVolume/GapUp/Leaders/RS)
    ├── eod_seen_IPO.txt       # US IPO sidecar(獨立——就緒時晉升進 US 分組)
    ├── eod_seen_HKIPO.txt     # HK IPO sidecar(獨立——就緒時晉升進 HK 分組)
    ├── morning_gap_seen_{pre,post}_<date>.txt   # US MorningGap 每日去重(盤前/盤後,每日自動重置)
    ├── hk_morning_gap_seen_post_<date>.txt      # HK MorningGap 每日去重(僅盤後,與 US 獨立)
    ├── ntfy_last_seen.txt           # ntfy 訂閱器的斷點續傳進度(Unix 時間戳)
    ├── rs_rating_<date>.csv         # US 12M IBD RS 百分位緩存(來自 Fred6725/rs-log)
    ├── rs_rating_3m_<date>.csv      # US 3M RS 雲端 CSV 的本地緩存(raw_score + 百分位, vs SPY)
    ├── hk_rs_rating_<date>.csv      # HK RS 雲端 CSV 的本地緩存(12M + 3M, vs HSI)
    ├── hk_metrics_<date>.csv        # HK 長線 metrics 幀本地緩存(雲端 data/hk_metrics/)
    ├── premarket_catalyst_<date>.md # Reports/PreMarket/ 背後的 markdown 累積文件(-20/-10/-5 間追加;保留 2 天)
    └── edgar_cache/                 # SEC EDGAR companyfacts 緩存(CANSLIM 報告用)
```

每次運行都會為每個分組寫一個全新的帶日期 `.txt`;結果為空時**不寫文件**(不留 0 字節文件),Futu 同步也會**跳過**,以免在休市日清掉已有分組。

**TradingView ticker 格式:** US 分組用 `NASDAQ:AAPL` / `NYSE:WMT` / `AMEX:GLD`(Finviz 派生)。HK 分組用 `HKEX:NNN` 並**去掉前導零**——TradingView 會靜默拒絕 `HKEX:0148` 這種,必須是 `HKEX:148`。≥ 1000 的代碼(4 位)原樣寫:`HKEX:1810`(小米)、`HKEX:9988`(阿里)。< 1000 的代碼去掉補位:`HKEX:148`(建滔集團)、`HKEX:522`(ASMPT)、`HKEX:700`(騰訊)。

## Futu 自動同步

在 `config.toml` 裡配 `[futu]`。同步 hook 會在每次自選寫成功後觸發,失敗只記 warning,絕不阻塞 `.txt` 輸出。

**一次性設置:**

1. 啟動 [FutuOpenD](https://openapi.futunn.com/futu-api-doc/intro/intro.html),登錄(默認 `127.0.0.1:11111`)。
2. 在 Futu PC 客戶端手動建這些自定義分組(API 只能改已存在的分組,不能新建):
   `EarningsGap`, `HighVolume`, `GapUp`, `NewHigh52W`, `TopGainers`, `Leaders`, `Shorts`, `RS`, `IPO`(US)。

多數 EOD 分組是 append-only——太滿時在客戶端手動清空(Futu 上限:非交易戶 500/組,活躍交易戶 2000)。

## TradingView 自動同步(可選,`tv_sync.py`)

`[tv_sync]`(默認 **`enabled = false`**)把同一批自選同步到 TradingView 列表,走其**非官方 REST API**,用 `sessionid` cookie 認證。憑證讀取順序:先看環境變量(`TV_SESSIONID`、`TV_SESSIONID_SIGN`),再看 `~/.config/momentum-scanner/tv_cookie.json`。18 個列表須先在 TV 網頁手動建好(名字區分大小寫、精確匹配),找不到的名字會記 warning 並跳過。軟失敗契約與 Futu 一致——cookie 過期也絕不阻塞 `.txt` 輸出。Append-only 語義走 TV 自己的 `[tv_sync].append_only_lists` 鍵(與 `[futu].append_only_groups` 同語義;建議兩邊保持一致,避免行為分裂);注意 TV 把 `MorningGap` 保留為獨立列表,而 Futu 那邊併入了 `EarningsGap`。

## 推送通知 (ntfy)

Morning-gap 掃描通過 [ntfy.sh](https://ntfy.sh) 推送四類通知:

- **常規**——本輪出現**新**票(當天同階段更早的掃描裡沒見過的)時推一條,正文列出全部入選票;
- **PROMOTED(高優先級)**——盤前見過的 gapper 在盤後首次通過累計量閘門(盤前缺口被 RTH 成交量確認)時單獨推一條;
- **Catalyst Report Ready**——盤前 catalyst 報告寫完後推一條,附 `output/Reports/PreMarket/` 下的 HTML 路徑;
- **SKIPPED(高優先級)**——定時掃描因網絡遲遲不通(net-ready 閘)而乾淨退出、錯過窗口時推一條,避免窗口丟失得無聲無息。

RS workflow 自觸發器(見「自動化」一節)的失敗告警也複用同一 topic,所有 pipeline 告警落在同一頻道。

在 `config.toml` 裡配 `[notify]`,並在 ntfy 的 iOS/Android app 裡訂閱對應 topic。Mac 本機還有一個常駐 launchd 訂閱器,把同一 topic 的消息橋接到 macOS 通知中心(見「自動化」一節)。

## 安裝 (Setup)

```bash
uv sync                                              # 安裝
uv run main.py --mode us-eod                         # US EOD (Longs/Leaders/Shorts/RS/IPO)
uv run main.py --mode hk-eod                         # HK EOD (Shorts + Longs/Leaders/RS)
uv run main.py --mode morning-gap                    # US 盤中缺口掃描(自動檢測窗口,窗口外乾淨退出)
uv run main.py --mode hk-morning-gap                 # HK 盤中缺口掃描(僅盤後)
uv run main.py --mode report --market us             # 為今日 US 票生成 CANSLIM 簡報 → Reports/PostMarket/(需後端密鑰)
uv run main.py --mode report --market us --date YYYY-MM-DD   # 回填某一天(--market hk 也能跑,但不在排程內)
uv run main.py --mode rs-line-audit --market both    # 按 RS-line 趨勢給跨日 master 打分,提示裁剪(手動)
```

> 不帶後綴的 `--mode eod` 仍會 US、HK 一起跑,但計劃槽用分市場的 `us-eod` / `hk-eod`(10:00 HKT 時 HK 的當日 bar 還沒收完)。

## 自動化(macOS launchd + pmset)

兩個每日 EOD 槽(按收盤時間拆分)、兩個盤中 morning-gap 掃描器、兩個 RS-workflow 自觸發,外加一個常駐 ntfy 訂閱器和一個每週喚醒重排,各自把日誌寫到 `output/` 下:

| 槽             | 觸發                                 | Mode             | Plist                                         |
| -------------- | ------------------------------------ | ---------------- | --------------------------------------------- |
| US EOD         | Tue–Sat 10:00 HKT                    | `us-eod`         | `com.xue.finviz-to-tv.plist`                  |
| HK EOD         | Mon–Fri 20:00 HKT                    | `hk-eod`         | `com.xue.finviz-to-tv.hk-eod.plist`           |
| US Morning Gap | 90 條/周(ET-aware)                   | `morning-gap`    | `com.xue.finviz-to-tv.morning-gap.plist`      |
| HK Morning Gap | Mon–Fri × 6 offsets(9:40–10:30 HKT)  | `hk-morning-gap` | `com.xue.finviz-to-tv.hk-morning-gap.plist`   |
| US RS trigger  | Tue–Sat 08:45 HKT(`gh workflow run`) | —                | `com.xue.finviz-to-tv.us-rs-3m-trigger.plist` |
| HK RS trigger  | Mon–Fri 18:45 HKT(`gh workflow run`) | —                | `com.xue.finviz-to-tv.hk-rs-trigger.plist`    |
| ntfy 訂閱器    | 常駐(KeepAlive)                      | —                | `com.xue.finviz-to-tv.ntfy-subscriber.plist`  |
| 喚醒重排       | 每週日 18:00(root LaunchDaemon)      | —                | `com.xue.finviz-to-tv.schedule-wakes.plist`   |

除喚醒重排裝在 `/Library/LaunchDaemons/`(`pmset` 需要 root)外,其餘 plist 都放在 `~/Library/LaunchAgents/`,源文件副本在 `scripts/`。ntfy 訂閱器把 morning-gap topic 的每條消息橋接到 macOS 通知中心,並用 `output/state/ntfy_last_seen.txt` 記錄進度,睡眠/重啟後只補發漏掉的消息。兩個 RS trigger 會在每個 EOD 前 **75 分鐘**派發雲端 RS/metrics workflow——因為 GitHub 自帶的計劃 cron 不可靠(觀察到延遲數小時,甚至被跳過);好在 workflow 的 commit 步是冪等的,GH cron 與 launchd 雙重觸發也無害。

10:00 HKT 槽落在美股收盤之後(EDT、EST 兩種夏令時都覆蓋),也在每日上游 RS Rating commit 之後。20:00 HKT 槽在 HK 收盤(16:00 HKT)後留了 4 小時餘量,等 k 線數據定稿。US 槽用 `--mode us-eod`,刻意跳過 HK——10:00 HKT 時 HK 才開市 30 分鐘,當日 k 線 bar 還沒收完。每個 EOD 步成功後,wrapper 腳本會以軟失敗方式調一次 `--mode report --market {us,hk}`,讓當天的 CANSLIM 簡報在同一窗口產出;這一步失敗不影響 EOD 退出碼。

```bash
# US 槽
sudo pmset repeat wakeorpoweron TWRFS 09:59:00
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.plist

# HK 槽(無 pmset——Mac 在 20:00 HKT 通常醒著;若睡著 launchd 在下次喚醒時觸發)
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.hk-eod.plist

# Morning-gap(獨立 plist,90 條日曆項:Mon–Fri × 9 offsets × EDT/EST)
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.morning-gap.plist
sudo uv run scripts/schedule_morning_gap_wakes.py    # 排一次性喚醒(首次裝機手動跑;之後由 schedule-wakes LaunchDaemon 每週日 18:00 自動重排)
```

morning-gap 腳本每次觸發都會自校驗 ET 時間,不在窗口內就直接乾淨退出。

## 導入 (Importing)

- **TradingView**:Watchlist → "Import list..." → 選最新的 `output/TV/{US,HK}/<date>_*.txt`。HK ticker 一律不帶前導零(寫 `HKEX:148` 而非 `HKEX:0148`),否則 TradingView 會靜默拒絕。
- **Webull**:Watchlist → "Upload as File" → 從 `output/Webull/{US,HK}/` 選對應文件(換行分隔,逗號格式會被靜默截斷)。

## 配置 (Configuration)

所有 screener 過濾、閾值、Futu/ntfy 設置都在 `config.toml`。架構與貢獻說明見 [`CLAUDE.md`](CLAUDE.md)。

## 依賴 (Dependencies)

Python ≥ 3.12(見 `pyproject.toml`)—— [finviz](https://github.com/mariostoev/finviz), [yfinance](https://github.com/ranaroussi/yfinance), [openpyxl](https://openpyxl.readthedocs.io/), [curl-cffi](https://pypi.org/project/curl-cffi/), [futu-api](https://pypi.org/project/futu-api/), [anthropic](https://pypi.org/project/anthropic/)(報告——所有後端都走它,非 Anthropic 廠商經各自的 Anthropic 兼容端點), [httpx](https://www.python-httpx.org/)(Tavily 搜索 + TV 同步), [markdown](https://pypi.org/project/Markdown/)。開發:pytest + pytest-asyncio。

## 參考資料 (References)

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
