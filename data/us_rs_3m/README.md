# US 3M Relative Strength — Published Tables

Daily IBD-style 3-month RS percentiles for the US universe (~5900 tickers), published by `.github/workflows/update_us_rs_3m.yml` every weekday at 01:00 UTC. The same job publishes the 12-month table to `data/us_rs_12m/`.

## Why this directory exists

Computing the full 3M RS table locally on a home IP gets throttled by Yahoo Finance after ~2000 tickers (rolling IP-cumulative limit, not solvable by `curl_cffi` browser fingerprinting alone). GitHub Actions runners get fresh Azure-pool IPs per run, so the compute runs reliably there.

Local pipeline reads, cloud pipeline writes. The job downloads ~18 months once (for the 12M table) and scores this table on the last 6 months.

## Schema

One CSV per weekday, named `<YYYY-MM-DD>.csv` (ISO date, dashes).

| Column | Type | Meaning |
|--------|------|---------|
| `ticker` | string (index) | NASDAQ/NYSE symbol, uppercase |
| `raw_score` | float | `Σ wᵢ·Rᵢ - SPY_score` where `WEIGHTS_3M = [(1,0.5),(2,0.3),(3,0.2)]` |
| `rs_percentile` | int (0-99) | rank of `raw_score` across the universe |
| `rs_percentile_1m` | int (0-99) | market-wide percentile on the 1M weekly ladder (`WEIGHTS_1M_BARS`); blank when a ticker has no 1M score. ETF leaders entry gate |
| `rs_below_ma` | int (0/1) | RS line (price/SPY) below its own EMA21 on the latest bar |
| `rs_days_below_ma` | int | trailing consecutive sessions the RS line stayed below its MA |
| `rs_frac_below_ma` | float 0-1 | fraction of the last 20 sessions the RS line was below its MA |

The `raw_score` column is preserved so the IPO ladder (`us_ipo.py`) can `np.searchsorted` against the full distribution to score out-of-universe tickers.

## Retention

The workflow prunes files older than 14 days on every run. To extend or shorten, edit `scripts/compute_us_rs_3m_cloud.py` (`_RETENTION_DAYS` constant).

## Consumed by

- `us_rs_3m.build_3m_table` (local fetcher, via `raw.githubusercontent.com`)
- `main.py` (US EOD pipeline, 12:00 HKT launchd run)

## Manual trigger

If today's CSV is missing (workflow failure), trigger manually:

```bash
gh workflow run update_us_rs_3m.yml
```
