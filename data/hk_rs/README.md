# HK Relative Strength — Published Tables

Daily IBD-style 12-month + 3-month RS percentiles for the HKEX Main Board universe (~2,400 tickers), benchmarked against HSI, published by `.github/workflows/update_hk_rs.yml` every weekday at 11:00 UTC (19:00 HKT).

## Why this directory exists

Computing the full HK RS table locally on a home IP gets throttled by Yahoo Finance — the ~2,400-ticker batch download came back ~50% sparse on 2026-05-25, so the percentile distribution was built over only half the universe (a ticker ranked "90th percentile" among the surviving half is not 90th among the true universe). GitHub Actions runners get fresh Azure-pool IPs per run, so the compute runs reliably there.

This mirrors the US 3M pipeline (`data/us_rs_3m/`) — local pipeline reads, cloud pipeline writes.

## Schema

One combined CSV per weekday, named `<YYYY-MM-DD>.csv` (ISO date, dashes).

| Column | Type | Meaning |
|--------|------|---------|
| `code` | string (index) | Futu-format HK code, e.g. `HK.00700` |
| `rs_percentile_12m` | int (0-99), may be blank | `WEIGHTS_12M = [(3,0.4),(6,0.2),(9,0.2),(12,0.2)]` vs HSI; blank when the ticker lacks ≥253 rows |
| `rs_percentile_3m` | int (0-99), may be blank | `WEIGHTS_3M = [(1,0.4),(2,0.3),(3,0.3)]` vs HSI; blank when the ticker lacks ≥64 rows |
| `rs_below_ma` | int (0/1) | RS line (price/HSI) below its own EMA21 on the latest bar |
| `rs_days_below_ma` | int | trailing consecutive sessions the RS line stayed below its MA |
| `rs_frac_below_ma` | float 0-1 | fraction of the last 20 sessions the RS line was below its MA |

A ticker scored for 3M but lacking 12-month history has `rs_percentile_12m` blank — the local fetcher drops blanks per column when splitting into the two single-column tables.

## Retention

The workflow prunes files older than 14 days on every run. To extend or shorten, edit `scripts/compute_hk_rs_cloud.py` (`_RETENTION_DAYS` constant).

## Consumed by

- `hk_rs.build_hk_rs_tables` (local fetcher, via `raw.githubusercontent.com`)
- `hk_eod.run_hk_eod` (HK EOD pipeline, 20:00 HKT launchd run)

## Manual trigger

If today's CSV is missing (workflow failure), trigger manually:

```bash
gh workflow run update_hk_rs.yml
```
