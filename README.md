**English** | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

# Daily Stock Screener Pipeline

**In one sentence: every day, on schedule, this pipeline uses Finviz, yfinance, Futu snapshots, and GitHub-Actions-hosted RS tables to automatically scan US and HK stocks for long/short candidates following the O'Neil / Kell / Kullamägi momentum playbook, exports TradingView / Webull / Futu watchlists, and calls an LLM to generate a CANSLIM briefing.**

A multi-source momentum and short-selling stock scanner: US discovery runs on Finviz, intraday gaps come from Futu snapshots. Results are exported as watchlists directly importable into TradingView / Webull, and auto-synced to custom groups in Futu (moomoo) via OpenAPI; optional sync to TradingView lists (via its unofficial REST API) is also available. On top of that, an LLM API (Claude, DeepSeek, etc.) is called daily to produce a CANSLIM-style research briefing. The screening methodology mainly follows William O'Neil, Oliver Kell, and Kristjan Kullamägi.

> **Status (2026-08-02):** Both US and HK are live. US data comes from Finviz and yfinance, plus a 12M IBD RS CSV and a 3M RS table; HK uses yfinance for k-lines and HSI history (the original Futu-only approach was abandoned — Futu's free/Lv1 tier only covers 12-month history for ~12% of the main board). Futu now only supplies market cap and the real-time HSI daily-change snapshot for the conditional RS trigger on the HK side, and intraday gap discovery plus Shorts market-cap snapshots on the US side, plus watchlist-group sync for both markets.
>
> **The percentile RS tables (US 3M, HK 12M+3M) and the HK long-side metrics frame are computed daily on GitHub Actions and published as CSVs to `data/`; the local pipeline only fetches them** — yfinance computation from a residential IP gets rate-limited halfway through. The RS gates are structurally symmetric across both markets: **event groups (US Longs 5 groups, HK EarningsGap/HighVolume/GapUp) use a single 12M ≥ 90 gate; everything else long-side (Leaders / conditional RS groups in both markets, US Shorts) uses a single 3M ≥ 90 gate**; each group has an independent knob, and the double gate can be re-enabled per group at any time. New issues with less than 12 months of history go through a **history-depth-tiered IPO ladder**.
>
> The HK pipeline has its own 20:00 HKT schedule slot; US runs at 10:00 HKT, each writing its own per-market log. After the US EOD run, the wrapper script runs `--mode report --market us`, generating a CANSLIM briefing (standalone HTML) for the day's newly discovered long-side names; HK is not scheduled for a report. Report backends: DeepSeek V4 + Tavily by default, Anthropic `web_search` as the alternative; Kimi / GLM / MiniMax (+ Tavily) are also supported via their Anthropic-compatible endpoints.

## Screeners

All Finviz-based scans add `ind_stocksonly` to exclude ETFs/ETNs; Morning Gap uses Futu `stock_type=STOCK`, which naturally contains only common stocks.

### Global gates (long-side)

Applied after Finviz screening but before any expensive yfinance computation. All thresholds configurable under `[settings]`.

| Gate                                            | Scope                                       | Threshold                                                            | Source                                                                                                                                                                                                                                                                                      |
| ----------------------------------------------- | ------------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **IBD RS Percentile 12M (event groups)**        | Longs 5 groups                              | ≥ 90 (top 10%; tickers missing from the table are KEPT)              | [Fred6725/rs-log](https://github.com/Fred6725/relative-strength), `RS = 0.4·P3 + 0.2·P6 + 0.2·P9 + 0.2·P12` normalized vs SPY, refreshed weekdays ~01:30 UTC                                                                                                                                |
| **IBD RS Percentile 3M (Leaders/RS/US Shorts)** | Leaders + RS groups + US Shorts (not Longs) | ≥ 90 (top 10%; missing tickers kept)                                 | `RS_3M = 0.5·R21 + 0.3·R42 + 0.2·R63` vs SPY, universe = Fred6725 ticker list (~6100), **computed in the cloud on GitHub Actions** and published to `data/us_rs_3m/<date>.csv` (with `raw_score` for IPO out-of-universe ranking); fetched by `us_rs_3m.py` (walks back ≤ 3 days on a miss) |
| **Dollar Volume**                               | Longs + Leaders                             | price × 20-day avg volume ≥ $100M                                    | yfinance daily bars                                                                                                                                                                                                                                                                         |
| **ADR%**                                        | Longs + Leaders                             | mean(`(High − Low) / Close`) × 100 over last 20 complete bars ≥ 4.0% | yfinance daily bars                                                                                                                                                                                                                                                                         |

**RS scope (one independent knob per group, current effective values):**

| Group                                                               | 12M gate                                                                | 3M gate                                                                     |
| ------------------------------------------------------------------- | ----------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Longs 5 groups (EarningsGap/HighVolume/GapUp/NewHigh52W/TopGainers) | `min_rs_percentile_longs` = **90**                                      | — (no such layer by design)                                                 |
| Leaders                                                             | `min_rs_percentile` = 0 (off)                                           | `min_rs_percentile_3m` = **90**                                             |
| Conditional RS group                                                | `min_rs_percentile_rs` = 0 (off; inherits the longs key when unset)     | `min_rs_percentile_3m` = **90**                                             |
| US Shorts                                                           | `min_rs_percentile_shorts` = 0 (off; inherits the longs key when unset) | `min_rs_percentile_3m` = **90**                                             |
| US IPO ladder (≥ 64 days)                                           | —                                                                       | `min_rs_percentile_3m` (via `np.searchsorted` against Fred6725 `raw_score`) |

Current doctrine: **event groups check long-term strength (12M ≥ 90); everything else long-side checks recent strength (3M ≥ 90)**. 12M ≥ 90 means "long-term leader"; 3M ≥ 90 means "still leading recently". **Using only 12M for the Longs 5 groups is deliberate** — their event filters are already strong (EarningsGap / RVol spike / GapUp / 52W high / Top Gainer), and stacking a 3M layer on top would narrow the universe too much. Every knob is independently tunable; setting `0` disables that layer. The `_rs` / `_shorts` keys default to `min_rs_percentile_longs` when unset (so the 12M ∩ 3M double gate can be re-enabled per group at any time). Setting `min_rs_percentile_3m` to `0` turns off the entire 3M layer (the cloud CSV is no longer fetched either). HK Shorts uses a single 3M ≥ 90 gate (`[hk_shorts].min_rs_percentile_3m`, defaults to `min_rs_percentile_longs_3m`, applied as a universe pre-filter before the yfinance batch); Morning Gap has no RS gate.

ADR% replaced the old Finviz `beta > 1.5` filter: beta reflects multi-year correlation with the index and tends to kill mid/large-cap catalyst names that are genuinely in play right now, while ADR% (Kullamägi-style) directly measures a stock's current range.

### Longs (5 strategies, mutually exclusive)

Oliver Kell's momentum/breakout setups. Ordered by priority: earlier strategies win, and each ticker enters at most one Longs file per day.

| Priority | Strategy      | Finviz filters                                                                                           |
| -------- | ------------- | -------------------------------------------------------------------------------------------------------- |
| 1        | `EarningsGap` | Small Cap+, Earnings Today, Avg Vol > 500K, Price > $20, Rel Vol > 1.5, Gap Up 5%+, Above SMA50 & SMA200 |
| 2        | `HighVolume`  | Small Cap+, Avg Vol > 500K, Price > $20, Day Up, Above SMA50 & SMA200 + yfinance Rel Vol ≥ 3× 20-day avg |
| 3        | `GapUp`       | Small Cap+, Avg Vol > 500K, Price > $20, Gap Up 3%+, Above SMA50 & SMA200                                |
| 4        | `NewHigh52W`  | Small Cap+, Avg Vol > 500K, Price > $20, New 52W High, Above SMA50 & SMA200                              |
| 5        | `TopGainers`  | Small Cap+, Avg Vol > 500K, Price > $20, Above SMA50 & SMA200, Signal: Top Gainers                       |

These 5 groups also pass the global Dollar Volume / ADR% gates and IBD RS 12M ≥ 90. **Longs has no 3M layer** — the event filters themselves select fresh momentum.

### EOD Repeat (US, read-only sidecar)

Tickers already in the cross-day master that fire one of the 4 _event_ Longs groups again today (`[eod_repeat] keys` = EarningsGap/HighVolume/GapUp/TopGainers; NewHigh52W and Leaders are deliberately excluded — they are persistent states that would re-fire daily) are collected into `<date>_Repeat.txt` for daily re-review. Read-only with respect to the master: no write-back, and each group's own `.txt` keeps its "new names only" meaning. The Futu/TV sync mappings ship commented out (the `Repeat` group/list would have to be hand-created first), so sync is a no-op until enabled.

### Leaders (5 strategies, merged)

Long-term trend leaders above SMA50 and SMA200. The five strategies share one base filter set and differ only in the performance window.

**Base filters:** Small Cap+, Avg Vol > 500K, Price > $20, Above SMA50, Above SMA200, plus the global gates (**RS is a single 3M ≥ 90 gate**; the 12M layer `min_rs_percentile` is currently set to 0/off).

| Strategy          | Performance threshold |
| ----------------- | --------------------- |
| Leaders 4W +30%   | 4-week perf ≥ 30%     |
| Leaders 13W +50%  | 13-week perf ≥ 50%    |
| Leaders 26W +100% | 26-week perf ≥ 100%   |
| Leaders YTD +100% | YTD perf ≥ 100%       |
| Leaders 52W +150% | 52-week perf ≥ 150%   |

### US Shorts

Kullamägi's parabolic blow-off setup. Two phases: a Finviz Ownership pre-filter, then yfinance post-processing on one shared download.

**Phase 1 — Finviz Ownership:** SMA20 +20%, Above SMA50, Avg Vol > 1M (Finviz 3-month average), Cap > $50M (lowered from $300M on 2026-08-24); then **IBD RS 3M ≥ 90** (the 12M layer `min_rs_percentile_shorts` is currently 0/off) prunes the list before the yfinance batch.

**Phase 2 — yfinance + Futu market-cap snapshot, in order: performance → dollar volume → ADR% → consecutive up days.**

| Filter                             | Threshold                                                 | Source                                                       |
| ---------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------ |
| Market cap (for perf bucketing)    | real-time USD value                                       | Futu snapshot `total_market_val` → Finviz Ownership fallback |
| Dollar Volume                      | ≥ $100M (20-day avg volume)                               | yfinance                                                     |
| ADR%                               | ≥ 4.0% (20-day)                                           | yfinance                                                     |
| Performance — Large Cap (≥ $10B)   | Up 50%+ within 2, 3, or 4 weeks                           | yfinance                                                     |
| Performance — Mid Cap ($2B–$10B)   | Up 200%+ within 2, 3, or 4 weeks                          | yfinance                                                     |
| Performance — Small Cap ($50M–$2B) | Up 300%+ within 2, 3, or 4 weeks                          | yfinance                                                     |
| Consecutive up days                | ≥ 3 green days (excluding today's incomplete bar if open) | yfinance                                                     |

Market cap uses Futu's exact value rather than Finviz's coarse `"6.96M"`/`"1.23B"` strings — the latter easily mis-buckets names near the $2B / $10B boundaries.

### RS — Relative Strength (conditionally triggered)

Oliver Kell's relative-strength play: stocks holding up in a weak tape. **Runs only when both SPY and QQQ are down ≥ 1.0% on the day** (`check_market_down`, threshold hardcoded).

Filters: Small Cap+, Avg Vol > 500K, Price > $20, Day Up, Above SMA50 & SMA200, Dollar Volume ≥ $100M, ADR% ≥ 4.0%, **IBD RS 3M ≥ 90** (the 12M layer `min_rs_percentile_rs` is currently 0/off).

### RS-line trend annotation (log only)

The cloud scripts write `rs_below_ma` / `rs_days_below_ma` / `rs_frac_below_ma` as extra columns in `data/{us_rs_3m,hk_rs}/<date>.csv` (TraderLion-style **RS line** = price ÷ benchmark, compared to its own EMA21). The EOD log uses these to _annotate_ long-side survivors whose RS line sits persistently below its MA (weakening). This step **writes to the log only** — no effect on `.txt` output or dedup; manual pruning of the cross-day master goes through `--mode rs-line-audit`. Config: `[rs_line]`.

**Daily strongest-RS snapshot:** a `--dry-run` audit is chained as a soft step at the end of each EOD wrapper (`run_eod.sh` / `run_hk_eod.sh`), scoring the cross-day master by RS-line trend and writing the strongest `[rs_line].top_n` (10) names to `output/TV/US/rs_us_<date>.txt` (US, after the 10:00 slot; lands in the TradingView folder for direct import) and `output/hk_rs_<date>.txt` (HK, after the 20:00 slot). Dated, comma-separated, skipped when empty, overwritten on a same-day rerun; the scheduled run never prunes the master. Retention: snapshots and the audit report + sidecars all share a 4-day window (today + 3 prior).

### IPO (auto-collected sidecar)

Collects long-side candidates that passed a Longs/Leaders/RS Finviz screen but were dropped by yfinance for insufficient daily history — typically stocks that listed in the last few months. These candidates then pass a history-depth-tiered ladder (implemented in `us_ipo.filter_us_ipo_candidates`, mirroring HK's `filter_hk_ipo_candidates`), so a stock 30 days post-IPO can still surface while one 200 days post-IPO must pass a nearly full long-side baseline:

| Gate         | Threshold                                     | Condition                                                    |
| ------------ | --------------------------------------------- | ------------------------------------------------------------ |
| min history  | ≥ 20 trading days                             | always (first 19 days are too volume-noisy)                  |
| cap          | ≥ $300M                                       | always (cap from the Finviz value captured at screener pass) |
| price        | ≥ $20                                         | always                                                       |
| avg vol      | ≥ 500K shares/day                             | only when ≥ 20 days                                          |
| $vol         | ≥ $100M                                       | only when ≥ 20 days                                          |
| ADR%         | ≥ 4.0%                                        | only when ≥ 20 days                                          |
| above SMA50  | —                                             | only when ≥ 50 days                                          |
| above SMA200 | —                                             | only when ≥ 200 days                                         |
| 3M RS        | ≥ 90 (vs the Fred6725 raw_score distribution) | only when ≥ 64 days                                          |

Thresholds align with the US Longs baseline, so a name graduates seamlessly once its history fills in. The 3M RS gate is special: IPO candidates (< 120 days listed) aren't in the Fred6725 universe, so the ladder computes their score locally and ranks it into the Fred6725 `raw_score` distribution via `np.searchsorted` — effectively asking "if this new issue joined the universe today, where would it rank". With `min_rs_percentile_3m = 0` the whole RS gate is skipped.

- Output: `output/TV/US/<date>_IPO.txt`, plus the Webull mirror and Futu group `IPO`.
- Cross-day master: `output/state/eod_seen_IPO.txt`, independent of `eod_seen_US.txt` — so once a new issue has enough history, it lands in its proper long-side group on the first qualifying day.
- One safeguard: a ticker present in the 12M Fred6725 RS table necessarily has ≥ 12 months of history and cannot be a new issue; such drops are just transient yfinance gaps and are removed from the IPO bucket before the ladder.

### HK Shorts

Same methodology as US Shorts, with the data source switched to the HKEX stock list (~2,400 main-board names) plus yfinance, and all thresholds in native HKD: **single 3M RS ≥ 90 gate** (vs HSI, aligned with US Shorts; applied as a universe pre-filter before the yfinance batch, missing tickers kept), cap ≥ HKD 50M (lowered from 300M on 2026-08-24, in step with the US $50M floor), avg vol ≥ 1M shares/day (a shorts-only floor; long side uses 500K), dollar volume ≥ HKD 100M (aligned with the US $100M), ADR% ≥ 4.0%, perf 50/200/300% by the HKD 10B / 2B / 50M cap tiers, ≥ 3 consecutive up days; output in `HKEX:NNN` format with leading zeros stripped. Re-enabled 2026-05-06.

### HK long side: EarningsGap / HighVolume / GapUp / Leaders / RS

Five strategies, sourced from **yfinance** (k-lines + HSI) plus **Futu** (market cap + real-time HSI daily change). Originally Futu-only, but Futu's free/Lv1 tier covers 12-month history for only ~12% of the main board, leaving the IBD 12-month RS algorithm with almost nothing to rank — so long-side k-lines moved to yfinance, which reliably serves 2+ years for nearly every ticker. Methodology mirrors US Longs/Leaders/RS, thresholds in native HKD, universe = HKEX main board (~2,400 names). Output: `output/TV/HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,RS}.txt`, in TradingView's `HKEX:NNN` format (leading zeros stripped — TV silently rejects forms like `HKEX:0148`).

**Unified baseline (`[hk_settings]`):**

| Gate                 | Threshold                  | Notes                                                                                                                                                                                                                                          |
| -------------------- | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Market Cap           | ≥ HK$300M                  | small-cap friendly; HK liquidity is ~10× thinner than the US                                                                                                                                                                                   |
| Avg Volume           | ≥ 500K shares/day (20-day) | aligned with US `sh_avgvol_o500`                                                                                                                                                                                                               |
| Dollar Volume        | ≥ HK$100M (20-day)         | aligned with the US $100M; same as HK Shorts                                                                                                                                                                                                   |
| ADR%                 | ≥ 3.0% (20-day)            | lowered from 4.0%; HK blue-chip volatility is structurally lower                                                                                                                                                                               |
| Last Price           | ≥ HK$20                    | HK-native (`min_price`)                                                                                                                                                                                                                        |
| Above SMA50 & SMA200 | both                       | aligned with US `ta_sma50_pa` + `ta_sma200_pa`, applied to every long-side filter                                                                                                                                                              |
| RS Percentile        | per-group split (vs HSI)   | **event groups (EarningsGap/HighVolume/GapUp) 12M ≥ 90; Leaders/RS groups 3M ≥ 90** (`min_rs_percentile_longs` / `min_rs_percentile_longs_3m`, set 0 to disable a layer); mirrors the US structure; IBD algorithm vs HSI, not the Fred6725 CSV |

**Per-strategy gates** (priority-ordered, earlier wins, each ticker enters at most one HK long-side file per day). All five inherit the unified baseline above (now including the SMA50 & SMA200 trend filter), so the table lists only the gates stacked on top:

| Priority | Strategy       | Additional gates                                                                                                                              |
| -------- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| 1        | HK EarningsGap | gap ≥ 3% + RVol ≥ 3 (pattern proxy — HK has no earnings calendar)                                                                             |
| 2        | HK HighVolume  | RVol ≥ 3                                                                                                                                      |
| 3        | HK GapUp       | gap ≥ 3%                                                                                                                                      |
| 4        | HK Leaders     | any of (4w +30 / 13w +50 / 26w +100 / YTD +100 / 52w +150)                                                                                    |
| 5        | HK RS          | green close (last > prev_close, ≙ US `ta_perf_dup`); **conditionally triggered** — runs only when HSI daily change ≤ −1.0% (`hsi_rs_trigger`) |

**HK RS algorithm**: same weighted quarterly-return formula as the US (`0.4·R3 + 0.2·R6 + 0.2·R9 + 0.2·R12`, plus the 3M layer), benchmark switched to HSI (`^HSI`), percentiles ranked within the HK main-board universe. **Computation runs in the cloud on GitHub Actions**, merging 12M, 3M, and RS-line into one CSV published to `data/hk_rs/<date>.csv`; `hk_rs.py` fetches and splits it, walking back up to 3 days on a miss. The HK long-side **metrics frame** is likewise cloud-published to `data/hk_metrics/` and fetched by `hk_metrics.build_hk_metrics_cloud`, falling back to a live local yfinance fetch when the cloud copy is unavailable. **Weekend reruns** map the data day to the previous Friday (`hk_effective_data_day`) — Friday's close is settled, so they hit Friday's cloud metrics/RS CSVs at full coverage, don't trim k-lines, and don't skip the HSI conditional RS group; weekday holidays have no calendar data and still fall through 404 → local fallback.

**OpenD as a soft dependency**: HK long-side k-lines and HSI history come from yfinance, so an OpenD outage never empties the `.txt` files. With OpenD offline, market caps become NaN (the cap ≥ HK$300M baseline then filters everything out), the conditional-RS HSI trigger snapshot and Futu sync are skipped, but ranking and file writing still complete; with OpenD online everything is fully populated. Each strategy writes to its own append-only Futu group (`HKEarningsGap`, `HKHighVolume`, `HKGapUp`, `HKLeaders`, `HKRS`), which must be created manually in the Futu PC client before first run.

### HK IPO (auto-collected sidecar)

The counterpart of the US IPO sidecar. Collects tickers in the HKEX main-board universe that yfinance returns but with fewer than 253 daily closes (not enough for the IBD 12-month RS computation) — almost always HK new issues that recently appeared on yfinance and haven't accumulated 12 months of data yet.

- **The baseline is history-depth-tiered, enabled gate by gate.** Each gate takes effect only once the ticker has enough data — past the 20-trading-day floor (mirroring the US sidecar) a young issue can still surface early, while one 200 days post-IPO must pass a nearly full long-side baseline:

  | Gate         | Threshold         | Condition                                                                                                        |
  | ------------ | ----------------- | ---------------------------------------------------------------------------------------------------------------- |
  | min history  | ≥ 20 trading days | always (first 19 days are too volume-noisy)                                                                      |
  | cap          | ≥ HK$300M         | always                                                                                                           |
  | price        | ≥ HK$20           | always                                                                                                           |
  | avg vol      | ≥ 500K shares/day | only when ≥ 20 trading days                                                                                      |
  | $vol         | ≥ HK$100M         | only when ≥ 20 trading days                                                                                      |
  | ADR%         | ≥ 3.0%            | only when ≥ 20 trading days                                                                                      |
  | above SMA50  | —                 | only when ≥ 50 trading days                                                                                      |
  | above SMA200 | —                 | only when ≥ 200 trading days                                                                                     |
  | 3M RS        | ≥ 90 (vs HSI)     | only when ≥ 64 trading days (12M skipped by definition; a ≥ 64-day name missing from the cloud table is dropped) |

  All tier thresholds read directly from `[hk_settings]`, consistent with the HK long-side baseline, so a name graduates seamlessly at 253 rows of history.

- **Output:** `output/TV/HK/<date>_IPO.txt`, mirrored to Webull.
- **Independent cross-day master:** `output/state/eod_seen_HKIPO.txt`. Once a new issue reaches ≥ 253 rows it exits the IPO bucket and lands in its proper long-side group on the first qualifying day (the long-side master `eod_seen_HK.txt` is separate — no cross-contamination).
- **Futu group:** append-only `HKIPO` — must be created manually in the Futu PC client before first run.

### Morning Gap (pre-market + post-open, 9 scans daily)

A two-stage intraday gap scanner. Each scan writes its own per-offset snapshot (a scan that finds nothing writes no file): **pre-market (20/10/5 minutes before the open)** writes `MorningGapPre{20,10,5}.txt`; **post-open (5/10/15/20/25/30 minutes after the open)** writes `MorningGap{5..30}.txt` with one extra intraday cumulative-volume gate, selecting stocks whose volume in the first 30 minutes already matches their 20-day average daily volume — per Kullamägi, the signature of catalyst-driven institutional buying.

**Phase 1 — Futu snapshot discovery (replacing Finviz `ta_topgainers`, which ranks by regular-session perf and misses pre-market gappers):**

| Filter     | Threshold                                           | Source                     |
| ---------- | --------------------------------------------------- | -------------------------- |
| Universe   | NASDAQ / NYSE / AMEX, listed, `stock_type = STOCK`  | Futu `get_stock_basicinfo` |
| Market Cap | ≥ $300M                                             | `total_market_val`         |
| Price      | ≥ $20                                               | `last_price`               |
| Gap (pre)  | `pre_change_rate` ≥ 5% (and `pre_volume > 0`)       | `pre_change_rate`          |
| Gap (post) | `(last_price − prev_close) / prev_close × 100` ≥ 5% | derived from the snapshot  |

**Phase 2 — yfinance post-processing + Futu intraday volume:**

| Filter               | Threshold                                                                                                         | Pre | Post |
| -------------------- | ----------------------------------------------------------------------------------------------------------------- | --- | ---- |
| Dollar Volume        | ≥ $100M (20-day avg volume)                                                                                       | ✓   | ✓    |
| ADR%                 | ≥ 4.0% (20-day); floor relaxed to 3.0% when gap ≥ 10%                                                             | ✓   | ✓    |
| SMA50 / SMA200 trend | **live price** (pre-market print / `last_price`) above both; gap ≥ 10% waives SMA50 only — SMA200 is never waived | ✓   | ✓    |
| 20-day Avg Volume    | ≥ 500K shares/day                                                                                                 | ✓   | ✓    |
| Intraday cum. volume | RTH cumulative volume since 9:30 ET ≥ 20-day avg daily volume                                                     | —   | ✓    |

The trend gate's comparison basis is the live price (`sma_use_live_price`, set `false` to restore the old previous-close basis); the SMA50/SMA200 averages themselves are always computed from completed daily bars. The SMA50 waiver (`sma_bypass_gap_percent`) and the ADR% relaxation (`adr_bypass_gap_percent` / `adr_bypass_min_percent`) are per-market morning-gap-only knobs — EOD gates are untouched.

**HK variant (`hk-morning-gap`)**: post-open only (Futu has no HK pre-market data) — 6 scans at +10…+60 minutes after the 09:30 HKT open, each writing `HKMorningGap{10..60}.txt`. Baseline aligns with `[hk_settings]` (gap ≥ 5%, ADR% ≥ 3.0%), same live-price trend gate and SMA50 waiver; the cumulative-volume gate uses the Futu snapshot's day volume (no yfinance 1-minute fallback). The ADR bypass is wired but left off in config — the HK base floor is already 3.0%.

Requires FutuOpenD online with US Lv1 BBO real-time quote entitlement; otherwise Phase 1 discovery and the post-open volume filter both return empty, with no Finviz fallback. Whenever a scan finds _new_ names (not seen in an earlier scan that day), it pushes an ntfy notification.

## Daily CANSLIM report

After the US EOD run, `--mode report --market us` reads the day's dated long-side `.txt` files, orders them by group priority with a per-market cap of 30 names, then calls the configured LLM backend to generate a CANSLIM-style fundamentals-plus-outlook briefing per ticker. Before that call, `report/evidence.py` pre-fetches yfinance news / analyst consensus / earnings calendar plus recent EDGAR filings per ticker for a single no-tool LLM call, falling back to the backend's web search (Tavily tool-loop for DeepSeek-style vendors, native web_search for Anthropic) only when that evidence is thin (`[report.evidence]`; EOD only — the pre-market catalyst path below still always searches). Output: a self-contained `output/Reports/PostMarket/<date>_{us,hk}.html` (inline CSS, zero external dependencies — double-click to open in any browser); no Markdown twin. Only the US report is scheduled (`run_eod.sh`); `run_hk_eod.sh` skips the report step, so `--market hk` is manual-only.

**Backends (`[report] backend`, case-insensitive; all go through the Anthropic Python SDK):**

| Backend                                         | Web context                              | Model                                                                                                                               | Keys                                                                          |
| ----------------------------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `deepseek` (**shipped default**)                | manual tool-loop → Tavily search         | `deepseek-v4-pro` (Anthropic-compatible endpoint)                                                                                   | `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`                                         |
| `anthropic` (code default when `backend` unset) | native `web_search_20250305` server tool | `claude-sonnet-4-6`                                                                                                                 | `ANTHROPIC_API_KEY`                                                           |
| `kimi` / `glm` / `minimax`                      | manual tool-loop → Tavily search         | `kimi-k2-turbo-preview` / `glm-4.6` / `MiniMax-M2` (each vendor's Anthropic-compatible endpoint; overridable via `[report.<name>]`) | `MOONSHOT_API_KEY` / `ZHIPUAI_API_KEY` / `MINIMAX_API_KEY` + `TAVILY_API_KEY` |

| Aspect                   | Details                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Input (US)**           | 8 dated files: `EarningsGap`, `HighVolume`, `Leaders`, `GapUp`, `NewHigh52W`, `IPO`, `TopGainers`, `RS`                                                                                                                                                                                                                                                                                            |
| **Input (HK)**           | 6 dated files: `EarningsGap`, `HighVolume`, `Leaders`, `GapUp`, `IPO`, `RS` (no NewHigh52W / TopGainers)                                                                                                                                                                                                                                                                                           |
| **Cap & priority**       | 30 per market (`MAX_TICKERS_PER_REPORT`); `EarningsGap > HighVolume > Leaders > GapUp > NewHigh52W > IPO > TopGainers > RS`. Overflow is listed in a trailing "Truncated" section.                                                                                                                                                                                                                 |
| **Structured fields**    | US fundamentals: **SEC EDGAR companyfacts first**, yfinance per-field fallback (cached in `output/state/edgar_cache/`); HK uses yfinance directly. Fields: Market Cap, Price, EPS (latest quarter + YoY), Revenue (latest quarter + YoY), **5-year annual YoY + last-4-quarter YoY trajectory** (both), PE, ROE, Inst. Hold %, latest earnings date. RS percentile from the cached IBD/HSI tables. |
| **Qualitative sections** | Model-generated, at most 2 web searches per ticker (`web_search_max_uses` / `max_search_calls`): company snapshot, fundamentals/earnings, competitiveness, policy/government support, new products/catalysts, risks, overall verdict.                                                                                                                                                              |
| **Bilingual**            | Snapshot fields stay in English/numeric; qualitative analysis in Simplified Chinese.                                                                                                                                                                                                                                                                                                               |
| **Soft-fail**            | Same contract as Futu sync — the wrapper exit code reflects only the EOD step. Missing backend keys (per the table above) → step skipped with a warning, `.txt` artifacts unaffected. 4xx config errors fail fast with a standalone `[配置錯誤]` placeholder; 5xx/429/timeouts retry once then fall back to `[分析失敗]`.                                                                          |
| **Excluded**             | US Shorts, HK Shorts, Morning Gap — technical/intraday plays where fundamentals don't drive the entry.                                                                                                                                                                                                                                                                                             |
| **Cost range**           | DeepSeek + Tavily ~$0.5/day/market (default, ~80% cheaper); Anthropic native `web_search` ~$1–2/day/market.                                                                                                                                                                                                                                                                                        |

**Configuration:** put the backend keys in `.env` (copy from `.env.example` — it lists the keys per backend): the default DeepSeek backend needs `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`, the Anthropic backend needs `ANTHROPIC_API_KEY`, and the Kimi/GLM/MiniMax backends need their vendor key + `TAVILY_API_KEY`. The wrapper scripts (`scripts/run_eod.sh` / `scripts/run_hk_eod.sh`) `source .env` before the report step; in interactive runs `report/state.py` also auto-loads `.env` from the project root.

### Pre-market catalyst report

A **standalone** short report. When a pre-market scan finds new US gappers, the morning-gap path **spawns a detached subprocess** to generate it (`[morning_gap_catalyst]`); it must never block the morning-gap main process. Regardless of `[report] backend`, it **always uses DeepSeek + Tavily**, and reads only the JSON snapshot sidecar (never touches Futu / yfinance). Output: `output/Reports/PreMarket/<date>_us_premarket.html`, triggered by any pre-market scan (-20/-10/-5) that finds fresh tickers, re-rendered across scans from a markdown accumulator in `output/state/` (per-run cap `max_tickers_per_run`, default 10; at most `max_search_calls` = 3 searches per ticker). Once written, it pushes a "Catalyst Report Ready" ntfy notification with the report path.

## Dedup

- **Within Longs** — the 5 strategies are mutually exclusive (priority `EarningsGap > HighVolume > GapUp > NewHigh52W > TopGainers`).
- **Cross-group** — long-side priority `Longs > Leaders > RS`.
- **Cross-day master** — `output/state/eod_seen_{US,HK,IPO,HKIPO}.txt`. Each ticker enters exactly one long-side group on first appearance; subsequent runs emit only _new_ names. Markets are independent; IPO/HKIPO have their own masters, so a graduated name can later appear in the group it belongs to. Delete the file to reset.
- **SMA50 auto-prune (US only)** — at the top of every `us-eod` run, before the master is loaded, any master ticker whose close has been **below its SMA50 for 2 consecutive completed days** (`[sma50_prune] consecutive_days`) **and whose latest close is below the prior day's close** (still declining — a rebound under the line is kept) is automatically removed from `eod_seen_US.txt`, so it can re-qualify and re-surface on a future EOD run. The master is backed up first (`eod_seen_US.txt.bak.<stamp>`). Soft-fail: a total yfinance failure skips the prune; tickers with missing/short history are kept.
- **RS-line audit prune (manual)** — `--mode rs-line-audit` scores the master by RS-line trend and prompts y/N before pruning (see the RS-line section). The scheduled daily run is `--dry-run` and never touches the master.
- **Not in the cross-day master**: Shorts, Morning Gap. For these, re-detection is the point.
- **EOD Repeat (US)** — old names the master would suppress that re-fired an event group today are surfaced in `<date>_Repeat.txt` (read-only w.r.t. the master; see the EOD Repeat section).

## Output

```
output/
├── TV/                        # comma-separated, for TradingView "Import list..."
│   ├── US/<date>_{EarningsGap,HighVolume,GapUp,NewHigh52W,TopGainers,Leaders,Shorts,RS,IPO,Repeat,MorningGapPre{20,10,5},MorningGap{5..30}}.txt
│   ├── US/rs_us_<date>.txt    # daily strongest-RS top-10 snapshot, US (from the scheduled rs-line audit)
│   └── HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,Shorts,RS,IPO,HKMorningGap{10..60}}.txt
├── Webull/                    # newline-separated mirror, for Webull "Upload as File"
│   ├── US/<date>_*.txt
│   └── HK/<date>_*.txt
├── Reports/                   # HTML only; 7-day retention
│   ├── PostMarket/<date>_us.html      # daily CANSLIM briefing (US only)
│   └── PreMarket/<date>_us_premarket.html   # pre-market catalyst report
├── hk_rs_<date>.txt           # daily strongest-RS top-10 snapshot, HK
├── rs-audit/                  # audit report + sidecars; 5-day retention
│   └── rs_line_audit_{US,HK}_<date>{,_drop,_keep_ranked}.txt
└── state/                     # cross-day "seen" masters, RS table caches, morning-gap daily seen, EDGAR cache
    ├── eod_seen_US.txt        # US long-side master (5 Longs groups + Leaders + RS)
    ├── eod_seen_HK.txt        # HK long-side master (EarningsGap/HighVolume/GapUp/Leaders/RS)
    ├── eod_seen_IPO.txt       # US IPO sidecar (independent — promotes into US groups when ready)
    ├── eod_seen_HKIPO.txt     # HK IPO sidecar (independent — promotes into HK groups when ready)
    ├── morning_gap_seen_{pre,post}_<date>.txt   # US MorningGap daily dedup (pre/post, auto-reset daily)
    ├── hk_morning_gap_seen_post_<date>.txt      # HK MorningGap daily dedup (post-open only, independent of US)
    ├── ntfy_last_seen.txt           # ntfy subscriber resume progress (Unix timestamp)
    ├── rs_rating_<date>.csv         # US 12M IBD RS percentile cache (from Fred6725/rs-log)
    ├── rs_rating_3m_<date>.csv      # local cache of the US 3M RS cloud CSV (raw_score + percentile, vs SPY)
    ├── hk_rs_rating_<date>.csv      # local cache of the HK RS cloud CSV (12M + 3M, vs HSI)
    ├── hk_metrics_<date>.csv        # local cache of the HK long-side metrics frame (cloud data/hk_metrics/)
    ├── premarket_catalyst_<date>.md # markdown accumulator behind Reports/PreMarket/ (appended across -20/-10/-5; 2-day)
    └── edgar_cache/                 # SEC EDGAR companyfacts cache (for the CANSLIM report)
```

Every run writes a fresh dated `.txt` per group; empty lists write **no file** (no 0-byte artifacts), and Futu sync is **skipped** on empty results, so an off-day never wipes an existing group.

**TradingView ticker format:** US groups use `NASDAQ:AAPL` / `NYSE:WMT` / `AMEX:GLD` (Finviz-derived). HK groups use `HKEX:NNN` with **leading zeros stripped** — TradingView silently rejects forms like `HKEX:0148`; it must be `HKEX:148`. Codes ≥ 1000 (4 digits) are written as-is: `HKEX:1810` (Xiaomi), `HKEX:9988` (Alibaba). Codes < 1000 lose the padding: `HKEX:148` (Kingboard), `HKEX:522` (ASMPT), `HKEX:700` (Tencent).

## Futu auto-sync

Configure `[futu]` in `config.toml`. The sync hook fires after every successful watchlist write; failures only log a warning and never block `.txt` output.

**One-time setup:**

1. Start [FutuOpenD](https://openapi.futunn.com/futu-api-doc/intro/intro.html) and log in (default `127.0.0.1:11111`).
2. Manually create these custom groups in the Futu PC client (the API can only modify existing groups, not create them):
   `EarningsGap`, `HighVolume`, `GapUp`, `NewHigh52W`, `TopGainers`, `Leaders`, `Shorts`, `RS`, `IPO` (US).

Most EOD groups are append-only — clear them manually in the client when they get full (Futu limits: 500/group for non-trading accounts, 2000 for active traders).

## TradingView auto-sync (optional, `tv_sync.py`)

`[tv_sync]` (default **`enabled = false`**) syncs the same watchlists to TradingView lists via its **unofficial REST API**, authenticated with the `sessionid` cookie. Credential lookup order: environment variables (`TV_SESSIONID`, `TV_SESSIONID_SIGN`) first, then `~/.config/momentum-scanner/tv_cookie.json`. The 18 lists must be created manually on the TV website first (names are case-sensitive, exact match); unmatched names log a warning and are skipped. Same soft-fail contract as Futu — an expired cookie never blocks `.txt` output. Append-only semantics use TV's own `[tv_sync].append_only_lists` key (same meaning as `[futu].append_only_groups`; keep the two in step to avoid behavior split); note TV keeps `MorningGap` as a separate list, while on the Futu side it's merged into `EarningsGap`.

## Push notifications (ntfy)

The morning-gap scans push four kinds of notifications via [ntfy.sh](https://ntfy.sh):

- **Regular** — pushed when a scan finds **new** names (not seen in an earlier same-phase scan that day), body lists all selected names;
- **PROMOTED (high priority)** — pushed separately when a pre-market gapper first passes the cumulative-volume gate post-open (the pre-market gap confirmed by RTH volume);
- **Catalyst Report Ready** — pushed when the pre-market catalyst report is written, with the HTML path under `output/Reports/PreMarket/`;
- **SKIPPED (high priority)** — pushed when a scheduled scan clean-exits without scanning because the network never came up within the net-ready gate, so a lost window doesn't pass silently.

The RS-workflow self-triggers (see "Automation") reuse the same topic for their failure alerts, so all pipeline alerts land in one channel.

Configure `[notify]` in `config.toml` and subscribe to the topic in the ntfy iOS/Android app. On the Mac itself, a resident launchd subscriber bridges the same topic to macOS Notification Center (see "Automation").

## Setup

```bash
uv sync                                              # install
uv run main.py --mode us-eod                         # US EOD (Longs/Leaders/Shorts/RS/IPO)
uv run main.py --mode hk-eod                         # HK EOD (Shorts + Longs/Leaders/RS)
uv run main.py --mode morning-gap                    # US intraday gap scan (auto-detects window, clean-exits outside it)
uv run main.py --mode hk-morning-gap                 # HK intraday gap scan (post-open only)
uv run main.py --mode report --market us             # CANSLIM briefing for today's US names → Reports/PostMarket/ (needs backend keys)
uv run main.py --mode report --market us --date YYYY-MM-DD   # backfill a given day (--market hk works too, but is not scheduled)
uv run main.py --mode rs-line-audit --market both    # score the cross-day master by RS-line trend, prompt to prune (manual)
```

> The bare `--mode eod` still runs US and HK together, but the schedule slots use the per-market `us-eod` / `hk-eod` (HK's daily bar isn't complete at 10:00 HKT).

## Automation (macOS launchd + pmset)

Two daily EOD slots (split by market close), two intraday morning-gap scanners, two RS-workflow self-triggers, plus a resident ntfy subscriber and a weekly wake rescheduler, each logging under `output/`:

| Slot            | Trigger                               | Mode             | Plist                                         |
| --------------- | ------------------------------------- | ---------------- | --------------------------------------------- |
| US EOD          | Tue–Sat 10:00 HKT                     | `us-eod`         | `com.xue.finviz-to-tv.plist`                  |
| HK EOD          | Mon–Fri 20:00 HKT                     | `hk-eod`         | `com.xue.finviz-to-tv.hk-eod.plist`           |
| US Morning Gap  | 90 entries/week (ET-aware)            | `morning-gap`    | `com.xue.finviz-to-tv.morning-gap.plist`      |
| HK Morning Gap  | Mon–Fri × 6 offsets (9:40–10:30 HKT)  | `hk-morning-gap` | `com.xue.finviz-to-tv.hk-morning-gap.plist`   |
| US RS trigger   | Tue–Sat 08:45 HKT (`gh workflow run`) | —                | `com.xue.finviz-to-tv.us-rs-3m-trigger.plist` |
| HK RS trigger   | Mon–Fri 18:45 HKT (`gh workflow run`) | —                | `com.xue.finviz-to-tv.hk-rs-trigger.plist`    |
| ntfy subscriber | resident (KeepAlive)                  | —                | `com.xue.finviz-to-tv.ntfy-subscriber.plist`  |
| wake reschedule | Sundays 18:00 (root LaunchDaemon)     | —                | `com.xue.finviz-to-tv.schedule-wakes.plist`   |

Except for the wake rescheduler, which lives in `/Library/LaunchDaemons/` (`pmset` needs root), all plists sit in `~/Library/LaunchAgents/`, with source copies in `scripts/`. The ntfy subscriber bridges every message on the morning-gap topic to macOS Notification Center, tracking progress in `output/state/ntfy_last_seen.txt` so it only replays missed messages after sleep/reboot. The two RS triggers dispatch the cloud RS/metrics workflows **75 minutes** before each EOD — GitHub's own scheduled cron is unreliable (observed hours late, sometimes skipped); the workflow's commit step is idempotent, so a GH-cron + launchd double-fire is harmless.

The 10:00 HKT slot lands after the US close (covering both EDT and EST) and after the daily upstream RS Rating commit. The 20:00 HKT slot leaves a 4-hour margin after the HK close (16:00 HKT) for k-line data to settle. The US slot uses `--mode us-eod`, deliberately skipping HK — at 10:00 HKT the HK session is only 30 minutes old and the daily bar is incomplete. After each successful EOD step, the wrapper script soft-fails through one `--mode report --market {us,hk}` call so the day's CANSLIM briefing lands in the same window; a failure there never affects the EOD exit code.

```bash
# US slot
sudo pmset repeat wakeorpoweron TWRFS 09:59:00
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.plist

# HK slot (no pmset — the Mac is usually awake at 20:00 HKT; if asleep, launchd fires on next wake)
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.hk-eod.plist

# Morning-gap (separate plist, 90 calendar entries: Mon–Fri × 9 offsets × EDT/EST)
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.morning-gap.plist
sudo uv run scripts/schedule_morning_gap_wakes.py    # schedule one-shot wakes (run manually on first install; afterwards the schedule-wakes LaunchDaemon re-schedules every Sunday 18:00)
```

The morning-gap script self-validates ET time on every trigger and clean-exits outside its window.

## Importing

- **TradingView**: Watchlist → "Import list..." → pick the latest `output/TV/{US,HK}/<date>_*.txt`. HK tickers never carry leading zeros (`HKEX:148`, not `HKEX:0148`), or TradingView silently rejects them.
- **Webull**: Watchlist → "Upload as File" → pick the matching file from `output/Webull/{US,HK}/` (newline-separated; comma format gets silently truncated).

## Configuration

All screener filters, thresholds, and Futu/ntfy settings live in `config.toml`. Architecture and contribution notes: [`CLAUDE.md`](CLAUDE.md).

## Dependencies

Python ≥ 3.12 (see `pyproject.toml`) — [finviz](https://github.com/mariostoev/finviz), [yfinance](https://github.com/ranaroussi/yfinance), [openpyxl](https://openpyxl.readthedocs.io/), [curl-cffi](https://pypi.org/project/curl-cffi/), [futu-api](https://pypi.org/project/futu-api/), [anthropic](https://pypi.org/project/anthropic/) (reports — every backend goes through it, non-Anthropic vendors via their Anthropic-compatible endpoints), [httpx](https://www.python-httpx.org/) (Tavily search + TV sync), [markdown](https://pypi.org/project/Markdown/). Dev: pytest + pytest-asyncio.

## References

Only the key resources are listed here — this is not exhaustive; plenty of other material informed the methodology.

**Books:**

- _How to Make Money in Stocks_ — William O'Neil (the source of CANSLIM and the IBD RS system)
- _Victory in Stock Trading: Strategy and Tactics of the 2020 U.S. Investing Champion_ — Oliver Kell
- _Trade Like a Stock Market Wizard_ — Mark Minervini
- _Think & Trade Like a Champion_ — Mark Minervini
- _A Complete Guide to Volume Price Analysis_ — Anna Coulling
- _The Power of Japanese Candlestick Charts_ — Fred K.H. Tam
- _The Trader's Handbook: Winning Habits and Routines of Successful Traders_ — Richard Moglen, Nick Schmidt, et al.
- _Market Wizards: The Next Generation: The World's Top Young Traders Reveal How They Beat the Market_ — Jack D. Schwager, George F. Coyle

**Sites & channels:**

- [Qullamaggie](https://qullamaggie.com/)
- [TraderLion](https://traderlion.com/)
- [Stockbee](https://stockbee.biz/)
- [Investor's Business Daily](https://www.youtube.com/@investorsbusinessdaily) (YouTuber)
- [Real Simple Ariel](https://www.youtube.com/@RealSimpleAriel) (YouTuber)
- [TheOneLanceB](https://www.youtube.com/@TheOneLanceB) (YouTuber)
- [TA Plot](https://www.youtube.com/@TAPlot) (YouTuber)
- [SMB Capital](https://www.youtube.com/@smbcapital)(Youtuber)
- [Qullamaggie](https://www.youtube.com/@Qullamaggie)(Youtuber)

**YouTube videos:**

- [The Simple Trading Setup That Made Lance Breitstein Millions](https://youtu.be/R215f4fj7V8) (TraderLion)
- [Trading Super-performance. Trade Like Market Wizard David Ryan](https://youtu.be/ZK5cnVQ2V3Q) (TraderLion)
- [How Hedge Fund Managers Trade Pullbacks — Exclusive with Charles Harris](https://youtu.be/ivL6E6Lc6gM) (TraderLion)
- [The Wedge Pop Trading Setup of Trading Champion Oliver Kell](https://youtu.be/m8F3KkBDtC0) (TraderLion)
- [The 10 Principles of Trading with Investing Champion Oliver Kell](https://youtu.be/ElocJ-b_NTs) (TraderLion)
- [How to Find and Trade the Next Tesla — Swing Trading Strategy](https://youtu.be/eu8onWJ5y34) (TraderLion)
- [The $1,000,000 Simple Trading System That Took 13 Years to Build](https://youtu.be/iu2gdI1cO88) (TraderLion)
- [Low Risk Stock Setups + PDF File](https://youtu.be/R5ScKXy1ytg) (TA Plot)
- [How To Pyramid Into Stocks (21 Stock Setup Examples + PDF File)](https://youtu.be/11h6iSQkzuA) (TA Plot)
- [Sitting Tight for the Right Low Risk Entry](https://youtu.be/Mt3iZ_Orv0g) (TA Plot)
- [How Do You Know It's Time to Get In a Stock? Analyzing Recent Trades.](https://youtu.be/hfwQUpEflEg) (TA Plot)

**Podcast:**

- [Stock Market Today With IBD](https://podcasts.apple.com/us/podcast/stock-market-today-with-ibd/id1685322096)
