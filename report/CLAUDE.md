# report/ — CANSLIM + catalyst reports

Moved from the root `CLAUDE.md` (loads only when working under `report/`).
The catalyst-report process constraints stay in the root file.

**Report — daily LLM generation is OFF since 2026-09-17** (both the post-market
CANSLIM step in `run_eod.sh`, commented out, and the pre-market catalyst report,
`[morning_gap_catalyst].enabled = false`). Code paths kept for manual use
(`--mode report --market us`). The rest of this bullet describes the wiring.
Soft-fail (wrapper exit code reflects only the EOD step). Shorts /
HK Shorts / Morning Gap are excluded from it. **US only** — `run_hk_eod.sh`
no longer runs the report step (HK code path kept for manual use). Output is
**HTML only**: `output/Reports/PostMarket/<date>_us.html` (no `.md`).
**Evidence prefetch** (`report/evidence.py`, `[report.evidence]`): the EOD
report pre-fetches yfinance news / analyst consensus / earnings calendar +
EDGAR recent filings per ticker and makes ONE no-tool LLM call; Tavily is
only offered when `news + filings < min_items_for_no_search`. Each source
soft-fails independently; prefetch has a hard timeout (empty bundle →
fallback search). `enabled = false` restores the tool-loop behavior. The
pre-market catalyst path is NOT on this yet (phase 2 — spec
`docs/superpowers/specs/2026-09-09-report-evidence-prefetch-design.md`).
`max_search_calls` is now an exact cap on Tavily searches (the old loop
could run one extra).
