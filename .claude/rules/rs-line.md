---
paths:
  - "rs_line.py"
  - "rs_line_audit.py"
  - "tests/test_rs_line*.py"
---

# RS-line trend, rs-line-audit and the strongest-RS snapshot

Loaded only when working with the files above (moved out of the root
`CLAUDE.md` on 2026-11-12; the root keeps the cross-cutting rules: EOD only
annotates, pruning is manual, the scheduled audit is `--dry-run`).
Incident history: `docs/decisions.md`.

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
  same-day snapshot stays; → decisions 2026-10-17). The report lists such
  names as `no data fetched` (KEPT), separate from `insufficient history`.
  Cleanup: snapshots 4-day window; audit report + sidecars (`rs-audit/`)
  5-day window.
