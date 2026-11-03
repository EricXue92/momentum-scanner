---
paths:
  - "etf_*.py"
  - "scripts/build_etf_rs_page.py"
  - ".github/workflows/etf_rs_page.yml"
  - "tests/test_etf_*.py"
  - "tests/test_build_etf_rs_page.py"
---

# ETF RS ranking (3M / 1M files, holdings line, leaders watchlist, trend page + Pages)

Loaded only when working with the files above (moved out of the root
`CLAUDE.md` on 2026-11-02 to keep it out of unrelated sessions). The root
file keeps the cross-cutting pieces: the undated-file exceptions under
"Dated files only" and the cleanup rule. Incident history: `docs/decisions.md`.


- **3M ranking file** (`etf_rs.py`, `[etf_rs]`): the fixed `[etf_rs.tickers]`
  table (ticker = 中文名, 53 entries) is scored **locally** with the same 3M
  algorithm (`compute_us_rs_3m_table`, vs `benchmark` SPY; one yfinance
  batch, no cloud step) and written to `output/TV/US/<date>_ETF_rs_3m.txt`
  as **one `N. TICKER 🟢↑N - 中文名 | 前十大持仓` per line, strongest at the
  top** — human-readable, NOT a TradingView import (the two ranking files
  are the only non-comma `.txt` in `TV/US/`). Percentile is **within the
  ETF set**, not the stock universe. The **rank-change marker** after the
  ticker (`🟢↑N` / `🔴↓N` / `=` / `新`, `rank_delta_marker`) compares
  against the latest `*_ETF_rs_3m.txt` (or legacy `*_ETF_rs.txt`,
  `_LEGACY_STEMS`) in the same folder dated strictly **before** today
  (`read_previous_ranks`; ticker = first whitespace token after the `N.`
  prefix of each line, indented lines skipped) — a same-day rerun keeps
  comparing to the prior day, and with no earlier snapshot the marker is
  omitted altogether. No extra state file; the 5-day `TV/US` retention IS
  the lookback window. Holdings come from the **static** `[etf_rs.holdings]`
  table (hand-transcribed, dated in its comment; no API refresh; missing
  entry → segment omitted). Tickers with an **identical 中文名** collapse to
  the strongest one (`collapse_same_name`; no duplicates today, rule kept;
  the log lists what was hidden). **Non-ETF rows** (`[etf_rs.yf_symbols]`,
  display ticker → Yahoo symbol, 7-day klines cut to SPY's trading days by
  `align_to_benchmark_days` because the 3M score counts rows): mechanism
  kept, table empty (→ decisions 2026-10-14); a crypto name must differ from
  IBIT's "比特币" (would collapse). Ranking snapshot only: same-day rerun
  overwrites, no `eod_seen` dedup, no Webull mirror, no Futu/TV sync; the
  report ignores it (unknown group stem). Soft side-step at the end of
  us-eod / eod (also `--mode etf-rs`); `TV/US` 5-day rule.
- **1M companion file** `<date>_ETF_rs_1m.txt` (`_write_1m_ranking`, soft
  after the 3M file — a failure never costs the 3M `.txt`): the same klines
  on the **1M weekly ladder** `WEIGHTS_1M_BARS` (cumulative 1/2/3/4-week
  returns = 5/10/15/20 bars back, 0.4/0.2/0.2/0.2, 21 bars minimum; same
  benchmark-relative score + in-set percentile). Same line format, collapse
  and marker rules, but markers compare only against the previous `_1m`
  file (`read_previous_ranks(stem=)`; stems matched exactly). Scoring
  lookbacks are bars, not months: `us_rs_3m._score_from_kline_bars` takes
  `(bars, weight)` pairs and `compute_us_rs_3m_table(weights_bars=)`
  selects the ladder; the month-based `_score_from_kline` / `WEIGHTS_3M`
  API is a thin front (`BARS_PER_MONTH` = 21), unchanged for IPO/cloud
  (→ decisions 2026-10-15).
- **Per-ETF strongest-holdings line** (`etf_holdings_rs.py`,
  `[etf_rs.holdings_rs]`): under every ETF row in BOTH `.txt` files one
  3-space-indented continuation line `   1M强: A > B > C > D > E` — the
  ETF's **US-listed** constituents ranked by the same 1M ladder vs SPY
  (within the ETF only, no percentile), top `top_n` (5), fewer when fewer
  score, no line when none. Constituent source chain per ETF, first hit
  wins: today's cache → stockanalysis `/etf/<slug>/holdings/` (SSR table;
  **free tier = top 25 by weight**) → earlier cache ≤ `cache_max_age_days`
  (7) → yfinance `funds_data.top_holdings` (line gets ` (前十大)`) → nothing.
  Cache `output/state/etf_holdings_<YYYY_MM_DD>.json` holds stockanalysis
  results only (foreign codes included; filtered at ranking) plus the
  page's own US marker (`us`: rows whose Symbol cell links to
  `/stocks/<sym>/`; a regex cannot tell cash / futures pseudo-symbols from
  real tickers — → decisions 2026-10-31), 8-day cleanup rule; a same-day
  rerun reads it and never goes online. Ranking pool = `us` when known,
  else (yfinance fallback, pre-`us` cache) the `is_us_symbol` regex, which
  also drops 5-letter symbols ending in X (mutual / money-market funds).
  `BRK.B` is requested as `BRK-B`, displayed as `BRK.B`. Live fetch: HTTP
  404 = no page (DXYZ, IBIT), 3 consecutive transient failures = no more
  live requests that run (`_MAX_CONSECUTIVE_FAILURES`); one ETF's exception
  never costs the others or the cache write. Computed once in `run_etf_rs`
  **before** either file is written (soft: failure → files without the
  line). Full holdings later = add an issuer source function in front of
  the stockanalysis step. Spec:
  `docs/superpowers/specs/2026-10-31-etf-holdings-1m-rs-design.md`.
- **Leaders watchlist** (`etf_rs_leaders.py`, `[etf_rs.leaders]`): right
  after the 1M file is written, its first `top_etfs` (10) rows are read
  back (the **file**, not the in-memory tables, by design) and the symbols
  of their `1M强:` lines merged in rank order (an ETF without a line
  contributes nothing, no back-fill from rank 11) into
  `output/TV/US/ETF_rs_leaders.txt` — the **master**: undated,
  comma-separated (TradingView import), append-only in order of first
  appearance, never aged out, delete to reset; hand edits with newlines /
  duplicates are read fine. Names the master lacked go to
  `<date>_ETF_rs_new.txt` (comma-sep; absent when nothing new; a same-day
  rerun grows the master but keeps the first run's file; `TV/US` 5-day
  rule; stem rationale → decisions, standing). Soft step; no ntfy / Webull
  / Futu / eod_seen. **One bar download per run** (`run_leaders`: 6mo
  Close + Low + Volume for master ∪ today's candidates,
  `sma50_prune.drop_stale_series` + `fill_missing_last_close` reused;
  High too, for ADR%) serves two gates. **Floors** (`min_price` 20 /
  `min_dollar_volume` 100M = 20-session mean of close × volume,
  `find_floor_drops`; `min_adr_percent` 2.5 = 20-session mean of
  (High − Low) / Close × 100, `find_adr_drops`; 0 = off): a candidate under a floor is **not added** today (enters the day it
  qualifies; no bars → added, pass-through), and an existing name under a
  floor is removed by the prune. **Master prune** (`prune_master`, right
  AFTER today's names were added, `prune = true` / `sma_period = 50`):
  floors ∪ `find_break_drops` (trailing streak of closes below SMA50 ≥ 2
  sessions AND latest close < the **low of the streak's first day**) →
  removed, written to `<date>_ETF_rs_pruned.txt` (comma-sep, same-day
  rerun merges, 5-day rule). This is NOT `sma50_prune`'s rule (prior
  close) — do not unify them. Streak of 1 / close back above the line
  (streak restarts) / close ≥ first-day low / < 50 bars / no data / stale
  series / NaN first-day low → kept by the SMA rule; no volume series →
  kept by the dollar-volume floor; no high / low series or < 20 usable bars →
  kept by the ADR% floor (unlike the strict `main._filter_adr_percent`). Failed batch → entry gate waived, prune
  skipped. No `.bak`; the pruned file is the record. A pruned name
  re-enters the next day it is in a top ETF's top 5 and clears the floors.
  Test files that run `run_etf_rs` stub `etf_rs_leaders._fetch_daily_bars`
  (autouse) or the step goes online.
- **Trend page** (`etf_rs_chart.py`): the same run writes
  `output/Reports/ETF/etf_rs_trend.html` — **undated, overwritten** (it
  carries its own history; no `_RETENTION_RULES` entry matches it, by
  design). History is **recomputed from the run's klines** (`_fetch_klines`
  pulls `1y`), not read from old `.txt`. Each day's score = `rank_etfs` on
  klines truncated to that day (tests pin this), with today's ticker list +
  adjusted closes, so a past day may sit a place off that day's `.txt`.
  Y = 0-99 percentile (within the ETF set → RS ≥ 90 is always ~5 of 53) /
  rank / raw excess return; top `chart_top_n` (20) thick. `write_trend_chart`
  computes both histories (`score_history(weights_bars=WEIGHTS_1M_BARS)`
  for 1M) on the 3M calendar and embeds them as `s`/`p` (3M) and `s1`/`p1`
  (1M) per series; `run_etf_rs` passes the 3M-unscored names too
  (`missing`, 21-63 bars) so they get a 1M line with null 3M. The holdings
  line is embedded verbatim as `strong` per series (`extras=`) and rendered
  under the holdings row of the side list (tooltip untouched). Soft inside
  `run_etf_rs`: a render failure never costs the `.txt`.
- **GitHub Pages** (https://ericxue92.github.io/momentum-scanner/):
  `.github/workflows/etf_rs_page.yml` rebuilds the page in the cloud
  (`scripts/build_etf_rs_page.py` → `_site/index.html`, deployed as a Pages
  artifact — **no HTML is committed**) running the same `run_etf_rs` chain,
  holdings line and leaders step included (`holdings_rs=False` is the
  opt-out; cold constituent cache every run, ~3 min extra, job timeout 25
  min; a stockanalysis block from GH IPs degrades to the yfinance top-10
  line, never an empty page). Triggers: `workflow_run` after the
  launchd-dispatched US 3M RS workflow, a backup cron, manual dispatch —
  and, **the reliable daily tick**, a direct `gh workflow run
  etf_rs_page.yml` at the end of `run_eod.sh` (soft; never changes the EOD
  exit status; → decisions 2026-10-23). The build exits 1 when no page was
  produced so an outage keeps yesterday's page live instead of deploying
  nothing. The local copy under `output/Reports/ETF/` is independent of it.
