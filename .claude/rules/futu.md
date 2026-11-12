---
paths:
  - "futu_sync.py"
  - "main.py"
  - "hk_eod.py"
  - "net_ready.py"
  - "notify.py"
  - "tv_sync.py"
  - "tests/test_notify_opend_down.py"
---

# Futu sync

Loaded only when working with the files above (moved out of the root
`CLAUDE.md` on 2026-11-04). Incident history: `docs/decisions.md`.

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
  Market caps are the partial exception (→ decisions 2026-11-12):
  `get_market_caps_futu` retries a failed snapshot batch once, and on the
  cloud-metrics path `hk_eod._yf_caps_fallback` asks yfinance for the names
  at the price / dollar-volume floors; no caps at all → an ERROR saying
  every Longs group and the IPO list will be empty.
