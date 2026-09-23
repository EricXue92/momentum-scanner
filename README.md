**English** | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

# Daily Stock Screener Pipeline

A scheduled momentum scanner for US and HK stocks. Every day it screens long and short candidates following the O'Neil / Kell / Kullamägi playbook, ranks a fixed ETF list by 3-month relative strength, and exports watchlists for TradingView, Webull, and Futu (moomoo).

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [US Screeners](#us-screeners)
  - [Shared Gates](#shared-gates) · [RS Gates by Group](#rs-gates-by-group) · [Longs](#longs) · [Leaders](#leaders) · [Conditional RS](#conditional-rs) · [US Shorts](#us-shorts) · [US IPO](#us-ipo) · [EOD Repeat](#eod-repeat) · [ETF Strength Ranking](#etf-strength-ranking) · [US Morning Gap](#us-morning-gap)
- [HK Screeners](#hk-screeners)
  - [HK Baseline](#hk-baseline) · [HK Long-Side Groups](#hk-long-side-groups) · [HK Shorts](#hk-shorts) · [HK IPO](#hk-ipo) · [HK Morning Gap](#hk-morning-gap)
- [RS Data](#rs-data)
  - [Cloud RS Tables](#cloud-rs-tables) · [RS-Line Annotation](#rs-line-annotation) · [RS-Line Audit and Daily Top 10](#rs-line-audit-and-daily-top-10)
- [Dedup and Pruning](#dedup-and-pruning)
- [Output](#output)
- [Sync and Notifications](#sync-and-notifications)
  - [Futu Sync](#futu-sync) · [TradingView Sync](#tradingview-sync) · [ntfy Notifications](#ntfy-notifications)
- [LLM Reports](#llm-reports)
  - [CANSLIM Report](#canslim-report) · [Pre-Market Catalyst Report](#pre-market-catalyst-report)
- [Automation](#automation)
- [Configuration](#configuration)
- [Dependencies](#dependencies)
- [References](#references)

## Overview

**What runs each day (all times HKT):**

| Run            | Schedule                         | Produces                                                                                            |
| -------------- | -------------------------------- | --------------------------------------------------------------------------------------------------- |
| US EOD         | Tue–Sat 10:00                    | Longs (6 groups), Leaders, conditional RS, Shorts, IPO, Repeat, **ETF strength ranking**, RS top-10 |
| HK EOD         | Mon–Fri 20:00                    | EarningsGap / HighVolume / GapUp / Leaders / conditional RS, Shorts, IPO, RS top-10                 |
| US Morning Gap | 9 scans around the 09:30 ET open | pre-market gappers (−20/−10/−5 min) and volume-confirmed post-open gappers (+5…+30 min), ntfy push  |
| HK Morning Gap | 6 scans after the 09:30 HKT open | post-open gappers (+10…+60 min), ntfy push                                                          |

**Data sources:**

| Source                   | Used for                                                                                                                                         |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Finviz                   | US screener discovery (all scans add `ind_stocksonly`, so no ETFs/ETNs)                                                                          |
| yfinance                 | daily bars — dollar volume, ADR%, Rel Vol, SMA, performance; HK k-lines + HSI history; the ETF ranking                                           |
| Futu OpenD               | morning-gap snapshots (US + HK), exact market caps, HSI day change, watchlist-group sync                                                         |
| GitHub Actions → `data/` | RS percentile tables (US 3M, HK 12M+3M) and the HK metrics frame — computed in the cloud because yfinance throttles home IPs; local only fetches |
| Fred6725/rs-log          | US 12M IBD-style RS percentile                                                                                                                   |
| HKEX stock list          | HK main-board universe (~2,400 names)                                                                                                            |

Results are written as dated `.txt` watchlists (TradingView comma-separated, Webull newline-separated) and auto-synced to Futu groups; TradingView list sync is optional. LLM research reports are available on demand ([daily generation is off](#llm-reports)).

## Quick Start

```bash
uv sync                                              # install
uv run main.py --mode us-eod                         # US EOD (Longs/Leaders/RS/Shorts/IPO/Repeat + ETF ranking)
uv run main.py --mode hk-eod                         # HK EOD (long side + Shorts + IPO)
uv run main.py --mode morning-gap                    # US gap scan (clean-exits outside its ET window)
uv run main.py --mode hk-morning-gap                 # HK gap scan (post-open only)
uv run main.py --mode etf-rs                         # rerun the ETF strength ranking alone
uv run main.py --mode rs-line-audit --market both    # score the master by RS-line trend; prompts before pruning
uv run main.py --mode report --market us             # CANSLIM report for today's US names (manual; needs API keys)
uv run pytest tests/ -v                              # tests
```

`--mode eod` still runs US + HK together, but the schedule uses the per-market modes — at 10:00 HKT the HK daily bar is incomplete.

## US Screeners

### Shared Gates

Applied after Finviz discovery, on yfinance daily bars. Thresholds live in `[settings]`.

| Gate          | Threshold                                                        | Applies to                                   |
| ------------- | ---------------------------------------------------------------- | -------------------------------------------- |
| Dollar Volume | price × 20-day avg volume ≥ $100M (Shorts: ≥ $50M)               | Longs, Leaders, RS, Shorts, IPO, Morning Gap |
| ADR%          | mean(`(High − Low) / Close`) × 100 over 20 completed bars ≥ 4.0% | same                                         |

ADR% (Kullamägi-style) measures how much a stock moves _now_; it replaced the old Finviz `beta > 1.5` filter, which penalised in-play mid/large caps.

### RS Gates by Group

Doctrine: **event groups check long-term strength (12M ≥ 90); everything else checks recent strength (3M ≥ 90).** One independent knob per group; `0` disables a layer.

| Group                                                             | 12M gate                                | 3M gate                         |
| ----------------------------------------------------------------- | --------------------------------------- | ------------------------------- |
| Longs: EarningsGap / HighVolume / GapUp / NewHigh52W / TopGainers | `min_rs_percentile_longs` = **90**      | —                               |
| Longs: TheSetup                                                   | off (per-group `min_rs_percentile = 0`) | —                               |
| Leaders                                                           | `min_rs_percentile` = 0 (off)           | `min_rs_percentile_3m` = **90** |
| Conditional RS                                                    | `min_rs_percentile_rs` = 0 (off)        | **90**                          |
| US Shorts                                                         | `min_rs_percentile_shorts` = 0 (off)    | **90**                          |
| US IPO (≥ 64 days of history)                                     | —                                       | **90**                          |
| Morning Gap, ETF ranking                                          | —                                       | —                               |

- Tickers **missing** from an RS table are kept, not dropped.
- A failed table fetch walks back ≤ 3 days of cache, then passes through ungated with a warning — never a hard failure.
- `min_rs_percentile_rs` / `_shorts` inherit the Longs value when **unset**; keep them explicitly `0` to stay off.

### Longs

Oliver Kell's momentum/breakout setups. Six mutually exclusive groups — the earlier group wins, so a ticker lands in at most one Longs file per day. All share: Small Cap+, Avg Vol > 500K, above SMA50 & SMA200, plus the shared gates.

| Priority | Group         | Additional filters                                                                                               |
| -------- | ------------- | ---------------------------------------------------------------------------------------------------------------- |
| 0        | `TheSetup`    | Price > $10, Gap Up 5%+, Rel Vol ≥ 3× 20-day avg (yfinance). **No RS gate** — the heavy-volume gap is the signal |
| 1        | `EarningsGap` | Price > $20, Earnings Today, Rel Vol > 1.5, Gap Up 5%+                                                           |
| 2        | `HighVolume`  | Price > $20, Day Up, Rel Vol ≥ 3× 20-day avg (yfinance)                                                          |
| 3        | `GapUp`       | Price > $20, Gap Up 3%+                                                                                          |
| 4        | `NewHigh52W`  | Price > $20, New 52-week High                                                                                    |
| 5        | `TopGainers`  | Price > $20, Finviz signal Top Gainers                                                                           |

### Leaders

Long-term trend leaders: Small Cap+, Avg Vol > 500K, Price > $20, above SMA50 & SMA200, shared gates, RS 3M ≥ 90. Five performance windows merged into one `Leaders.txt`:

| 4 weeks | 13 weeks | 26 weeks | YTD     | 52 weeks |
| ------- | -------- | -------- | ------- | -------- |
| ≥ +30%  | ≥ +50%   | ≥ +100%  | ≥ +100% | ≥ +150%  |

### Conditional RS

Stocks holding up in a weak tape. **Runs only when SPY and QQQ are both down ≥ 1.0% on the day.** Filters: Small Cap+, Avg Vol > 500K, Price > $20, Day Up, above SMA50 & SMA200, shared gates, RS 3M ≥ 90.

### US Shorts

Kullamägi's parabolic blow-off setup. Re-detected daily (excluded from all dedup).

1. **Finviz:** price 20%+ above SMA20, above SMA50, Avg Vol > 1M, price > $5, Cap > $50M → then **RS 3M ≥ 90** prunes the list.
2. **yfinance + Futu market cap**, in order:

| Filter              | Threshold                                                                                       |
| ------------------- | ----------------------------------------------------------------------------------------------- |
| Performance         | up within 2, 3, or 4 weeks: **50%+** (cap ≥ $10B) / **200%+** ($2B–$10B) / **300%+** ($50M–$2B) |
| Dollar Volume, ADR% | ≥ $50M (`[shorts].min_dollar_volume`, looser than the global $100M), ≥ 4.0%                     |
| Consecutive up days | ≥ 3 (today's incomplete bar excluded)                                                           |

Market cap comes from the Futu snapshot (exact value; Finviz strings like `"1.23B"` mis-bucket names near the tier boundaries), falling back to Finviz.

### US IPO

An auto-collected sidecar: candidates that passed a Longs/Leaders/RS Finviz screen but were dropped by yfinance for short history. Gates switch on as history accumulates, so a 30-day-old listing can surface while a 200-day-old one must pass nearly the full baseline. The HK IPO ladder is identical apart from thresholds:

| Gate                       | US                | HK                 | Active when |
| -------------------------- | ----------------- | ------------------ | ----------- |
| History                    | ≥ 20 trading days | ≥ 20 trading days  | always      |
| Market cap                 | ≥ $300M           | ≥ HK$300M          | always      |
| Price                      | ≥ $20             | ≥ HK$20            | always      |
| Avg volume / Dollar volume | ≥ 500K / ≥ $100M  | ≥ 500K / ≥ HK$100M | ≥ 20 days   |
| ADR%                       | ≥ 4.0%            | ≥ 3.0%             | ≥ 20 days   |
| Above SMA50                | ✓                 | ✓                  | ≥ 50 days   |
| RS 3M                      | ≥ 90 (vs SPY)     | ≥ 90 (vs HSI)      | ≥ 64 days   |
| Above SMA200               | ✓                 | ✓                  | ≥ 200 days  |

- US new issues aren't in the RS universe, so their 3M score is computed locally and ranked into the cloud table's `raw_score` distribution.
- A ticker found in the 12M RS table has ≥ 12 months of history and can't be an IPO — it is removed from the bucket (transient yfinance gap).
- Own master `eod_seen_IPO.txt`, so a graduated name still enters its proper group later. Output: `<date>_IPO.txt`, Futu group `IPO`.

### EOD Repeat

Names already in the cross-day master that **re-fire an event group today** (TheSetup / EarningsGap / HighVolume / GapUp / TopGainers) are collected in `<date>_Repeat.txt` for re-review. NewHigh52W and Leaders are excluded — they are persistent states that would re-fire daily. Read-only with respect to the master; each group's own file keeps its "new names only" meaning. Futu/TV mappings ship commented out (create a `Repeat` group by hand first). Config: `[eod_repeat]`.

### ETF Strength Ranking

A daily sector / theme rotation view: a fixed list of ~65 ETFs (US sectors and themes, commodities, leveraged products, global markets) ranked by 3-month relative strength.

- **Scoring:** the same 3M RS formula as stocks (`0.5·R21 + 0.3·R42 + 0.2·R63`, relative to SPY). The percentile is **within the ETF list**, not the stock universe.
- **When:** a soft step at the end of every `us-eod` run (a failure never affects EOD); rerun alone with `--mode etf-rs`. Computed locally — one yfinance batch, no cloud step.
- **Output:** `output/TV/US/<date>_ETF_rs.txt`, one ETF per line, **strongest first**, as `TICKER - name | top-5 holdings`:

  ```
  NRGU - 油气 3 倍做多 | VLO、MPC、PSX、CVX、DVN (ETN 挂钩指数前五成分股)
  ARKG - 基因组革命 | TXG、TWST、TEM、CRSP、PSNL
  USO - 原油 | 不适用: 原油期货及现金 / 国债抵押品
  ```

- **Human-readable, not a TradingView import** — the only non-comma `.txt` in `TV/US/`. The full scored table (rank / score / percentile) goes to the EOD log.
- **Maintenance:** edit `[etf_rs.tickers]` (`TICKER = "name"`) to change the list. Holdings come from the static, hand-maintained `[etf_rs.holdings]` table (no API refresh; missing entry → segment omitted).
- **Same name = same instrument:** tickers with an identical name collapse to the strongest one (e.g. the former GDXU / NUGT pair; the current table has no duplicate names, the rule stays in place); the log lists what was hidden.
- Ranking snapshot only: a same-day rerun overwrites; no dedup, no Webull mirror, no Futu/TV sync.

### US Morning Gap

Nine scans per day: **pre-market** at −20/−10/−5 min → `MorningGapPre{20,10,5}.txt`; **post-open** at +5…+30 min → `MorningGap{5..30}.txt`. Each scan writes its own snapshot; a scan that finds nothing writes no file. No RS gate.

**Phase 1 — Futu snapshot discovery** (NASDAQ / NYSE / AMEX common stocks):

| Filter            | Threshold                                      |
| ----------------- | ---------------------------------------------- |
| Market cap, price | ≥ $300M, ≥ $20                                 |
| Gap (pre-market)  | `pre_change_rate` ≥ 5%, with pre-market trades |
| Gap (post-open)   | `(last_price − prev_close) / prev_close` ≥ 5%  |

**Phase 2 — yfinance + Futu volume:**

| Filter               | Threshold                                                                                         | Pre | Post |
| -------------------- | ------------------------------------------------------------------------------------------------- | --- | ---- |
| 20-day avg volume    | ≥ 500K shares/day                                                                                 | ✓   | ✓    |
| Pre-market volume    | Futu `pre_volume` ≥ 5% × 20-day avg volume — drops thin-tape gaps printed on a few hundred shares | ✓   | —    |
| Dollar Volume        | ≥ $100M                                                                                           | ✓   | ✓    |
| ADR%                 | ≥ 4.0%; relaxed to 3.0% when gap ≥ 10%                                                            | ✓   | ✓    |
| SMA50 / SMA200 trend | **live price** above both; gap ≥ 10% waives SMA50 only — SMA200 is never waived                   | ✓   | ✓    |
| Intraday volume      | cumulative volume since 09:30 ET ≥ 20-day avg daily volume (the institutional-buying signature)   | —   | ✓    |

The averages always come from completed daily bars; only the comparison uses the live price. All knobs are in `[morning_gap]` (`min_pre_volume_ratio`, `sma_use_live_price`, `sma_bypass_gap_percent`, `adr_bypass_*`) and affect morning-gap only. Requires FutuOpenD with US Lv1 quotes — there is no Finviz fallback.

## HK Screeners

Same methodology as the US, thresholds in HKD, universe = HKEX main board. Output uses TradingView's `HKEX:NNN` format with leading zeros stripped.

### HK Baseline

Shared by all five long-side groups (`[hk_settings]`):

| Gate                 | Threshold                                           | Notes                                        |
| -------------------- | --------------------------------------------------- | -------------------------------------------- |
| Market cap           | ≥ HK$300M                                           | from the Futu snapshot                       |
| Avg volume           | ≥ 500K shares/day (20-day)                          |                                              |
| Dollar Volume        | ≥ HK$100M (20-day)                                  |                                              |
| ADR%                 | ≥ 3.0%                                              | HK blue chips are structurally less volatile |
| Price                | ≥ HK$20                                             |                                              |
| Above SMA50 & SMA200 | both                                                |                                              |
| RS (vs HSI)          | event groups **12M ≥ 90**; Leaders / RS **3M ≥ 90** | mirrors the US split                         |

### HK Long-Side Groups

Priority-ordered; each ticker enters at most one file per day.

| Priority | Group          | Additional gates                                                                 |
| -------- | -------------- | -------------------------------------------------------------------------------- |
| 1        | HK EarningsGap | gap ≥ 3% + Rel Vol ≥ 3 (pattern proxy — HK has no earnings calendar)             |
| 2        | HK HighVolume  | Rel Vol ≥ 3                                                                      |
| 3        | HK GapUp       | gap ≥ 3%                                                                         |
| 4        | HK Leaders     | any of 4W +30% / 13W +50% / 26W +100% / YTD +100% / 52W +150%                    |
| 5        | HK RS          | green close; **runs only when HSI is down ≥ 1.0% on the day** (`hsi_rs_trigger`) |

- **Data:** the metrics frame and RS tables are fetched from the cloud (`data/hk_metrics/`, `data/hk_rs/`); a cloud miss falls back to a local yfinance fetch.
- **Data-day rule:** only the 20:00 slot uses today's close; earlier runs trim today's incomplete bar and skip the HSI-triggered RS group. Weekend reruns map to the previous Friday.
- **OpenD:** market caps and the HSI trigger come from Futu. With OpenD offline the run still completes, but the cap gate filters everything out.

### HK Shorts

Same as US Shorts in HKD: RS 3M ≥ 90 (vs HSI, universe pre-filter), cap ≥ HK$50M, avg vol ≥ 1M shares/day, dollar volume ≥ HK$50M, ADR% ≥ 4.0%, performance 50% / 200% / 300% by cap tier (≥ HK$10B / HK$2B–10B / HK$50M–2B), ≥ 3 consecutive up days. Config: `[hk_shorts]`.

### HK IPO

Main-board tickers with fewer than 253 daily closes (not enough for a 12M RS). Same history-tiered ladder as the US — see the [US IPO](#us-ipo) table. A ≥ 64-day name missing from the cloud 3M table is dropped. Own master `eod_seen_HKIPO.txt`; output `<date>_IPO.txt`, Futu group `HKIPO`.

### HK Morning Gap

Post-open only (Futu has no HK pre-market data): 6 scans at +10…+60 min after the 09:30 HKT open → `HKMorningGap{10..60}.txt`. Gap ≥ 5%, baseline as `[hk_settings]` (ADR% ≥ 3.0%), same live-price trend gate and SMA50 waiver as the US; the cumulative-volume gate uses the Futu snapshot's day volume. Config: `[hk_morning_gap]`.

## RS Data

### Cloud RS Tables

| Table  | Formula                                                 | Benchmark | Published to                                                                           |
| ------ | ------------------------------------------------------- | --------- | -------------------------------------------------------------------------------------- |
| US 12M | `0.4·P3 + 0.2·P6 + 0.2·P9 + 0.2·P12`                    | SPY       | [Fred6725/rs-log](https://github.com/Fred6725/relative-strength) (weekdays ~01:30 UTC) |
| US 3M  | `0.5·R21 + 0.3·R42 + 0.2·R63`, universe ≈ 6,100 tickers | SPY       | `data/us_rs_3m/<date>.csv` (GitHub Actions)                                            |
| HK     | both formulas, ranked within the HK main board          | HSI       | `data/hk_rs/<date>.csv` (GitHub Actions)                                               |

The local pipeline only fetches these CSVs (cached in `output/state/`, walking back ≤ 3 days on a miss). Because GitHub's own cron is unreliable, launchd dispatches the workflows 75 minutes before each EOD — see [Automation](#automation).

### RS-Line Annotation

The cloud CSVs also carry `rs_below_ma` / `rs_days_below_ma` / `rs_frac_below_ma`: the TraderLion-style **RS line** (price ÷ benchmark) compared with its own EMA21. The EOD log annotates long-side survivors whose RS line sits persistently below its MA. **Log only** — no effect on `.txt` output or dedup. Config: `[rs_line]`.

### RS-Line Audit and Daily Top 10

`uv run main.py --mode rs-line-audit [--market us|hk|both] [--dry-run|--yes]` scores every ticker in the cross-day master by RS-line trend and writes a report plus `_drop` / `_keep_ranked` sidecars to `output/rs-audit/`.

- **Pruning is manual:** the command prompts y/N before removing the drops from `eod_seen_{US,HK}.txt` (backed up first as `.bak.<stamp>`), so pruned names can re-qualify. `--yes` skips the prompt; `--dry-run` never touches the master.
- **Daily top 10:** each EOD wrapper chains a `--dry-run` audit as a soft step and writes the strongest `[rs_line].top_n` (10) names to `output/TV/US/rs_us_<date>.txt` (TradingView-importable) and `output/hk_rs_<date>.txt`. Skipped when empty; a same-day rerun overwrites.

## Dedup and Pruning

1. **Within Longs** — the earlier group wins (`TheSetup > EarningsGap > HighVolume > GapUp > NewHigh52W > TopGainers`).
2. **Across groups** — Longs win over Leaders (HK: `EarningsGap > HighVolume > GapUp > Leaders > RS` within the day).
3. **Cross-day master** — `output/state/eod_seen_{US,HK,IPO,HKIPO}.txt`. A ticker is emitted once, on first appearance; later runs output only _new_ names. Markets are independent. Delete the file to reset.

**Never checked against the master** (re-detection is the point): Shorts, conditional RS, Morning Gap, and the ranking snapshots (ETF, RS top-10).

Two pruners can shrink the US master so weakened names may re-qualify later; both back it up first:

- **SMA50 auto-prune** — at the top of every `us-eod`: a close below SMA50 for 2 consecutive completed days **and** a latest close below the prior close (still declining; a rebound under the line is kept) → removed. Soft-fail: a yfinance outage skips the prune; missing/short-history tickers are kept. Config: `[sma50_prune]`.
- **RS-line audit** — manual, see [above](#rs-line-audit-and-daily-top-10).

Old names that re-fire an event group are surfaced separately in [EOD Repeat](#eod-repeat).

## Output

```
output/
├── TV/                        # comma-separated, for TradingView "Import list..."
│   ├── US/<date>_{TheSetup,EarningsGap,HighVolume,GapUp,NewHigh52W,TopGainers,Leaders,RS,Shorts,IPO,Repeat}.txt
│   ├── US/<date>_{MorningGapPre{20,10,5},MorningGap{5..30}}.txt
│   ├── US/<date>_ETF_rs.txt   # ETF strength ranking — human-readable, NOT an import list
│   ├── US/rs_us_<date>.txt    # daily strongest-RS top 10 (US)
│   └── HK/<date>_{EarningsGap,HighVolume,GapUp,Leaders,RS,Shorts,IPO,HKMorningGap{10..60}}.txt
├── Webull/{US,HK}/<date>_*.txt   # newline-separated mirror, for Webull "Upload as File"
├── hk_rs_<date>.txt           # daily strongest-RS top 10 (HK)
├── rs-audit/                  # rs-line audit report + _drop / _keep_ranked sidecars
├── Reports/                   # HTML reports (manual runs): PostMarket/<date>_us.html, PreMarket/<date>_us_premarket.html
├── state/                     # eod_seen_* masters, RS / metrics caches, morning-gap daily seen, EDGAR cache
└── launchd_*.log              # per-slot logs
```

- **Dated files only** — no "latest" copy. An empty result writes **no file**, and a rerun never deletes an earlier file; the log is the record of a 0-result scan.
- **Ticker format** — US: `NASDAQ:AAPL` / `NYSE:WMT` / `AMEX:GLD`. HK: `HKEX:NNN` **without leading zeros** (`HKEX:700`, `HKEX:9988`) — TradingView silently rejects `HKEX:0700`.
- **Webull** must be newline-separated; its upload silently truncates comma lists.
- **Importing** — TradingView: Watchlist → "Import list..." → a file from `output/TV/`. Webull: Watchlist → "Upload as File" → the matching file from `output/Webull/`.
- **Retention** (auto-cleanup, soft-fail): TV / Webull 5 days · rs-audit 5 days · RS top-10 snapshots 4 days · Reports 7 days · state caches 2–4 days. Masters (`eod_seen_*`), `ntfy_last_seen.txt`, `edgar_cache/` and logs are never touched.

## Sync and Notifications

### Futu Sync

Fires after every watchlist write (`[futu]`). Soft-fail: a failure logs a warning and never blocks `.txt` output; an empty result never wipes a group.

1. Start [FutuOpenD](https://openapi.futunn.com/futu-api-doc/intro/intro.html) and log in (default `127.0.0.1:11111`).
2. Create the 18 custom groups by hand in the Futu client (the API cannot create groups):
   - US: `TheSetup`, `EarningsGap`, `HighVolume`, `GapUp`, `NewHigh52W`, `TopGainers`, `Leaders`, `Shorts`, `RS`, `IPO`
   - HK: `HKShorts`, `HKEarningsGap`, `HKHighVolume`, `HKGapUp`, `HKLeaders`, `HKRS`, `HKIPO`, `HKMorningGap`

All groups are append-only — clear them in the client when full (Futu limits: 500/group, 2,000 for active traders). US morning-gap names sync into `EarningsGap`.

### TradingView Sync

Optional, **off by default** (`[tv_sync]`). Uses TradingView's unofficial REST API with your `sessionid` cookie: env `TV_SESSIONID` / `TV_SESSIONID_SIGN`, or `~/.config/momentum-scanner/tv_cookie.json`. Create the 19 lists by hand first (exact, case-sensitive names: the 18 Futu names plus a separate `MorningGap`); unmatched names are skipped with a warning. Same soft-fail contract as Futu.

### ntfy Notifications

Configure `[notify]` and subscribe to the topic in the [ntfy](https://ntfy.sh) app; a launchd subscriber bridges the same topic to macOS Notification Center.

| Notification          | Sent when                                                                      |
| --------------------- | ------------------------------------------------------------------------------ |
| New gappers           | a morning-gap scan finds names not seen in an earlier same-phase scan that day |
| **PROMOTED** (high)   | a pre-market gapper first passes the post-open cumulative-volume gate          |
| **SKIPPED** (high)    | a scheduled scan exits because the network never came up                       |
| RS workflow failure   | a launchd RS-workflow trigger fails                                            |
| Catalyst Report Ready | the pre-market catalyst report is written (only when that report is enabled)   |

## LLM Reports

> **Daily LLM generation has been off since 2026-09-17.** The report step in `run_eod.sh` is commented out and `[morning_gap_catalyst].enabled = false`. The code is kept for manual runs.

### CANSLIM Report

`uv run main.py --mode report --market us [--date YYYY-MM-DD]` reads the day's long-side `.txt` files (Shorts and Morning Gap excluded), takes up to 30 names by group priority, and writes a self-contained `output/Reports/PostMarket/<date>_us.html`. `--market hk` also works.

- **Structured fields:** SEC EDGAR companyfacts first, yfinance fallback — market cap, EPS / revenue with YoY trajectories, PE, ROE, institutional holding, earnings date, RS percentile.
- **Evidence prefetch** (`[report.evidence]`): yfinance news / analyst consensus / earnings calendar plus recent EDGAR filings are fetched per ticker for **one no-tool LLM call**; web search is offered only when that evidence is thin.
- **Language:** data fields in English, qualitative analysis in Simplified Chinese.
- **Soft-fail:** missing keys skip the report; `.txt` output is never affected.

| Backend (`[report] backend`) | Web search          | Default model                                      | Keys (`.env`)                                                                 |
| ---------------------------- | ------------------- | -------------------------------------------------- | ----------------------------------------------------------------------------- |
| `deepseek` (shipped default) | Tavily              | `deepseek-v4-pro`                                  | `DEEPSEEK_API_KEY` + `TAVILY_API_KEY`                                         |
| `anthropic`                  | native `web_search` | `claude-sonnet-4-6`                                | `ANTHROPIC_API_KEY`                                                           |
| `kimi` / `glm` / `minimax`   | Tavily              | `kimi-k2-turbo-preview` / `glm-4.6` / `MiniMax-M2` | `MOONSHOT_API_KEY` / `ZHIPUAI_API_KEY` / `MINIMAX_API_KEY` + `TAVILY_API_KEY` |

All backends go through the Anthropic SDK (non-Anthropic vendors via their Anthropic-compatible endpoints). Copy `.env.example` to `.env` for the keys.

### Pre-Market Catalyst Report

When enabled (`[morning_gap_catalyst]`), a pre-market scan that finds fresh US gappers spawns a **detached subprocess** — it never blocks the scan — that always uses DeepSeek + Tavily and reads only the scan's JSON snapshot. Output: `output/Reports/PreMarket/<date>_us_premarket.html`, re-rendered across the −20/−10/−5 scans, followed by a "Catalyst Report Ready" ntfy push.

## Automation

macOS launchd + pmset; all times HKT. Plists live in `~/Library/LaunchAgents/` (source copies in `scripts/`).

| Slot            | Trigger                               | Runs                            | Plist (`com.xue.finviz-to-tv.*`) |
| --------------- | ------------------------------------- | ------------------------------- | -------------------------------- |
| US EOD          | Tue–Sat 10:00                         | `scripts/run_eod.sh`            | `plist`                          |
| HK EOD          | Mon–Fri 20:00                         | `scripts/run_hk_eod.sh`         | `hk-eod.plist`                   |
| US Morning Gap  | 90 entries/week (9 offsets × EDT/EST) | `--mode morning-gap`            | `morning-gap.plist`              |
| HK Morning Gap  | Mon–Fri × 6 offsets (09:40–10:30)     | `--mode hk-morning-gap`         | `hk-morning-gap.plist`           |
| US RS trigger   | Tue–Sat 08:45                         | `gh workflow run`               | `us-rs-3m-trigger.plist`         |
| HK RS trigger   | Mon–Fri 18:45                         | `gh workflow run`               | `hk-rs-trigger.plist`            |
| ntfy subscriber | resident (KeepAlive)                  | ntfy → Notification Center      | `ntfy-subscriber.plist`          |
| Wake reschedule | Sundays 18:00 (root LaunchDaemon)     | `schedule_morning_gap_wakes.py` | `schedule-wakes.plist`           |

- Each EOD wrapper runs the EOD scan (watchdog: 30 min US / 45 min HK), then the `--dry-run` rs-line audit as a soft step; the exit code reflects only the EOD scan.
- Morning-gap runs **self-validate their time window and clean-exit outside it**, so extra or missed wakes are harmless.
- The RS triggers dispatch the cloud workflows 75 minutes before each EOD because GitHub's scheduled cron can be hours late or skipped; the workflow commit is idempotent, so a double-fire is harmless.
- 10:00 lands after the US close (EDT and EST) and the upstream 12M RS commit; 20:00 leaves 4 hours after the HK close for data to settle.

```bash
sudo pmset repeat wakeorpoweron TWRFS 09:59:00       # wake for the US slot ("wakeorpoweron" on macOS 26+)
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.plist
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.hk-eod.plist
launchctl load ~/Library/LaunchAgents/com.xue.finviz-to-tv.morning-gap.plist
sudo uv run scripts/schedule_morning_gap_wakes.py    # one-shot morning-gap wakes (first install; the LaunchDaemon re-runs it weekly)
```

## Configuration

All filters, thresholds, group mappings and the ETF list live in [`config.toml`](config.toml); API keys go in `.env`. Architecture notes and invariants: [`CLAUDE.md`](CLAUDE.md).

## Dependencies

Python ≥ 3.12 — [finviz](https://github.com/mariostoev/finviz), [yfinance](https://github.com/ranaroussi/yfinance), [futu-api](https://pypi.org/project/futu-api/), [curl-cffi](https://pypi.org/project/curl-cffi/), [openpyxl](https://openpyxl.readthedocs.io/), [anthropic](https://pypi.org/project/anthropic/) (reports), [httpx](https://www.python-httpx.org/) (Tavily + TV sync), [markdown](https://pypi.org/project/Markdown/). Dev: pytest + pytest-asyncio.

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
- [SMB Capital](https://www.youtube.com/@smbcapital) (YouTuber)
- [Qullamaggie](https://www.youtube.com/@Qullamaggie) (YouTuber)

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
