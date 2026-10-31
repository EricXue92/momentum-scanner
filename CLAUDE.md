# CLAUDE.md

Thresholds and group configs live in `config.toml`. This file covers only the
non-obvious invariants that are easy to break.

Baseline floors since 2026-10-03 (US/HK long-side, IPO and morning-gap;
Shorts unchanged): price ≥ $20 (Finviz `sh_price_o20`; TheSetup was $10)
/ ≥ HK$50 (`[hk_settings]` + `[hk_morning_gap]` `min_price`; was HK$20),
and 20-day avg volume ≥ 1M shares in both markets (Finviz
`sh_avgvol_o1000`, `min_avg_volume = 1_000_000`; was 500K). The code-side
fallbacks for unset `min_price` / `min_avg_volume` match these values.
Output older than that date was produced under the looser floors.
**Avg-volume exception since 2026-10-10:** every US list is back at ≥ 500K
— Longs, Leaders, conditional RS, Shorts (Finviz `sh_avgvol_o500`), the IPO
ladder (`[settings].min_avg_volume`) and morning-gap
(`[morning_gap].min_avg_volume`), with the US code-side fallbacks at 500K
too. HK stays at 1M.

## Commands

```bash
uv sync
uv run main.py --mode us-eod         # US EOD (Longs/Leaders/Shorts/RS/IPO) — 10:00 HKT
uv run main.py --mode hk-eod         # HK EOD (Shorts + Longs/Leaders/RS)   — 20:00 HKT
uv run main.py --mode morning-gap    # US intraday gap scan; clean-exits outside ET window
uv run main.py --mode hk-morning-gap # HK intraday gap scan (post-open only)
uv run main.py --mode report --market {us,hk}  # CANSLIM report (HTML) from today's .txt files; only US is scheduled
uv run main.py --mode etf-rs         # ETF 3M RS ranking + trend page only (also runs at the end of us-eod)
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
  nothing.
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
  alphabetically mid-run and the pruners preserve order. Two pruners may shrink the US master
  (both back up as `.bak.<stamp>` first): manual `rs-line-audit`, and the
  automatic **SMA50 prune** (`sma50_prune.py`, `[sma50_prune]` config) that runs
  at the top of every us-eod before the master is loaded — close below SMA50 for
  `consecutive_days` (2) completed days AND latest close below the prior day's
  close (still declining; a rebound under the line is kept) → removed, can
  re-qualify later.
  Soft-fail: total yfinance failure skips the prune; missing/short-history
  tickers are KEPT. **Stale series are KEPT too** (`drop_stale_series`, in
  `_fetch_daily_closes`, so EMA20 shares it): last valid close > 1 session
  behind the batch (session = a date ≥ half the tickers have a close) →
  delisted/halted, excluded from both checks with a WARNING — the union
  index used to let a plain `dropna` judge QRVO/FBRX on weeks-old closes
  (2026-10-17). A 1-session lag is still Yahoo lag → intraday fill.
  **Delistings are the one removal for missing data:** stale ≥
  `delisted_after_sessions` (10; unset/0 = off) AND a separate 6mo
  re-download (`_confirm_no_new_bars`) shows no newer bar → removed from the
  master in the same backup/prune, logged, NOT written to `SMA50Pruned.txt`.
  A failed recheck removes nothing. Stale info rides on the shared closes as
  `MasterCloses.stale`; a plain dict (test fakes) means no delistings. US only. The day's removed names are written to
  `output/TV/US/<date>_SMA50Pruned.txt` (comma-sep; absent when nothing was
  pruned; a same-day rerun **merges** into it, since the rerun no longer sees
  names the first run removed; no Webull mirror / Futu / TV sync; aged out by
  the `TV/US` 5-day rule; write failure is soft). **Trailing-NaN Close fill**
  (`fill_missing_last_close`): Yahoo sometimes publishes the latest session's
  daily row with OHLV but NaN Close for hours (all tickers, 2026-09-22 — CF
  was kept one day stale). The prune fills such a row from one batch of that
  day's 1h bars (last bar's close), warns, and only falls back to the stale
  day when intraday has nothing either. The ETF RS ranking gets the same fill
  (`us_rs_3m.fetch_us_klines_yf(fill_last_close=True)`, default off — the
  cloud RS jobs are unchanged; 2026-10-22 it ranked on 10-20, all markers
  `=`). The rest of us-eod still `dropna`s and runs a day stale on such days.
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
  `eod_seen_*.txt` masters, `ntfy_last_seen.txt`, `edgar_cache/`, logs. The
  pruners' `eod_seen_<MKT>.txt.bak.<stamp>` backups age out after 2 days.
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
  only the comparison basis moved. `sma_bypass_gap_percent` (in `[morning_gap]`
  US / `[hk_morning_gap]` HK) waives **SMA50 only** for big gappers — SMA200 is
  never waived. **Recent IPOs** (< 200 completed bars, ≥ 50) skip SMA200 and
  gate on SMA50 alone, which then can NOT be waived by the gap bypass (it is
  the only floor); < 50 bars → dropped. Since 2026-10-21 (SPCX, 90 bars, was
  dropped as "insufficient daily bars for SMA200"). Both knobs are per-market config; `sma_use_live_price = false`
  restores the old basis. Separately, the **ADR% floor relaxes for big
  gappers**: gap ≥ `adr_bypass_gap_percent` → floor drops from
  `min_adr_percent` to `adr_bypass_min_percent` (US: 10% → 3.0; HK wired but
  off — its base is already 3.0). Since 2026-10-21 US morning-gap overrides
  `[morning_gap].min_adr_percent = 3.0` (was inherited 4.0 — SPCX +8% pre at
  ADR% 3.97), so the US bypass is currently a no-op too; EOD stays at 4.0. **EOD carries the same bypass for US Longs
  `the_setup` + `earnings_gap` only** (per-group `[[longs]]` keys
  `adr_bypass_gap_percent` / `adr_bypass_min_percent`, 10% → 3.0; gap =
  latest completed daily bar's Open vs prior Close, `_daily_gaps`) — CRM
  2026-08-27 gapped +11.9% on earnings at ADR% 3.74 and no EOD group could
  emit it. Every other EOD ADR% call (other Longs groups, Leaders, RS,
  Shorts) is untouched. **Pre-market volume gate** (US negative offsets only,
  `_filter_pre_market_volume`, `[morning_gap].min_pre_volume_ratio` = 0.05,
  0 = off): Futu `pre_volume` (carried on `GapQuote.pre_volume`) must be ≥
  ratio × 20d avg volume — a 5% `pre_change_rate` printed on a few hundred
  shares is a thin-tape artifact (NVT/WIX 2026-09-11 opened +1.7%/-0.1%).
  Runs after the 20d avg-volume gate on the same yfinance frame; a quote
  without `pre_volume` is kept. **Big-gap bypass since 2026-10-23**
  (`pre_volume_bypass_gap_percent` = 10 / `pre_volume_bypass_ratio` = 0.02,
  same shape as the ADR% knob; gap_percent 0 = off, ratio 0 = waived):
  RNG gapped +12% on earnings with 33k pre-market shares = 2.2% of its
  1.49M avg and was dropped at all three pre-market scans; ELAN's 204
  shares still fail at 2%.
  Consequence, and it is intended: the gate drifts intraday, but each scan
  writes its own per-offset `.txt` (not cumulative, and morning-gap never
  touches `eod_seen_*`), so drift only affects the one-time ntfy/catalyst
  gate (`morning_gap_seen_{pre,post}_<date>.txt`), not what's written. Spec:
  `docs/superpowers/specs/2026-08-13-morning-gap-live-price-trend-gate-design.md`.
- **Report** — daily LLM generation is OFF since 2026-09-17; wiring and
  evidence-prefetch details live in `report/CLAUDE.md`.
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
`data/us_rs_3m/` (3M). HK: `data/hk_rs/` (12M+3M, vs HSI). Thresholds: 12M tiers
90, 3M tiers 90 in both markets since 2026-10-10 (was 95 from 2026-10-02;
Leaders / conditional RS / Shorts / IPO ≥ 64 rows; US `[settings].min_rs_percentile_3m`, HK
`[hk_settings].min_rs_percentile_longs_3m` + `[hk_shorts].min_rs_percentile_3m`;
the code-side fallback when a key is unset is still 90); set 0 to
disable a tier. The HK metrics frame is now also cloud-published (`data/hk_metrics/`,
same workflow) and fetched locally via `hk_metrics.build_hk_metrics_cloud`, so
discovery runs on the full universe on the happy path; a cloud miss falls back to the
local (throttle-prone) k-line fetch.

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
  both 20.0 since 2026-10-02, was 0.0) —
  HKEX:625 2026-09-29 was emitted 21 days after listing at 4w -35%. NaN
  `perf_4w` (exactly 20 rows) is **dropped, not kept**: the name isn't in the
  master yet, and keeping it would let every IPO through ungated on its first
  eligible day. HK re-evaluates it at 21 rows; a US name only comes back if it
  passes a Finviz long-side screener again. Key unset = gate off.
- **Do NOT make fetch failure hard-fail:** walk back ≤ 3 days of stale cache, then
  pass through (no gate) with a warning. Tickers **missing** from the table are
  KEPT, not dropped.
- **12M table freshness** (`rs_rating._wait_for_fresh_table`): rs-log publishes
  ~01:30–04:00Z and us-eod fetches ~02:00Z — 2026-10 got the previous
  session's table on 14/19 days (CART 10-27 gated at 90 on 10-23 data; the
  10-26 table said 94). The CSV has no date: its `Price` column is matched
  against raw yfinance closes of 5 mega caps (`table_data_session`). Behind
  the latest completed session (a partial bar during US hours doesn't
  count) → re-fetch every 5 min for ≤ 60 min, then gate on the stale table
  with a WARNING and do **not** cache it as `rs_rating_<today>.csv` (a rerun
  re-fetches). Any failure of the check itself (no Price column, yfinance
  down) → skipped, old behavior (use + cache). **The `run_eod.sh` watchdog
  (`EOD_TIMEOUT`, 5400s) must stay above this 60-min wait plus a full run:**
  at the old 1800s it SIGTERMed the 2026-10-28 run mid-poll (rs-log
  published 10:42 HKT) with nothing in the python log — the wrapper now
  logs an `[ERROR] [watchdog]` line when it fires. Change
  `_STALE_MAX_WAIT_SEC` and `EOD_TIMEOUT` together.
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
  same-day snapshot stays) — 2026-10-17 10:53 a throttled batch left 78/91
  without data and the top-10 was cut from 13 names. The report lists such
  names as `no data fetched` (KEPT), separate from `insufficient history`.
  Cleanup: snapshots 4-day window; audit report + sidecars (`rs-audit/`)
  5-day window.

## ETF 3M RS ranking

- **ETF 3M RS ranking** (`etf_rs.py`, `[etf_rs]` config): the fixed
  `[etf_rs.tickers]` table (ticker = 中文名, 54 entries) is scored
  **locally** with the same 3M algorithm (`compute_us_rs_3m_table`, vs
  `benchmark` SPY; one yfinance batch, no cloud step) and written to
  `output/TV/US/<date>_ETF_rs_3m.txt` (stem `ETF_rs` before 2026-10-15) as **one `N. TICKER 🟢↑N - 中文名 | 前十大持仓`
  per line, strongest at the top** — human-readable, NOT a TradingView
  import (the only non-comma `.txt` in `TV/US/`). The **rank-change marker**
  after the ticker (`🟢↑N` / `🔴↓N` / `=` / `新`, `rank_delta_marker`; the
  colored dot is the only way to tint an arrow in plain text) compares
  against the latest `*_ETF_rs_3m.txt` (or legacy `*_ETF_rs.txt`, `_LEGACY_STEMS`) in the same folder dated strictly
  **before** today (`read_previous_ranks`; ticker = first whitespace token after the `N.` prefix
  of each line, so annotated files re-parse) — a same-day rerun keeps
  comparing to the prior day, and with no earlier snapshot (first run,
  retention gap) the marker is omitted altogether. No extra state file; the
  5-day `TV/US` retention IS the lookback window. Holdings come from the
  **static** `[etf_rs.holdings]` table (hand-transcribed from issuer
  disclosures, dated in its comment; no API refresh — update by hand;
  missing entry → segment omitted). Tickers with an **identical 中文名**
  collapse to the strongest one (`collapse_same_name`; the name is the
  same-instrument key — the former GDXU/NUGT pair was the motivating case;
  NUGT and 12 global-market ETFs were dropped 2026-09-23, so no duplicate
  names remain today, rule kept) — the log still lists what was hidden.
  **Non-ETF rows** (mechanism kept, table empty since 2026-10-14 — BTCUSD /
  ETHUSD were removed together with QQQ IWM RSP USO GLD SLV EWJ EWY):
  `[etf_rs.yf_symbols]` maps display ticker → Yahoo symbol (`BTC-USD`); their
  7-day klines are cut to SPY's trading days (`align_to_benchmark_days`)
  because the 3M score counts rows. A crypto name must differ from IBIT's
  "比特币" (would collapse).
  Percentile is **within the ETF set**, not the stock universe. Ranking
  snapshot only: same-day rerun overwrites, no `eod_seen` dedup, no Webull
  mirror, no Futu/TV sync; the report ignores it (unknown group stem). Runs
  as a soft side-step at the end of us-eod / eod (also `--mode etf-rs`);
  aged out by the generic `TV/US` 5-day rule. **1M companion file**
  (since 2026-10-15): the same run also writes `<date>_ETF_rs_1m.txt`
  from the same klines on the **1M weekly ladder** `WEIGHTS_1M_BARS`
  (cumulative 1/2/3/4-week returns = 5/10/15/20 bars back, 0.4/0.2/0.2/0.2,
  21 bars minimum; same benchmark-relative score + in-set percentile).
  Same line format, same-name collapse and marker rules, but markers
  compare only against the previous `_1m` file (`read_previous_ranks(stem=)`;
  the stems are matched exactly so the two never cross).
  It is a soft step after the 3M file (`_write_1m_ranking`) — a failure
  never costs the 3M `.txt`; the trend page stays 3M-only. Scoring
  lookbacks are bars, not months: `us_rs_3m._score_from_kline_bars`
  takes `(bars, weight)` pairs and `compute_us_rs_3m_table(weights_bars=)`
  selects the ladder; the month-based `_score_from_kline` / `WEIGHTS_3M`
  API is a thin front (`BARS_PER_MONTH` = 21) and unchanged for IPO/cloud. **Trend page**
  (`etf_rs_chart.py`): the same run then writes
  `output/Reports/ETF/etf_rs_trend.html` — **undated, overwritten** (it
  carries its own history; no `_RETENTION_RULES` entry matches it, by
  design). History is **recomputed from the run's klines**, not read from
  old `.txt` (they hold no scores and age out in 5 days) — which is why
  `_fetch_klines` pulls `1y` although the ranking needs 64 bars. Each day's
  score = `rank_etfs` on klines truncated to that day (tests pin this), with
  today's ticker list + adjusted closes, so a past day may sit a place off
  that day's `.txt`. Y = 0-99 percentile (within the ETF set → RS ≥ 90 is
  always ~5 of 49) / rank / raw excess return; top `chart_top_n` (20) thick.
  Soft inside `run_etf_rs`: a render failure never costs the `.txt`.
  **3M + 1M on one page** (since 2026-10-15): `write_trend_chart` computes
  both histories (`score_history(weights_bars=WEIGHTS_1M_BARS)` for 1M) on
  the 3M calendar and embeds them as `s`/`p` (3M) and `s1`/`p1` (1M) per
  series; `run_etf_rs` passes the 3M-unscored names too (`missing`, 21-63
  bars) so they get a 1M line with null 3M.
  **Per-ETF strongest-holdings line** (since 2026-10-31, `etf_holdings_rs.py`,
  `[etf_rs.holdings_rs]`): under every ETF row in BOTH `.txt` files one
  3-space-indented continuation line `   1M强: A > B > C > D > E` — the
  ETF's **US-listed** constituents ranked by the same 1M ladder vs SPY
  (within the ETF only, no percentile), top `top_n` (5), fewer when fewer
  score, no line when none. Constituent source chain per ETF, first hit
  wins: today's cache → stockanalysis `/etf/<slug>/holdings/` (SSR table;
  **free tier = top 25 by weight**, "Showing 25 of 78 holdings") → earlier
  cache ≤ `cache_max_age_days` (7) → yfinance `funds_data.top_holdings`
  (line gets ` (前十大)`) → nothing. Cache
  `output/state/etf_holdings_<YYYY_MM_DD>.json` holds stockanalysis
  results only (foreign codes included; filtered at ranking), 8-day
  cleanup rule; a same-day rerun reads it and never goes online. `BRK.B`
  is requested as `BRK-B`, displayed as `BRK.B`. Computed once in
  `run_etf_rs` **before** either file is written (soft: failure → files
  without the line); `read_previous_ranks` skips indented lines so the
  rank markers ignore it. The cloud Pages build passes
  `run_etf_rs(holdings_rs=False)` — no scrape, no ~1000-ticker download in
  GH Actions, page unchanged. Full holdings later = add an issuer source
  function in front of the stockanalysis step. Spec:
  `docs/superpowers/specs/2026-10-31-etf-holdings-1m-rs-design.md`.
  **Also published to GitHub Pages** (https://ericxue92.github.io/momentum-scanner/):
  `.github/workflows/etf_rs_page.yml` rebuilds it in the cloud
  (`scripts/build_etf_rs_page.py` → `_site/index.html`, deployed as a Pages
  artifact — **no HTML is committed**). Triggers: `workflow_run` after the
  launchd-dispatched US 3M RS workflow, a backup cron, manual dispatch —
  and, **the reliable daily tick since 2026-10-23**, a direct
  `gh workflow run etf_rs_page.yml` at the end of `run_eod.sh` (soft; never
  changes the EOD exit status): the 08:45 dispatch fails when that slot
  wakes on battery without a network (2026-10-23: 10/10 attempts, page
  stayed on 10-22 while the local `.txt` was 10-23) and GH cron is
  unreliable, whereas a finished us-eod proves the network is up. The build exits 1 when no page was produced so an
  outage keeps yesterday's page live instead of deploying nothing. The local
  copy under `output/Reports/ETF/` is independent of it.

## yfinance single-ticker frames

- **yfinance single-ticker frames:** `group_by="ticker"` returns a (ticker,
  field) MultiIndex even for ONE ticker (yfinance 1.x); every `single` branch
  indexes `data["Close"]` flat, so each download site wraps the result in
  `_flatten_single_ticker_frame`. Without it a one-hit screener day drops its
  only candidate ("failed to process ..., dropping" — ACVA 2026-09-11).
  Corollary: anything going through `_yf_download_with_retry` with ONE
  ticker gets a flat frame back — `fetch_hsi_kline_yf` indexed `data["^HSI"]`
  and KeyError'd on every run 2026-09-12 → 09-25 (HK rs-line audit scored
  0/37, cloud HK RS shipped without `rs_below_ma`); it now accepts both shapes.
  Same trap, found 2026-09-29: `us_rs_3m.fetch_us_klines_yf` indexed
  `batch_data[t]` on the one-ticker SPY benchmark fetch (`_fetch_spy_kline`,
  also any trailing batch of one) → no benchmark since 2026-09-12: US rs-line
  audit scored 0/118 (no `rs_us_<date>.txt`), cloud US 3M CSVs shipped
  absolute scores without the `rs_*_ma` columns (percentile order unaffected —
  the benchmark score is a constant offset). `hk_eod.fetch_hk_klines_yf` had
  the same latent bug. Both accept flat frames now. **Test fakes must mimic
  the runtime shape** (flat for one ticker) — the old MultiIndex-only fake is
  why the single-ticker test stayed green.
  **Filters decide flat-vs-batch by the frame's shape** (`_is_flat_frame`),
  never by `len(tickers) == 1`: morning-gap downloads `daily_data` once for
  the whole discovery list, so a lone survivor of a later gate is still
  inside a MultiIndex batch — the old heuristic KeyError'd and dropped it
  (PENG/DUOL/IONQ, 2162.HK, 2026-10). EOD Longs/Leaders batch downloads go
  through `_download_daily_batch`: empty-after-retry or still-throttled
  after one 30s cooldown = *degraded* → the group's drops are NOT recorded
  in `ipo_drops` (2026-10-17: 43 Leaders names incl. ABBV/CRM went in from
  one throttled batch). `rs_rating.fetch_rs_table` also catches
  `http.client.HTTPException` / `UnicodeDecodeError` (neither is an
  OSError; they aborted us-eod before any `.txt`). `_yf_download_with_retry`
  waits `_YF_RETRY_DELAYS` (5s, 20s) between its 3 attempts — an empty batch
  is Yahoo's throttle and its window is ≥ 30s. A Longs group downloads its
  2mo batch once (`_longs_group_batch`, `batch=` on both filters). HK Shorts
  goes through `_download_daily_batch(threads=True)` too. **K-line fetchers
  recover once:** `us_rs_3m.fetch_us_klines_yf` (`recover=True`) and
  `hk_eod.fetch_hk_klines_yf` re-download, after a 30s cooldown, the tickers
  a failed or throttled batch left with NO data (a short-but-real history,
  e.g. a fresh IPO, is not retried) — one pass, no loop, and skipped when
  the first pass already took > 25 min (US) / 12 min (HK)
  (`_RECOVERY_MAX_ELAPSED_SEC`: a dead Yahoo must not push the cloud job
  past its GH timeout before `return 1`). One-batch callers
  (SPY benchmark, IPO candidates, rs-line audit, ETF RS) had no second
  chance before. IPO candidates still without k-lines are counted as
  `drops['no_data']` (warned; re-evaluated next run), not silently lost.

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
  every Futu consumer soft-fails, so a dead OpenD used to leave only WARNINGs
  while scans came back empty (2026-09-28: HK EOD, all 9 US morning-gap scans
  and the next day's runs; KOD's pre-market gap never scanned). One probe at
  the top of us-eod / hk-eod and of each **in-window** morning-gap scan
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
  `/usr/bin/caffeinate -i -s`, `main._CAFFEINATE_BIN`). Do not remove. The
  US morning-gap plist runs `run_morning_gap.sh` (log rotated daily like the
  other wrappers; it used to write `launchd_morning_gap.log` via
  StandardOutPath and grew unbounded). On battery
  `pmset -g` has `sleep 1`: 2026-10-17 (12%, lid closed) us-eod idle-slept
  3 min in and only advanced inside 5-15s DarkWake windows with no Wi-Fi/DNS
  — 55 min, yfinance empty, Futu 网络中断, conditional RS (SPY -1.1%/QQQ
  -2.5%) lost to NameResolutionError; the 08:45 trigger ran at a 09:01
  DarkWake and slept 2s after dispatching. `-i` works on battery, `-s` is
  AC-only; a lid-close by the user still sleeps the Mac. Belt-and-suspenders:
  EOD/in-window morning-gap runs probe `pmset -g batt` (`power.py`,
  `main._alert_if_on_battery`) and ntfy once per HKT day
  (`state/on_battery_alerted_<date>.txt`, 2-day cleanup); the conditional
  RS group retries network errors (`_retry_on_network_error`: wait for
  finviz reachable ≤ 180s, 3 attempts; non-network errors propagate); and
  the trigger script polls `gh run view` status/conclusion for up to ~20 min
  before alerting — `completed`+non-success → "failed", never confirmed →
  "unconfirmed" (default priority, exit 2), so a watch killed by sleep /
  no network no longer pushes a false "failed (conclusion=unknown)".
  `TRIGGER_*` env overrides exist only for `tests/test_trigger_rs_workflow.py`.
- **RS workflow self-trigger:** GH Actions' scheduled cron is unreliable
  (delayed hours; sometimes skipped — observed 2026-06-16: today's 3M CSV
  missing because GH cron never fired). Launchd dispatches the workflow via
  `gh workflow run` 75 min before EOD: `us-rs-3m-trigger` (Tue-Sat 08:45) and
  `hk-rs-trigger` (Mon-Fri 18:45) → `scripts/trigger_rs_workflow.sh`. The
  GH-side cron is kept as belt-and-suspenders; workflow's commit step is
  idempotent (`git diff --staged --quiet → exit 0`) so a double-fire is
  harmless. Failures ntfy via the morning-gap topic. Each trigger has a
  pmset wake 1 min before it (08:44 Tue-Sat / 18:44 Mon-Fri, seeded weekly by
  `schedule_morning_gap_wakes.py` with the morning-gap wakes) and the script
  retries the dispatch ~5 min — without both, launchd ran the missed 08:45
  slot at the next wake with no network (2026-10-15) and us-eod got a stale
  3M table.
  Every other launchd slot gets the same 1-min-lead wake (Mon-Fri): one
  per US / HK morning-gap slot (`US_GAP_SLOTS` / `HK_GAP_SLOTS` — `pmset
  sleep 1` re-sleeps an idle Mac within a minute and caffeinate only holds
  during a scan, so one wake covers one slot; the 20:59 / 21:59 11-min-lead
  wakes stay) and 19:59 for hk-eod (2026-10-16 it ran only when the lid
  opened). `tests/test_schedule_wakes.py` reads the plists and fails when a
  slot has no wake — edit the slot lists together with the plists.
- **Finviz 1000-row cap:** pages past r=1000 403 without Elite and the
  library swallows it. `run_screener` compares against the page's total and
  re-queries `order="-ticker"` for the tail (covers ≤ 2000 rows; beyond that
  it WARNs "still truncated"). Keep every Screener call inside `run_screener`
  — it (and `check_market_down`) also retries transient network errors via
  `_retry_on_network_error` (DNS/reset/5xx/finviz timeout/"Too many
  requests"; a Finviz 4xx is deterministic and NOT retried, except 429).
  Nothing else needs its own wrapper.
- **Finviz redesign (2026-10-20):** the screener moved to `/screener/stocks`
  with rows as SSR JSON; finviz 2.0.0 parsed nothing → NoResults → every US
  EOD list empty, silently. `main.Screener` is now
  `finviz_screener.Screener` (same `.data` / `._total_rows` / NoResults
  surface; 20 rows/page, `r=` paging to the 1000 cap; Market Cap from the
  overview row, `table` ignored). A page with no rows block raises
  `FinvizLayoutError`; a filter code the page does not define raises
  `FinvizFilterError` — the new site **silently ignores** unknown codes
  (`ta_perf_52w150o` returned the whole market; Leaders 52W is now `+100%`,
  `ta_perf` has no custom range). `ind_stocksonly` is implied by the path
  and whitelisted. `finviz.get_stock` (market check) still uses the library.
  **Guard:** `run_screener` counts into `_finviz_tally` (reset at us-eod
  start); after the RS section `_alert_if_finviz_broken` logs ERROR + ntfys
  (once/day, `state/finviz_broken_alerted_<date>.txt`, 2-day cleanup) when
  ≥ 5 screens all returned 0 rows or any code was rejected. Alert only.
