# US 12M Relative Strength — Published Tables

Daily IBD-style 12-month RS percentiles for the US universe (~5900 tickers), published by `.github/workflows/update_us_rs_3m.yml` (the same job and the same ~18-month yfinance download as `data/us_rs_3m/`). Until 2026-11-14 the pipeline read this table from Fred6725/rs-log; the formula is unchanged (see `us_rs_12m.py` and `docs/decisions.md`, 2026-11-14).

## Schema

One CSV per run, named `<YYYY-MM-DD>.csv` — the **run** date (UTC), which is the HKT date of the `us-eod` that reads it. Rows are sorted strongest first.

| Column | Type | Meaning |
|--------|------|---------|
| `Ticker` | string | NASDAQ/NYSE symbol, uppercase, letters only |
| `Relative Strength` | float | `(1 + strength) / (1 + strength_SPY) × 100`, truncated to 2 dp; `strength = 0.4·perf(63) + 0.2·perf(126) + 0.2·perf(189) + 0.2·perf(252)` sessions |
| `Percentile` | int (0-99) | `pd.qcut(Relative Strength, 100, duplicates="drop")` |
| `Session` | ISO date | the trading day the closes belong to (SPY's last bar) — the same in every row |

Not ranked: fewer than 120 bars, `Relative Strength >= 590`, last bar more than 5 sessions behind SPY's.

## Universe

`nasdaqtraded.txt` from nasdaqtrader.com: non-ETF, non-test, letters-only symbols, minus units, warrants, rights, preferreds, notes/bonds, ETNs and structured trust certificates, recognised from the security name (`us_rs_12m.is_common_equity`).

## When nothing is published

The job exits non-zero and writes neither this table nor the 3M one when SPY's latest bar is not the last completed session (after 3 retries, 5 minutes apart), or when more than 10% of the universe still lacks that bar after one re-fetch. The local run then walks back up to 3 days.

## Retention

Files older than 14 days are pruned by the job (`_RETENTION_DAYS` in `scripts/compute_us_rs_3m_cloud.py`); the workflow keeps the 4 most recent.

## Consumed by

- `rs_rating.fetch_rs_table` (local fetcher, via `raw.githubusercontent.com`)

## Manual trigger

```bash
gh workflow run update_us_rs_3m.yml
```
