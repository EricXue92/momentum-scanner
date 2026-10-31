# Decisions and incidents log

The "why" behind the rules in `CLAUDE.md`. Each entry: what happened, what
rule it produced, where that rule lives. `CLAUDE.md` references entries by
date (`→ decisions YYYY-MM-DD`). Newest at the bottom; add an entry whenever
a rule is added or changed because of something that happened.

## Standing decisions (undated)

- **Same-name collapse in the ETF ranking.** GDXU / NUGT were the same
  instrument under two tickers; the 中文名 is the same-instrument key and
  only the strongest shows. NUGT is gone (see 2026-09-23) and no duplicate
  names remain, the rule stays. — `collapse_same_name`, ETF section.
- **Finviz 1000-row cap.** Pages past `r=1000` 403 without Elite and the
  library swallows it; `run_screener` re-queries `order="-ticker"` for the
  tail (≤ 2000 rows covered). — Scheduling section.
- **`_opend_reachable` TCP probe.** Without it the Futu SDK retries forever
  on `ECONNREFUSED`. — Futu section.
- **Futu snapshot quirks.** `get_market_snapshot` has no `change_rate`
  (derive from last / prev_close); `suspension` is the string `"N/A"`, not a
  bool (use `delisting`). — Futu section.
- **pmset keyword.** `wakepoweron` stopped parsing on macOS 26+; use
  `wakeorpoweron`. — Scheduling section.
- **US morning-gap log.** The plist used to write `launchd_morning_gap.log`
  via `StandardOutPath` and it grew unbounded; the plist now runs
  `run_morning_gap.sh`, which rotates daily like the other wrappers.
- **Rank-change marker glyphs.** `🟢↑N` / `🔴↓N`: a colored dot is the only
  way to tint an arrow in plain text. — ETF section.
- **ETF trend page pulls `1y` klines** although the ranking needs 64 bars:
  the page's history is recomputed from the run's klines because the dated
  `.txt` files hold no scores and age out in 5 days.
- **`ETF_rs_new` stem** for the leaders watchlist's daily additions: the
  requested `<date>_etf_rs.txt` differs from the legacy `_ETF_rs` ranking
  stem only by case, and macOS file systems are case-insensitive.
- **12M table timing.** `Fred6725/rs-log` publishes ~01:30–04:00Z while
  us-eod fetches ~02:00Z; that gap is why the freshness wait exists (see
  2026-10-27).

## Dated entries

### 2026-06-16 — GH Actions cron skipped the US 3M RS run
Today's 3M CSV was missing because the scheduled cron never fired (GH cron is
also routinely hours late). → Launchd dispatches both RS workflows itself via
`gh workflow run` 75 min before EOD (`us-rs-3m-trigger` Tue–Sat 08:45,
`hk-rs-trigger` Mon–Fri 18:45, `scripts/trigger_rs_workflow.sh`); GH cron kept
as backup; the workflow's commit step is idempotent so a double fire is
harmless. — Scheduling.

### 2026-08-27 — CRM earnings gap missed by every EOD group
CRM gapped +11.9% on earnings at ADR% 3.74; no EOD Longs group could emit it
under the 4.0 ADR% floor. → EOD ADR% bypass for US Longs `the_setup` +
`earnings_gap` only (`adr_bypass_gap_percent` 10% → `adr_bypass_min_percent`
3.0; gap = latest completed bar's Open vs prior Close, `_daily_gaps`). Every
other EOD ADR% call untouched. — Invariants / morning-gap bullet.

### 2026-09-11 — ACVA: a one-hit screener day lost its only candidate
yfinance 1.x returns a (ticker, field) MultiIndex even for ONE ticker; the
`single` branches indexed `data["Close"]` flat and logged "failed to process
..., dropping". → `_flatten_single_ticker_frame` at every download site. —
yfinance section.

### 2026-09-11 — NVT / WIX: 5% pre-market prints on a few hundred shares
Both passed the pre-market gap gate and opened +1.7% / −0.1%. → Pre-market
volume gate: Futu `pre_volume` ≥ `min_pre_volume_ratio` (0.05) × 20d avg
volume, US negative offsets only. — Invariants / morning-gap bullet.

### 2026-09-12 → 09-29 — flat frames broke every single-ticker k-line fetch
`fetch_hsi_kline_yf` indexed `data["^HSI"]` and KeyError'd on every run
09-12 → 09-25: HK rs-line audit scored 0/37 and the cloud HK RS shipped
without `rs_below_ma`. Found 09-29: `us_rs_3m.fetch_us_klines_yf` had the
same bug on the one-ticker SPY benchmark fetch (`_fetch_spy_kline`, also any
trailing batch of one) → no benchmark since 09-12, US audit 0/118 (no
`rs_us_<date>.txt`), cloud US 3M CSVs with absolute scores and no `rs_*_ma`
columns (percentile order unaffected — the benchmark is a constant offset).
`hk_eod.fetch_hk_klines_yf` had it latently. → All three accept both shapes;
**test fakes must mimic the runtime shape** (the MultiIndex-only fake is why
the single-ticker test stayed green). — yfinance section.

### 2026-09-17 — daily LLM report generation switched off
Post-market CANSLIM step commented out in `run_eod.sh`; pre-market catalyst
`[morning_gap_catalyst].enabled = false`. Code paths kept for manual use. —
`report/CLAUDE.md`.

### 2026-09-22 — Yahoo published the latest daily row with NaN Close
All tickers, for hours after the close. A plain `dropna` evaluated the SMA50
prune one day stale and CF (two closes under SMA50, still falling) was kept.
→ `fill_missing_last_close`: fill such a row from one batch of that day's 1h
bars, warn, fall back to the stale day only when intraday has nothing. The
rest of us-eod still `dropna`s and runs a day stale on such days. —
Invariants / SMA50 prune.

### 2026-09-23 — ETF list trimmed
NUGT and 12 global-market ETFs dropped; no duplicate 中文名 remain (same-name
collapse rule kept). — ETF section.

### 2026-09-28 — OpenD was down and nothing said so
HK EOD, all 9 US morning-gap scans and the next day's runs came back empty
with only WARNINGs; KOD's pre-market gap was never scanned. → OpenD-down ntfy
(`main._alert_if_opend_down`): one probe at the top of us-eod / hk-eod and of
each in-window morning-gap scan, high-priority push once per HKT day
(`state/opend_down_alerted_<date>.txt`). Alert only. — Futu section.

### 2026-09-29 — HKEX:625 emitted 21 days after listing at 4w −35%
An IPO below 64 rows has no RS, so nothing gated it. → Direction gate on both
IPO ladders: `perf_4w > ipo_min_perf_4w` (strict), 0.0 at first, **20.0 since
2026-10-02**; NaN `perf_4w` (exactly 20 rows) is dropped, not kept. — RS
gating.

### 2026-10-02 / 10-03 / 10-10 — thresholds and floors
RS tiers 95 from 10-02, back to 90 on 10-10 (both markets, 12M and 3M).
Floors 10-03: price ≥ $20 / HK$50 (from $10 / HK$20), 20-day avg volume ≥ 1M
in both markets (from 500K). 10-10: every US list back to ≥ 500K (Finviz
`sh_avgvol_o500`, `[settings]` / `[morning_gap]` `min_avg_volume`); HK stays
at 1M. Output before those dates was produced under the other values. —
Header.

### 2026-10 — lone survivors inside a MultiIndex batch
Morning-gap downloads `daily_data` once for the whole discovery list, so a
lone survivor of a later gate is still inside a MultiIndex batch; the
`len(tickers) == 1` heuristic KeyError'd and dropped PENG / DUOL / IONQ and
2162.HK. → Filters decide flat-vs-batch by the frame's shape
(`_is_flat_frame`). — yfinance section.

### 2026-10-14 — non-ETF rows removed from the ETF list
BTCUSD / ETHUSD removed together with QQQ IWM RSP USO GLD SLV EWJ EWY.
`[etf_rs.yf_symbols]` (display ticker → Yahoo symbol, 7-day klines cut to
SPY's trading days) stays as a mechanism with an empty table. — ETF section.

### 2026-10-15 — missed trigger slot ran later with no network
Launchd ran the missed 08:45 `us-rs-3m-trigger` at the next wake, offline, and
us-eod got a stale 3M table. → A pmset wake 1 min before every trigger slot
(08:44 Tue–Sat / 18:44 Mon–Fri, seeded weekly by
`schedule_morning_gap_wakes.py`) and the trigger script retries the dispatch
for ~5 min. — Scheduling.

### 2026-10-15 — ETF 1M companion file
`<date>_ETF_rs_1m.txt` on the 1M weekly ladder; the 3M stem became `ETF_rs_3m`
(legacy `ETF_rs` still read for rank markers, `_LEGACY_STEMS`); 3M + 1M on one
trend page. — ETF section.

### 2026-10-16 — hk-eod ran only when the lid was opened
→ 19:59 wake for hk-eod, and the same 1-min-lead wake for every other launchd
slot (one per US / HK morning-gap slot; `pmset sleep 1` re-sleeps an idle Mac
within a minute and caffeinate only holds during a scan).
`tests/test_schedule_wakes.py` fails when a slot has no wake. — Scheduling.

### 2026-10-17 — us-eod on battery, lid closed (12%)
`pmset -g` had `sleep 1`: the run idle-slept 3 min in and only advanced inside
5–15s DarkWake windows with no Wi-Fi / DNS — 55 min, yfinance empty, Futu
网络中断, the conditional RS group (SPY −1.1% / QQQ −2.5%) lost to
NameResolutionError; the 08:45 trigger ran at a 09:01 DarkWake and slept 2s
after dispatching. → Every scheduled job holds `caffeinate -i -s` (`-i` works
on battery, `-s` is AC-only; a lid-close still sleeps the Mac); EOD /
in-window morning-gap runs probe `pmset -g batt` and ntfy once per HKT day;
the conditional RS group retries network errors (`_retry_on_network_error`);
the trigger script polls `gh run view` for ~20 min and reports "unconfirmed"
instead of a false "failed (conclusion=unknown)". — Scheduling.

### 2026-10-17 — three more from the same throttled morning
- 10:53 a throttled batch left 78/91 master names without data and the daily
  RS top-10 was cut from 13 names → snapshot not written when k-line coverage
  < 50% (`MIN_SNAPSHOT_COVERAGE`); such names reported as `no data fetched`
  (KEPT). — RS gating.
- 43 Leaders names incl. ABBV / CRM were recorded in `ipo_drops` from one
  throttled batch → `_download_daily_batch`: empty-after-retry or
  still-throttled after one 30s cooldown = *degraded*, the group's drops are
  not recorded. — yfinance section.
- QRVO / FBRX were judged by the SMA50 prune on weeks-old closes (the union
  index let a plain `dropna` through) → `drop_stale_series`: a series whose
  last valid close is > 1 session behind the batch is excluded from both
  master checks with a WARNING; delistings (stale ≥ `delisted_after_sessions`
  AND a 6mo recheck shows no newer bar) are the one removal for missing data.
  — Invariants / SMA50 prune.

### 2026-10-20 — Finviz redesign emptied every US list, silently
The screener moved to `/screener/stocks` with rows as SSR JSON; finviz 2.0.0
parsed nothing → NoResults → empty lists. → `finviz_screener.Screener`
replaces `main.Screener` (same surface; 20 rows/page, `r=` paging to the 1000
cap). The new site silently ignores unknown filter codes (`ta_perf_52w150o`
returned the whole market) → `FinvizFilterError`; Leaders 52W is now the
preset `+100%` (`ta_perf` has no custom range). `FinvizLayoutError` when a
page has no rows block. Guard: `_alert_if_finviz_broken` ntfys once per day
when ≥ 5 screens all returned 0 rows or any code was rejected. — Scheduling.

### 2026-10-21 — SPCX dropped twice
90 completed bars → "insufficient daily bars for SMA200" at the morning-gap
trend gate → recent IPOs (< 200 bars, ≥ 50) gate on SMA50 alone, which the
gap bypass can NOT waive; < 50 bars dropped. Then +8% pre-market at ADR% 3.97
→ `[morning_gap].min_adr_percent = 3.0` (was inherited 4.0), which makes the
US morning-gap ADR% bypass a no-op today; EOD stays at 4.0. — Invariants /
morning-gap bullet.

### 2026-10-22 — ETF ranking ran on 10-20 data, all markers `=`
Same NaN-Close day as 2026-09-22, now in the ETF step. →
`us_rs_3m.fetch_us_klines_yf(fill_last_close=True)` for the ETF ranking
(default off; cloud RS jobs unchanged). — ETF section.

### 2026-10-23 — RNG / ELAN and the pre-market volume gate
RNG gapped +12% on earnings with 33k pre-market shares = 2.2% of its 1.49M
average and was dropped at all three pre-market scans. → Big-gap bypass
`pre_volume_bypass_gap_percent` = 10 / `pre_volume_bypass_ratio` = 0.02
(ELAN's 204 shares still fail at 2%). — Invariants / morning-gap bullet.

### 2026-10-23 — Pages page stayed on 10-22 while the local `.txt` was 10-23
The 08:45 trigger slot woke on battery without a network: 10/10 dispatch
attempts failed, and GH cron is unreliable. → `gh workflow run
etf_rs_page.yml` at the end of `run_eod.sh` (soft) is the reliable daily
tick: a finished us-eod proves the network is up. — ETF section.

### 2026-10-27 / 10-28 — stale 12M table and the watchdog
rs-log publishes ~01:30–04:00Z, us-eod fetches ~02:00Z: in 2026-10 the
previous session's table arrived on 14/19 days (CART 10-27 gated at 90 on
10-23 data; the 10-26 table said 94). → `rs_rating._wait_for_fresh_table`:
the CSV's `Price` column is matched against raw yfinance closes of 5 mega caps
(`table_data_session`); behind the latest completed session → re-fetch every
5 min for ≤ 60 min, then gate on the stale table with a WARNING and don't
cache it. On 10-28 the `run_eod.sh` watchdog at 1800s SIGTERMed the run
mid-poll (rs-log published 10:42 HKT) with nothing in the python log →
`EOD_TIMEOUT` 5400s, an `[ERROR] [watchdog]` log line, and the rule that
`_STALE_MAX_WAIT_SEC` and `EOD_TIMEOUT` change together. — RS gating.

### 2026-10-31 — per-ETF strongest-holdings line
`etf_holdings_rs.py`: stockanalysis free tier gives the top 25 holdings
("Showing 25 of 78 holdings"), accepted as the source with yfinance top-10 as
fallback; the page's `/stocks/` link is the US marker because cash / futures
pseudo-symbols (`KRW`, `IXPZ6`) pass a regex while `HKD` / `USD` are real
tickers. AGPXX (a money-market fund in BWET's collateral) ranked first in
BWET's line → 5-letter symbols ending in X excluded. BWET itself was removed
from the list the same day (futures ETF, no stock constituents). Spec:
`docs/superpowers/specs/2026-10-31-etf-holdings-1m-rs-design.md`. — ETF
section.

### 2026-11-01 — leaders watchlist, its prune, and the page line
The trend page's side list shows the `1M强:` line, so the cloud Pages build
runs the full chain (stockanalysis reachable from GH IPs, ~3 min, timeout 25
min). `etf_rs_leaders.py`: cumulative TradingView master of the 1M top-10
ETFs' strongest constituents, pruned on an SMA50 **first-break-low** rule
(latest close below the low of the first session under the line after ≥ 2
sessions below) — deliberately not `sma50_prune`'s prior-close rule. — ETF
section.
