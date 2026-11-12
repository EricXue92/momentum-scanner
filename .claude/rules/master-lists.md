---
paths:
  - "sma50_prune.py"
  - "ema20_break.py"
  - "tests/test_sma50_prune*.py"
  - "tests/test_ema20_break*.py"
---

# US master lists: SMA50 prune and EMA20 first-break

Loaded only when working with the files above (moved out of the root
`CLAUDE.md` on 2026-11-12; the root keeps the cross-cutting rules: backup
first, missing / stale data KEPT). Incident history: `docs/decisions.md`.

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
