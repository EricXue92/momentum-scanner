---
paths:
  - "*.py"
  - "**/*.py"
---

# yfinance frames and batches

Loaded only when working with Python files (moved out of the root
`CLAUDE.md` on 2026-11-26; the root keeps the one cross-cutting rule: test
fakes must mimic the runtime frame shape). Incident history: `docs/decisions.md`.

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
