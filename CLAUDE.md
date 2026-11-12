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
uv run main.py --mode us-eod         # US EOD (Longs/Leaders/Shorts/RS/IPO) — 12:00 HKT
uv run main.py --mode hk-eod         # HK EOD (Shorts + Longs/Leaders/RS)   — 20:00 HKT
uv run main.py --mode morning-gap    # US intraday gap scan; clean-exits outside ET window
uv run main.py --mode hk-morning-gap # HK intraday gap scan (post-open only)
uv run main.py --mode report --market {us,hk}  # CANSLIM report (HTML) from today's .txt files; only US is scheduled
uv run main.py --mode etf-rs         # ETF 3M + 1M ranking files, holdings line, leaders watchlist + prune, trend page (also at the end of us-eod)
uv run main.py --mode rs-line-audit --market {us,hk,both} --dry-run  # audit the master; without --dry-run it prompts to prune
uv run pytest tests/ -v             # `uv run python -m pytest` works too
```

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
  alphabetically mid-run and the pruners preserve order. Master / seen-set
  writes go through `_atomic_write_lines` (temp file + `os.replace`; the ETF
  leaders master likewise) so a watchdog SIGTERM never leaves a truncated
  master. **US Longs + Repeat files are written right after the Longs loop**,
  before the Leaders / Shorts / RS screens — a later Finviz outage that runs
  into the watchdog must not cost them (→ decisions 2026-11-12).
- **SMA50 prune + EMA20 first-break list (US master)** run at the top of
  every us-eod, before the master is loaded (`main._run_us_master_lists`,
  one shared 6mo fetch, each step independently soft). Details live in
  `.claude/rules/master-lists.md` (path-scoped to `sma50_prune.py` /
  `ema20_break.py`). The parts that must hold everywhere: the prune backs
  the master up as `.bak.<stamp>` first; missing, short-history and
  **stale series are KEPT** (the confirmed-delisting rule is the one
  removal for missing data); EMA20 never touches the master.
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
  not what's written. That seen file is written **after** the ntfy push and
  only when it went out (`_morning_gap_classify(persist=False)` →
  `notify_morning_gap` → `_morning_gap_mark_seen`): a failed POST leaves the
  names fresh for the next scan, and the catalyst spawn waits with it. Spec:
  `docs/superpowers/specs/2026-08-13-morning-gap-live-price-trend-gate-design.md`.
- **Report** — daily LLM generation is OFF (→ decisions 2026-09-17); wiring
  and evidence-prefetch details live in `report/CLAUDE.md`.
- **Catalyst report (pre-market)** — currently OFF
  (`[morning_gap_catalyst].enabled = false`, 2026-09-17) — is a **detached subprocess** spawned
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
  + cache). The cloud 3M job passes `wait_for_fresh=False` (it only needs
  the universe; the poll ate its 60-min GH timeout → decisions 2026-11-12).
  **The `run_eod.sh` watchdog (`EOD_TIMEOUT`, 5400s) must stay
  above this 60-min wait plus a full run** and logs an `[ERROR] [watchdog]`
  line when it fires; change `_STALE_MAX_WAIT_SEC` and `EOD_TIMEOUT`
  together (→ decisions 2026-10-27).
- **RS-line trend + `rs-line-audit` + daily strongest-RS snapshot:**
  details in `.claude/rules/rs-line.md` (path-scoped to `rs_line.py` /
  `rs_line_audit.py`). The parts that must hold everywhere: EOD only
  *annotates* (no `.txt` / dedup effect, columns computed cloud-side);
  **pruning the master stays manual** — the audit chained at the end of
  `run_eod.sh` / `run_hk_eod.sh` runs with `--dry-run`, which is
  load-bearing (no prompt, no prune, no backup).
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

## Scheduling (launchd, HKT)

- US EOD Tue-Sat 12:00 (→ decisions 2026-11-07); HK EOD Mon-Fri 20:00; morning-gap modes fire many entries
  and **self-validate their window, clean-exiting outside it — do not add a hard
  error path** (missed wakes are silent by design). The window is checked
  once **before** the network gate (`_morning_gap_in_window`) and again in
  the run path after it: an out-of-window fire exits in < 1s instead of
  paying the 20s settle — or, offline, the 300s wait plus a false
  "scan skipped" ntfy. The network gate probes only the hosts the mode uses
  (`_network_gate_hosts`: morning-gap = Yahoo; hk-eod = GitHub raw + Yahoo;
  us-eod adds finviz.com).
- Every scheduled job holds a `caffeinate -i -s` assertion. Do not remove it.
- Keep every Finviz Screener call inside `run_screener`.
- **The ntfy topic is not in git** (public repo): `NTFY_TOPIC` in `.env`,
  resolved by `notify._topic` and read by the two ntfy shell scripts;
  `config.toml` keeps `ntfy_topic = ""`. Never commit a real topic
  (→ decisions 2026-11-12).

The launchd / pmset wake, Finviz and Futu rules live in
`.claude/rules/{scheduling,finviz,futu}.md` (path-scoped: they load when you
touch the matching files).
