---
paths:
  - "finviz_*.py"
  - "main.py"
  - "tests/test_finviz_*.py"
---

# Finviz screener

Loaded only when working with the files above (moved out of the root
`CLAUDE.md` on 2026-11-04). Incident history: `docs/decisions.md`. The root file keeps
the "every Screener call goes through `run_screener`" rule.

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
