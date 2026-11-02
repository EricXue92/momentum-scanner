# CLAUDE.md

Thresholds and group configs live in `config.toml`. This file covers only the
non-obvious invariants that are easy to break. The incidents and decisions
behind them are in `docs/decisions.md`; a `→ decisions <date>` reference
points there — read it before changing the rule it is attached to.

Current floors (long-side, IPO and morning-gap; Shorts share the US ones):
**US** price ≥ $20 (Finviz `sh_price_o20`) and 20-day avg volume ≥ 500K
(`sh_avgvol_o500`, `[settings]` / `[morning_gap]` `min_avg_volume`);
**HK** price ≥ HK$50 and avg volume ≥ 1M (`[hk_settings]` /
`[hk_morning_gap]`). The code-side fallbacks for unset `min_price` /
`min_avg_volume` match these values (history → decisions 2026-10-02).

## Commands

```bash
uv sync
uv run main.py --mode us-eod         # US EOD (Longs/Leaders/Shorts/RS/IPO) — 10:00 HKT
uv run main.py --mode hk-eod         # HK EOD (Shorts + Longs/Leaders/RS)   — 20:00 HKT
uv run main.py --mode morning-gap    # US intraday gap scan; clean-exits outside ET window
uv run main.py --mode hk-morning-gap # HK intraday gap scan (post-open only)
uv run main.py --mode report --market {us,hk}  # CANSLIM report (HTML) from today's .txt files; only US is scheduled
uv run main.py --mode etf-rs         # ETF 3M + 1M ranking files, holdings line, leaders watchlist + prune, trend page (also at the end of us-eod)
uv run main.py --mode rs-line-audit --market {us,hk,both} --dry-run  # audit the master; without --dry-run it prompts to prune
uv run pytest tests/ -v             # `uv run python -m pytest` works too
```

## Module map

- `main.py` — US pipeline + CLI (`--mode`), Finviz discovery, Longs/Leaders/
  RS/Shorts filters, dedup masters, yfinance download helpers
  (`_yf_download_with_retry`, `_download_daily_batch`), US **and HK**
  morning-gap scans (`run_morning_gap` / `run_hk_morning_gap`), alerts.
  `hk_eod.py` — the HK EOD pipeline (universe, Shorts, Longs/Leaders/RS,
  IPO, HK k-line fetch).
- Universe RS: `rs_rating.py` (12M table fetch + freshness wait),
  `us_rs_3m.py` (3M scorer, k-line fetcher, cloud script's core),
  `hk_rs.py` / `hk_metrics.py` (HK RS + metrics cloud fetch with local
  fallback), `rs_line.py` + `rs_line_audit.py` (RS-line trend, audit mode,
  daily top-10 snapshot). Cloud side: `scripts/compute_us_rs_3m_cloud.py`,
  `scripts/compute_hk_rs_cloud.py`.
- Master maintenance: `sma50_prune.py` (SMA50 prune + delisting removal,
  shared close fetch), `ema20_break.py` (first-break list), `us_ipo.py`
  (US IPO ladder).
- ETF: `etf_rs.py` (ranking files) → `etf_holdings_rs.py` (per-ETF
  constituents + 1M top-5 line) → `etf_rs_leaders.py` (cumulative
  watchlist + its prune) → `etf_rs_chart.py` (trend page);
  `scripts/build_etf_rs_page.py` is the Pages build.
- Integrations: `finviz_screener.py` (post-redesign screener),
  `finviz_timeout.py` (socket timeouts for the `finviz` lib),
  `futu_sync.py`, `tv_sync.py`, `notify.py` (ntfy), `power.py` (battery
  probe), `net_ready.py` (wait for network), `cleanup.py` (retention
  rules). `report/` — CANSLIM + catalyst reports (own `CLAUDE.md`).
- `scripts/run_*.sh` are the launchd entry points (watchdog, caffeinate,
  log rotation, soft trailing steps); `scripts/*.plist` the schedules;
  `scripts/schedule_morning_gap_wakes.py` seeds the pmset wakes;
  `scripts/trigger_rs_workflow.sh` dispatches the GH RS workflows.

## Invariants (don't break these)

- **Dated files only**, no "latest" copy. `write_watchlist` skips empty lists
  (no 0-byte artifacts) and deliberately does NOT delete an existing file — a
  manual EOD rerun dedups today's names to 0 and must not wipe the earlier
  run's output. The log is the record of a 0-result scan. Morning-gap files
  are per-scan (`_gap_scan_stem`: `MorningGapPre20`/`MorningGap5`/
  `HKMorningGap10`), one snapshot per offset, absent when that scan found
  nothing. The two undated exceptions are the ETF trend page and the ETF
  leaders master (see the ETF section).
- **Webull mirror is newline-separated** (its file upload truncates comma lists);
  TV `.txt` stays comma-separated.
- **Dedup, layered:** (1) within Longs, earlier `config.toml` entry wins; (2) Longs
  union deduped against Leaders (`Longs > Leaders`); (3) cross-day master
  `output/state/eod_seen_{US,HK,IPO,HKIPO}.txt` (`_dedup_seen`) — daily output = within-day
  survivors minus master, survivors append. Markets independent; IPO/HKIPO have
  own masters. **RS and Shorts are excluded from all dedup** (re-detect by design).
  Reset masters only by deleting the file. **US and HK master line order = 3M RS,
  strongest first** (`_sort_seen_by_rs_3m`, re-sorted every us-eod / hk-eod
  after the last `_dedup_seen`; no-score tickers last; no table → order left
  as is). US ranks by `raw_score`; the HK cloud table has percentiles only,
  so HK ties stay alphabetical, and HK entries are mapped `HKEX:522` →
  `HK.00522` for the lookup. IPO/HKIPO masters stay alphabetical.
  Order is cosmetic — `_load_seen` reads a set; `_persist_seen` still writes
  alphabetically mid-run and the pruners preserve order.
- **SMA50 prune of the US master** (`sma50_prune.py`, `[sma50_prune]`), at
  the top of every us-eod before the master is loaded, backs the master up
  as `.bak.<stamp>` first (so does the manual `rs-line-audit` prune): close
  below SMA50 for `consecutive_days` (2) completed days AND latest close
  below the prior day's close (a rebound under the line is kept) → removed,
  can re-qualify later. Soft-fail: total yfinance failure skips the prune;
  missing/short-history tickers are KEPT. **Stale series are KEPT**
  (`drop_stale_series` in `_fetch_daily_closes`, so EMA20 shares it): last
  valid close > 1 session behind the batch (session = a date ≥ half the
  tickers have a close) → delisted/halted, excluded from both checks with a
  WARNING; a 1-session lag is Yahoo lag → intraday fill (→ decisions
  2026-10-17). **Delistings are the one removal for missing data:** stale ≥
  `delisted_after_sessions` (10; unset/0 = off) AND a separate 6mo
  re-download (`_confirm_no_new_bars`) shows no newer bar → removed in the
  same backup/prune, logged, NOT written to `SMA50Pruned.txt`; a failed
  recheck removes nothing. Stale info rides on the shared closes as
  `MasterCloses.stale`; a plain dict (test fakes) means no delistings. US
  only. The day's removed names go to `output/TV/US/<date>_SMA50Pruned.txt`
  (comma-sep; absent when nothing was pruned; a same-day rerun **merges**
  into it, since the rerun no longer sees names the first run removed; no
  Webull mirror / Futu / TV sync; `TV/US` 5-day rule; write failure is
  soft). **Trailing-NaN Close fill** (`fill_missing_last_close`): Yahoo
  sometimes publishes the latest session's daily row with OHLV but NaN
  Close for hours; the prune fills such a row from one batch of that day's
  1h bars (last bar's close), warns, and falls back to the stale day only
  when intraday has nothing either (→ decisions 2026-09-22). The ETF RS
  ranking gets the same fill (`us_rs_3m.fetch_us_klines_yf(fill_last_close=True)`,
  default off — the cloud RS jobs are unchanged; → decisions 2026-10-22).
  The rest of us-eod still `dropna`s and runs a day stale on such days.
- **EMA20 first-break list (US, read-only):** `ema20_break.py`
  (`[ema20_break]`) runs right before the SMA50 prune over the same master
  (`main._run_us_master_lists` fetches the master's 6mo closes ONCE and
  passes `closes=` to both; each step is still independently soft) —
  latest close < EMA20 AND prior close ≥ prior EMA20 (cross-down day only) →
  `output/TV/US/<date>_EMA20Break.txt` (comma-sep). Never touches the master.
  Empty → no file; same-day rerun overwrites; keeps only the newest
  `keep_files` (3) lists itself (stricter than the `TV/US` 5-day rule).
  Reuses `sma50_prune._fetch_daily_closes` (incl. the NaN-Close fill). Soft-fail.
- **EOD Repeat (US):** tickers the master would drop that still fired an
  _event_ Longs group today are collected by `_repeat_hits` **before**
  `_dedup_seen` (which mutates `us_seen`) and written to
  `<date>_Repeat.txt`. Read-only w.r.t. the master — no write-back, and each
  group's own `.txt` keeps its "new names only" meaning. Config `[eod_repeat]`
  (`keys` = the 5 event groups incl. `the_setup`; `new_high_52w`/Leaders deliberately excluded as
  persistent states). Futu/TV mappings ship commented out (groups must be
  hand-created), so sync is a no-op until you enable them.
- **Cleanup** (`cleanup_old_outputs`) is driven by an explicit regex rule table
  (`_RETENTION_RULES`, not globs) and soft-fails; **never touches** the
  `eod_seen_*.txt` masters, `ntfy_last_seen.txt`, `edgar_cache/`, logs, or
  any undated file. The pruners' `eod_seen_<MKT>.txt.bak.<stamp>` backups
  age out after 2 days.
- **HK data-day rule:** only the 20:00 HKT slot uses today's close; earlier runs
  trim today's incomplete bar (and skip the conditional HSI-trigger RS group).
  Weekends map to the previous Friday (`hk_effective_data_day`): Friday's close
  is settled, so weekend reruns fetch Friday's cloud metrics/RS CSVs at full
  coverage, don't trim, and don't skip the HSI-trigger group. Weekday holidays
  have no calendar — they 404 into the yfinance fallback as before.
- **Morning-gap trend gate uses the live price:** `_filter_sma_trend` compares
  the pre-market print (US negative offsets) or `last_price` (US positive
  offsets, all HK) against SMA50/SMA200 — **not** the previous close. The
  averages themselves still come from `_trim_today`-trimmed completed bars;
  only the comparison basis moved (`sma_use_live_price = false` restores the
  old basis). `sma_bypass_gap_percent` (`[morning_gap]` US /
  `[hk_morning_gap]` HK) waives **SMA50 only** for big gappers — SMA200 is
  never waived. **Recent IPOs** (< 200 completed bars, ≥ 50) skip SMA200 and
  gate on SMA50 alone, which then can NOT be waived by the gap bypass (it is
  the only floor); < 50 bars → dropped (→ decisions 2026-10-21).
  **ADR% floor relaxes for big gappers**: gap ≥ `adr_bypass_gap_percent` →
  floor drops from `min_adr_percent` to `adr_bypass_min_percent` (US: 10% →
  3.0; HK wired but off — its base is already 3.0). US morning-gap overrides
  `[morning_gap].min_adr_percent = 3.0`, so the US bypass is currently a
  no-op; EOD stays at 4.0 (→ decisions 2026-10-21). **EOD carries the same
  bypass for US Longs `the_setup` + `earnings_gap` only** (per-group
  `[[longs]]` keys `adr_bypass_gap_percent` / `adr_bypass_min_percent`, 10%
  → 3.0; gap = latest completed daily bar's Open vs prior Close,
  `_daily_gaps`; → decisions 2026-08-27). Every other EOD ADR% call (other
  Longs groups, Leaders, RS, Shorts) is untouched. **Pre-market volume
  gate** (US negative offsets only, `_filter_pre_market_volume`,
  `[morning_gap].min_pre_volume_ratio` = 0.05, 0 = off): Futu `pre_volume`
  (carried on `GapQuote.pre_volume`) must be ≥ ratio × 20d avg volume (→
  decisions 2026-09-11). Runs after the 20d avg-volume gate on the same
  yfinance frame; a quote without `pre_volume` is kept. Big-gap bypass
  `pre_volume_bypass_gap_percent` = 10 / `pre_volume_bypass_ratio` = 0.02
  (same shape as the ADR% knob; gap_percent 0 = off, ratio 0 = waived; →
  decisions 2026-10-23). Consequence, and it is intended: the gate drifts
  intraday, but each scan writes its own per-offset `.txt` (not cumulative,
  and morning-gap never touches `eod_seen_*`), so drift only affects the
  one-time ntfy/catalyst gate (`morning_gap_seen_{pre,post}_<date>.txt`),
  not what's written. Spec:
  `docs/superpowers/specs/2026-08-13-morning-gap-live-price-trend-gate-design.md`.
- **Report** — daily LLM generation is OFF (→ decisions 2026-09-17); wiring
  and evidence-prefetch details live in `report/CLAUDE.md`.
- **Catalyst report (pre-market)** is a **detached subprocess** spawned
  from the morning-gap path; it MUST NOT block the morning-gap process.
  Always uses DeepSeek + Tavily regardless of `[report] backend`. Reads
  only the JSON snapshot sidecar — MUST NOT call Futu / yfinance. Output:
  `output/Reports/PreMarket/<date>_us_premarket.html`, re-rendered across the
  pre-market scans (-20/-10/-5) whenever one finds fresh tickers. The
  append-across-scans source is the markdown accumulator
  `output/state/premarket_catalyst_<date>.md` (internal state, 2-day
  cleanup) — do not write `.md` under `Reports/`.

## RS gating

Percentile tables are computed daily on **GitHub Actions** and published as CSVs;
the local pipeline only fetches them. US: `Fred6725/rs-log` (12M, vs SPY) +
`data/us_rs_3m/` (3M). HK: `data/hk_rs/` (12M+3M, vs HSI). Thresholds: 12M
tiers 90, 3M tiers 90 in both markets (Leaders / conditional RS / Shorts /
IPO ≥ 64 rows; US `[settings].min_rs_percentile_3m`, HK
`[hk_settings].min_rs_percentile_longs_3m` + `[hk_shorts].min_rs_percentile_3m`;
the code-side fallback when a key is unset is 90; → decisions 2026-10-02);
set 0 to disable a tier. The HK metrics frame is also cloud-published
(`data/hk_metrics/`, same workflow) and fetched locally via
`hk_metrics.build_hk_metrics_cloud`, so discovery runs on the full universe
on the happy path; a cloud miss falls back to the local (throttle-prone)
k-line fetch.

- Event groups gate on 12M only: US Longs 6 组 (per-group `min_rs_percentile`
  override, `_longs_rs_threshold`; `the_setup` = 0 → no RS gate); HK EarningsGap/HighVolume/GapUp
  (per-group wiring in `run_hk_eod`, mirrors US). US Leaders / conditional RS /
  Shorts are structurally 12M+3M double-gated; config.toml sets their 12M keys
  to 0 so they currently gate on 3M only. 12M keys: `min_rs_percentile`
  (Leaders, defaults 0) and `min_rs_percentile_rs` / `_shorts` — the latter two
  default to `min_rs_percentile_longs` when **unset**, so deleting the key
  re-enables the 12M tier at the Longs threshold; keep them explicitly 0 to stay off. Effective
  3M-only likewise for HK Leaders + conditional RS, and HK Shorts
  (`[hk_shorts].min_rs_percentile_3m`, defaults to
  `min_rs_percentile_longs_3m`; applied as a universe pre-filter before the
  yfinance batch in `filter_hk_shorts`). The 12M∩3M double gate is thus
  currently nowhere active (all knobs remain independently tunable).
- Not gated: Morning Gap. IPO: conditional 3M only (≥ 64-day history).
  **An IPO below 64 rows has no RS at all**, so both ladders carry a
  direction gate instead: `perf_4w > ipo_min_perf_4w` (strict;
  `drops['perf_4w']`; key in `[hk_settings]` for HK, `[settings]` for US;
  both 20.0; → decisions 2026-09-29). NaN `perf_4w` (exactly 20 rows) is
  **dropped, not kept**: the name isn't in the master yet, and keeping it
  would let every IPO through ungated on its first eligible day. HK
  re-evaluates it at 21 rows; a US name only comes back if it passes a
  Finviz long-side screener again. Key unset = gate off.
- **Do NOT make fetch failure hard-fail:** walk back ≤ 3 days of stale cache, then
  pass through (no gate) with a warning. Tickers **missing** from the table are
  KEPT, not dropped.
- **12M table freshness** (`rs_rating._wait_for_fresh_table`): the CSV has
  no date, so its `Price` column is matched against raw yfinance closes of 5
  mega caps (`table_data_session`). Behind the latest completed session (a
  partial bar during US hours doesn't count) → re-fetch every 5 min for ≤
  60 min, then gate on the stale table with a WARNING and do **not** cache
  it as `rs_rating_<today>.csv` (a rerun re-fetches). Any failure of the
  check itself (no Price column, yfinance down) → skipped, old behavior (use
  + cache). **The `run_eod.sh` watchdog (`EOD_TIMEOUT`, 5400s) must stay
  above this 60-min wait plus a full run** and logs an `[ERROR] [watchdog]`
  line when it fires; change `_STALE_MAX_WAIT_SEC` and `EOD_TIMEOUT`
  together (→ decisions 2026-10-27).
- **RS-line trend (annotate in EOD; manual prune via audit mode):** cloud scripts
  publish `rs_below_ma` / `rs_days_below_ma` / `rs_frac_below_ma` (TraderLion-style
  RS line = price/index vs its own EMA21) as extra columns in
  `data/{us_rs_3m,hk_rs}/<date>.csv`. The EOD log annotates long-side survivors
  whose RS line is persistently below its MA; EOD itself has **no `.txt`/dedup
  effect**. Computed cloud-side only (local never refetches klines). Config:
  `[rs_line]`. Spec:
  `docs/superpowers/specs/2026-05-27-rs-line-trend-filter-design.md`.
- **`uv run main.py --mode rs-line-audit [--market us|hk|both] [--dry-run|--yes]`**
  scores the cross-day master, writes
  `output/rs-audit/rs_line_audit_<MKT>_<date>{,_drop,_keep_ranked}.txt`, prints the
  report, then **prompts y/N** to prune the drops from
  `output/state/eod_seen_{US,HK}.txt` so they can re-qualify on a future EOD run.
  Confirmed prunes back the master up first as `eod_seen_<MKT>.txt.bak.<stamp>`.
  `--yes` skips the prompt (auto-prune, legacy non-interactive behavior);
  `--dry-run` writes the report + sidecars but does NOT touch the master
  (no prompt, no backup). **Pruning** stays manual/operator-triggered, but a
  `--dry-run` audit is chained daily as a soft step at the end of
  `run_eod.sh` / `run_hk_eod.sh` (after the report) to produce the
  strongest-RS snapshot below.
- **Daily strongest-RS snapshot:** every audit run also writes the top
  `[rs_line].top_n` (10) of `keep_ranked` (unknowns excluded, never padded) to
  `output/TV/US/rs_us_<date>.txt` / `output/hk_rs_<date>.txt` — dated, skipped
  when empty, overwritten on same-day rerun (ranking snapshot, no dedup
  semantics; not Webull-mirrored, no eod_seen effect). **Not written when
  k-line coverage of the master < 50%** (`MIN_SNAPSHOT_COVERAGE`; an earlier
  same-day snapshot stays; → decisions 2026-10-17). The report lists such
  names as `no data fetched` (KEPT), separate from `insufficient history`.
  Cleanup: snapshots 4-day window; audit report + sidecars (`rs-audit/`)
  5-day window.

## ETF RS ranking

Rules for `etf_rs.py` / `etf_holdings_rs.py` / `etf_rs_leaders.py` /
`etf_rs_chart.py`, the Pages build and their tests live in
`.claude/rules/etf-rs.md` (path-scoped: it loads when you touch those
files). The step is a soft side-step at the end of us-eod (`--mode etf-rs`
alone); its two undated outputs (`output/Reports/ETF/etf_rs_trend.html`,
`output/TV/US/ETF_rs_leaders.txt`) are the exceptions to "dated files only"
and must never be aged out.

## yfinance frames and batches

- **Single-ticker frames:** `group_by="ticker"` returns a (ticker, field)
  MultiIndex even for ONE ticker (yfinance 1.x); every `single` branch
  indexes `data["Close"]` flat, so each download site wraps the result in
  `_flatten_single_ticker_frame`. Corollary: anything going through
  `_yf_download_with_retry` with ONE ticker gets a flat frame back —
  `fetch_hsi_kline_yf`, `us_rs_3m.fetch_us_klines_yf` (`_fetch_spy_kline`,
  also any trailing batch of one) and `hk_eod.fetch_hk_klines_yf` accept
  both shapes. **Test fakes must mimic the runtime shape** (flat for one
  ticker) — a MultiIndex-only fake keeps a broken single-ticker path green
  (→ decisions 2026-09-11, 2026-09-12).
- **Filters decide flat-vs-batch by the frame's shape** (`_is_flat_frame`),
  never by `len(tickers) == 1`: morning-gap downloads `daily_data` once for
  the whole discovery list, so a lone survivor of a later gate is still
  inside a MultiIndex batch (→ decisions 2026-10).
- **Throttled batches:** EOD Longs/Leaders batch downloads go through
  `_download_daily_batch`: empty-after-retry or still-throttled after one
  30s cooldown = *degraded* → the group's drops are NOT recorded in
  `ipo_drops` (→ decisions 2026-10-17). `rs_rating.fetch_rs_table` also
  catches `http.client.HTTPException` / `UnicodeDecodeError` (neither is an
  OSError; they used to abort us-eod before any `.txt`).
  `_yf_download_with_retry` waits `_YF_RETRY_DELAYS` (5s, 20s) between its 3
  attempts — an empty batch is Yahoo's throttle and its window is ≥ 30s. A
  Longs group downloads its 2mo batch once (`_longs_group_batch`, `batch=`
  on both filters). HK Shorts goes through `_download_daily_batch(threads=True)`
  too.
- **K-line fetchers recover once:** `us_rs_3m.fetch_us_klines_yf`
  (`recover=True`) and `hk_eod.fetch_hk_klines_yf` re-download, after a 30s
  cooldown, the tickers a failed or throttled batch left with NO data (a
  short-but-real history, e.g. a fresh IPO, is not retried) — one pass, no
  loop, and skipped when the first pass already took > 25 min (US) / 12 min
  (HK) (`_RECOVERY_MAX_ELAPSED_SEC`: a dead Yahoo must not push the cloud
  job past its GH timeout before `return 1`). IPO candidates still without
  k-lines are counted as `drops['no_data']` (warned; re-evaluated next
  run), not silently lost.

## Futu sync

Soft side-effect — logs a warning on failure, never raises. No-op when disabled /
unmapped / empty tickers (an empty `.txt` must not wipe the existing group).
Diff-based (one DEL + one ADD max). Append-only groups skip DEL and accumulate.
Ticker format: US `AAPL`→`US.AAPL`, HK `522`→`HK.00522` (5-digit). The 18 custom
groups must be created by hand in the client (API can't create groups).

**Gotchas (do not regress):**

- TCP probe `_opend_reachable` (1.5s) before `OpenQuoteContext` — without it the
  SDK retries forever on `ECONNREFUSED`. **Do not remove.**
- **OpenD-down ntfy** (`main._alert_if_opend_down` → `notify.notify_opend_down`):
  every Futu consumer soft-fails, so a dead OpenD would otherwise leave only
  WARNINGs while scans come back empty (→ decisions 2026-09-28). One probe
  at the top of us-eod / hk-eod and of each **in-window** morning-gap scan
  (out-of-window fires stay silent); high-priority push **once per HKT day**
  across all modes (`state/opend_down_alerted_<date>.txt`, written only after
  a successful POST so a failed push retries on the next scan; 2-day cleanup).
  Alert only — it never changes the run.
- `get_market_snapshot` has no `change_rate` — derive from `(last_price -
prev_close_price) / prev_close_price`. `pre_/after_change_rate` do exist.
- `suspension` is a string (`"N/A"`), not bool — use bool `delisting` instead.
- HK long-side data fetch hard-depends on OpenD (Futu _sync_ being soft-fail does
  not make the _fetch_ soft).

## Scheduling (launchd, HKT)

- US EOD Tue-Sat 10:00; HK EOD Mon-Fri 20:00; morning-gap modes fire many entries
  and **self-validate their window, clean-exiting outside it — do not add a hard
  error path** (missed wakes are silent by design). The window is checked
  once **before** the network gate (`_morning_gap_in_window`) and again in
  the run path after it: an out-of-window fire exits in < 1s instead of
  paying the 20s settle — or, offline, the 300s wait plus a false
  "scan skipped" ntfy.
- pmset wake keyword is **`wakeorpoweron`** on macOS 26+ (`wakepoweron` no longer
  parses). US EOD uses mode `us-eod` not `eod` (HK bar is incomplete at 10:00 HKT).
- **The schedule assumes AC power; every scheduled job holds a `caffeinate
  -i -s` assertion** (`run_eod.sh` / `run_hk_eod.sh` / `trigger_rs_workflow.sh`
  source `scripts/lib/hold_awake.sh` → `caffeinate -i -s -w $$ &`;
  `run_morning_gap.sh` / `run_hk_morning_gap.sh` `exec caffeinate -i -s uv
  run …`; the detached catalyst subprocess prepends its own
  `/usr/bin/caffeinate -i -s`, `main._CAFFEINATE_BIN`). Do not remove. `-i`
  works on battery, `-s` is AC-only; a lid-close by the user still sleeps
  the Mac (→ decisions 2026-10-17). Belt-and-suspenders: EOD/in-window
  morning-gap runs probe `pmset -g batt` (`power.py`,
  `main._alert_if_on_battery`) and ntfy once per HKT day
  (`state/on_battery_alerted_<date>.txt`, 2-day cleanup); the conditional
  RS group retries network errors (`_retry_on_network_error`: wait for
  finviz reachable ≤ 180s, 3 attempts; non-network errors propagate); and
  the trigger script polls `gh run view` status/conclusion for up to ~20 min
  before alerting — `completed`+non-success → "failed", never confirmed →
  "unconfirmed" (default priority, exit 2). `TRIGGER_*` env overrides exist
  only for `tests/test_trigger_rs_workflow.py`. The US morning-gap plist
  runs `run_morning_gap.sh` (log rotated daily like the other wrappers).
- **RS workflow self-trigger:** GH Actions' scheduled cron is unreliable
  (delayed hours, sometimes skipped; → decisions 2026-06-16). Launchd
  dispatches the workflow via `gh workflow run` 75 min before EOD:
  `us-rs-3m-trigger` (Tue-Sat 08:45) and `hk-rs-trigger` (Mon-Fri 18:45) →
  `scripts/trigger_rs_workflow.sh`. The GH-side cron is kept as
  belt-and-suspenders; the workflow's commit step is idempotent (`git diff
  --staged --quiet → exit 0`) so a double-fire is harmless. Failures ntfy via
  the morning-gap topic. Each trigger has a pmset wake 1 min before it
  (08:44 Tue-Sat / 18:44 Mon-Fri, seeded weekly by
  `schedule_morning_gap_wakes.py` with the morning-gap wakes) and the script
  retries the dispatch ~5 min (→ decisions 2026-10-15). Every other launchd
  slot gets the same 1-min-lead wake (Mon-Fri): one per US / HK morning-gap
  slot (`US_GAP_SLOTS` / `HK_GAP_SLOTS` — `pmset sleep 1` re-sleeps an idle
  Mac within a minute and caffeinate only holds during a scan, so one wake
  covers one slot; the 20:59 / 21:59 11-min-lead wakes stay) and 19:59 for
  hk-eod (→ decisions 2026-10-16). `tests/test_schedule_wakes.py` reads the
  plists and fails when a slot has no wake — edit the slot lists together
  with the plists.
- **Finviz 1000-row cap:** pages past r=1000 403 without Elite and the
  library swallows it. `run_screener` compares against the page's total and
  re-queries `order="-ticker"` for the tail (covers ≤ 2000 rows; beyond that
  it WARNs "still truncated"). Keep every Screener call inside `run_screener`
  — it (and `check_market_down`) also retries transient network errors via
  `_retry_on_network_error` (DNS/reset/5xx/finviz timeout/"Too many
  requests"; a Finviz 4xx is deterministic and NOT retried, except 429).
  Nothing else needs its own wrapper.
- **Finviz screener is `finviz_screener.Screener`** (not the library's; →
  decisions 2026-10-20): the site serves `/screener/stocks` with rows as SSR
  JSON (same `.data` / `._total_rows` / NoResults surface; 20 rows/page,
  `r=` paging to the 1000 cap; Market Cap from the overview row, `table`
  ignored). A page with no rows block raises `FinvizLayoutError`; a filter
  code the page does not define raises `FinvizFilterError` — the site
  **silently ignores** unknown codes (Leaders 52W is the preset `+100%`;
  `ta_perf` has no custom range). `ind_stocksonly` is implied by the path
  and whitelisted. `finviz.get_stock` (market check) still uses the library.
  **Guard:** `run_screener` counts into `_finviz_tally` (reset at us-eod
  start); after the RS section `_alert_if_finviz_broken` logs ERROR + ntfys
  (once/day, `state/finviz_broken_alerted_<date>.txt`, 2-day cleanup) when
  ≥ 5 screens all returned 0 rows or any code was rejected. Alert only.
