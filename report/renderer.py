"""Compose the daily Markdown + standalone HTML report.

Aesthetic: editorial financial broadsheet — cream paper, ink-on-paper monochrome
with two semantic accents (forest green for positive, oxblood for negative).
Per-ticker block has a numbered header, a snapshot strip, prominent latest-Q
earnings, mini SVG line charts for 5-year EPS / Revenue YoY + 4-quarter
trajectory, and the LLM-written Chinese prose. Self-contained: all CSS +
SVG inline, no external assets.

The structured data sections (snapshot / quarterly / annual YoY) are rendered
deterministically in Python — the LLM's output is appended only as prose.
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

import markdown as md_lib

# --- Aesthetic constants -----------------------------------------------------

INLINE_CSS = """
:root {
  --bg:           #F7F2E8;     /* warm linen / book-paper, same as cover */
  --card:         #FFFFFF;
  --ink:          #0A0A0A;     /* near-black */
  --ink-soft:     #222222;     /* deeper than before */
  --muted:        #555555;     /* darker secondary text */
  --faint:        #888888;
  --rule:         #C9C2B5;     /* warm rule, harmonised with paper */
  --navy:         #1F2D3D;     /* deeper navy */
  --navy-soft:    #2C3E50;
  --tint:         #EFE9DC;     /* zebra-stripe band, warmer than --bg */
  --positive:     #1E7E34;     /* deeper green */
  --negative:     #A02828;     /* deeper red */
}
* { box-sizing: border-box; }
html, body { background: var(--bg); }
body {
  margin: 0;
  padding: 32px 24px 80px;
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue",
    "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 17px;
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}
.sheet {
  width: 210mm;
  max-width: 100%;
  margin: 0 auto;
  padding: 0 22mm;
}

/* --- Masthead --- */
.masthead {
  border-bottom: 2px solid var(--navy);
  padding-bottom: 14px;
  margin-bottom: 24px;
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.masthead-title {
  margin: 0;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--navy);
}
.masthead-meta {
  font-size: 13px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.masthead-meta strong { color: var(--ink); font-weight: 600; }

/* --- Index (anchored ticker grid) --- */
.index {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 6px 16px;
  margin: 0 0 40px;
  padding: 12px 0;
  border-top: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.index a {
  text-decoration: none;
  color: var(--ink);
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.index a:hover { color: var(--navy); }
.index a:hover .sym { text-decoration: underline; }
.index .num { color: var(--faint); font-size: 11px; min-width: 18px; }
.index .sym { font-weight: 600; }
.index .grp {
  color: var(--muted);
  font-size: 10px;
  margin-left: auto;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

/* --- Ticker block --- */
.ticker {
  margin-bottom: 56px;
  scroll-margin-top: 16px;
  page-break-inside: avoid;     /* keep one ticker on one printed page */
}
.ticker-head {
  background: var(--navy);
  color: #FFFFFF;
  padding: 10px 16px;
  border-radius: 5px 5px 0 0;
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
}
.ticker-num {
  font-size: 12px;
  color: rgba(255,255,255,0.55);
  font-variant-numeric: tabular-nums;
  min-width: 22px;
}
.ticker-symbol {
  font-size: 23px;
  font-weight: 700;
  letter-spacing: -0.005em;
}
.ticker-group {
  font-size: 15px;
  font-weight: 500;
  color: rgba(255,255,255,0.88);
  letter-spacing: 0.02em;
}
.ticker-company {
  font-size: 15px;
  font-weight: 400;
  color: rgba(255,255,255,0.78);
  margin-left: 4px;
}

.ticker-body {
  background: var(--card);
  border: 1px solid var(--rule);
  border-top: 0;
  border-radius: 0 0 5px 5px;
  padding: 18px 20px 22px;
}

/* --- Snapshot table (vertical, zebra-striped) --- */
.snapshot { margin-bottom: 22px; }
.snapshot-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 15px;
  font-variant-numeric: tabular-nums;
  background: var(--card);
  border: 1px solid var(--rule);
  box-shadow: 0 1px 2px rgba(0,0,0,0.04);
}
.snapshot-table thead th {
  background: var(--tint);
  color: var(--navy);
  font-weight: 700;
  font-size: 13px;
  text-align: left;
  padding: 9px 16px;
  border-bottom: 1px solid var(--rule);
  letter-spacing: 0.04em;
}
.snapshot-table tbody td {
  padding: 9px 16px;
  border-bottom: 1px solid var(--rule);
  vertical-align: top;
}
.snapshot-table tbody tr:last-child td { border-bottom: 0; }
.snapshot-table tbody tr:nth-child(odd) td { background: #FBFBFB; }
.snapshot-table .snap-label {
  font-weight: 600;
  color: var(--ink-soft);
  width: 200px;
  white-space: nowrap;
}
.snapshot-table .snap-value { color: var(--ink); }
.snapshot-table .snap-value.positive { color: var(--positive); font-weight: 500; }
.snapshot-table .snap-value.negative { color: var(--negative); font-weight: 500; }
.snapshot-table .snap-value.hot { color: var(--ink); font-weight: 800; }

/* --- Latest quarter (CANSLIM "C") --- */
.quarterly {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 22px;
  padding: 16px 18px;
  margin-bottom: 22px;
  background: var(--card);
  border: 1px solid var(--rule);
  border-left: 4px solid var(--navy);
  border-radius: 3px;
}
.quarterly .qtr-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--muted);
  margin-bottom: 7px;
  font-weight: 700;
}
.quarterly .metric-value {
  font-family: "SF Mono", Menlo, Monaco, Consolas, monospace;
  font-size: 30px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
  line-height: 1.1;
  color: var(--navy);
}
.quarterly .yoy {
  display: inline-block;
  font-size: 14px;
  font-family: "SF Mono", Menlo, Monaco, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  margin-top: 7px;
  padding: 3px 10px;
  border-radius: 3px;
  font-weight: 700;
}
.quarterly .yoy.positive { background: rgba(39,174,96,0.10); color: var(--positive); }
.quarterly .yoy.negative { background: rgba(192,57,43,0.10); color: var(--negative); }
.quarterly .yoy.null { background: var(--tint); color: var(--muted); font-weight: 500; }
/* CANSLIM-grade growth: latest-Q rev>20% / EPS>25% — boldest visual */
.quarterly .yoy.hot { background: rgba(0,0,0,0.92); color: #fff; font-weight: 800; }
.quarterly .metric-value.hot { color: #000; font-weight: 900; }

/* --- Generated-by footer (model attribution) --- */
.report-footer {
  margin-top: 32px;
  padding-top: 16px;
  border-top: 1px solid var(--rule);
  font-size: 12px;
  color: var(--muted);
  font-style: italic;
  text-align: center;
}

/* --- Annual YoY line charts --- */
.annual {
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 14px 18px;
  margin-bottom: 24px;
}
.annual-title {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--muted);
  margin-bottom: 12px;
  font-weight: 700;
}
.chart-row {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 14px;
  align-items: center;
  padding: 10px 0;
}
.chart-row + .chart-row {
  border-top: 1px solid var(--rule);
  margin-top: 4px;
  padding-top: 14px;
}
.chart-name {
  font-size: 13px;
  font-weight: 700;
  color: var(--navy);
  letter-spacing: 0.02em;
}
.chart-svg { width: 100%; height: 88px; display: block; }

/* --- Prose sections (LLM output) — main reading area --- */
.prose h3 {
  margin: 26px 0 10px;
  color: var(--navy);
  border-left: 4px solid var(--navy);
  padding: 4px 0 4px 14px;
  font-size: 18px;
  font-weight: 700;
  line-height: 1.3;
}
.prose h3.emph {
  border-left-width: 5px;
  background: var(--tint);
  padding-top: 8px;
  padding-bottom: 8px;
  font-size: 19px;
}
.prose p { margin: 0 0 10px; }
.prose ul, .prose ol { padding-left: 1.5em; margin: 4px 0 10px; }
.prose li { margin: 3px 0; }
.prose strong { font-weight: 700; color: var(--ink); }
.prose em { font-style: italic; color: var(--ink-soft); }

/* --- Truncated tail --- */
.truncated {
  margin-top: 48px;
  padding-top: 16px;
  border-top: 2px solid var(--navy);
}
.truncated h2 {
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin: 0 0 10px;
  color: var(--navy);
  font-weight: 700;
}
.truncated .lst {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 4px 14px;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}
.truncated .lst span {
  color: var(--muted);
  font-size: 10px;
  margin-left: 4px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

/* --- Fresh-IPO no-fundamentals banner --- */
.ipo-no-data {
  padding: 12px 16px;
  margin: 14px 0 18px;
  border-left: 3px solid var(--navy-soft);
  background: var(--tint);
  font-size: 14px;
  color: var(--ink-soft);
}
.ipo-no-data strong { color: var(--navy); font-weight: 600; }

/* --- Failure / placeholder --- */
.failure {
  padding: 10px 14px;
  border: 1px dashed var(--negative);
  background: var(--card);
  font-size: 13px;
  color: var(--negative);
  font-family: "SF Mono", Menlo, Monaco, Consolas, monospace;
  border-radius: 3px;
}

/* Print — true A4 pagination. Cover gets its own sheet (page-break-after);
   each ticker block is kept whole; cream paper bg flattens to white so
   ink doesn't waste cartridge on a tinted ground. */
@media print {
  @page { size: A4; margin: 0; }
  body { padding: 0; margin: 0; font-size: 11pt; background: #FFFFFF; }
  .sheet { width: 210mm; padding: 0 22mm; }
  .cover {
    width: 210mm;
    min-height: 297mm;
    margin: 0;
    padding: 20mm 22mm;
    background: #FFFFFF;
    page-break-after: always;
  }
  .ticker { page-break-inside: avoid; margin-bottom: 24px; }
  .index { display: none; }
  .ticker-head {
    background: #2C3E50 !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
}

/* --- Cover (A4 paper format, editorial trader's tearsheet) -----------------
   Fixed 210 × 297 mm sheet on cream paper with drop shadow. Strict 5-level
   modular type scale (48pt / 18pt / 11pt / 9pt / 8pt) so the page reads as
   one composition, not a stack of unrelated rows. mm-based vertical rhythm
   (4 / 8 / 12 / 18) keeps spacing harmonious at print scale.
   ------------------------------------------------------------------------ */
.cover {
  width: 210mm;
  min-height: 297mm;
  max-width: 100%;
  margin: 8px auto 32mm;
  padding: 20mm 22mm;
  background: var(--bg);
  position: relative;
  font-feature-settings: "kern", "liga", "onum";
  font-size: 11pt;
  line-height: 1.5;
  color: var(--ink);
  display: flex;
  flex-direction: column;
}
.cover-inner {
  display: flex;
  flex-direction: column;
  flex: 1;
}
/* MICRO (8pt) — eyebrow strip. No bottom rule: whitespace alone separates
   the metadata strip from the hero, keeping the upper page calm. */
.cover-eyebrow {
  display: flex;
  align-items: center;
  gap: 4mm;
  font-size: 8pt;
  letter-spacing: 0.18em;
  color: var(--navy);
  font-weight: 700;
  margin-bottom: 18mm;
}
.cover-eyebrow-rule {
  flex: 1;
  height: 0.75px;
  background: var(--navy);
}
.cover-eyebrow-issue {
  letter-spacing: 0.14em;
  color: #6B1A1F;
  font-variant-numeric: tabular-nums;
}

/* MASTHEAD (52pt) — short Chinese masthead title, set as a publication
   nameplate. With only 3-5 characters, it carries presence at this size
   while leaving room for the strap line beneath. */
.cover-hero { margin-bottom: 14mm; }
.cover-headline {
  font-family: "Iowan Old Style", "Apple Garamond", Baskerville,
    "Hoefler Text", Georgia,
    "Songti SC", "STSong", "PingFang SC", "Hiragino Sans GB", serif;
  margin: 0;
  font-size: 52pt;
  font-weight: 500;
  letter-spacing: 0.01em;
  line-height: 1.05;
  color: var(--ink);
}
.cover-strap {
  margin: 6mm 0 0;
  max-width: 145mm;
  font-family: "Iowan Old Style", "Hoefler Text", Georgia,
    "Songti SC", "STSong", "PingFang SC", serif;
  font-size: 11pt;
  line-height: 1.6;
  color: var(--ink-soft);
}
.cover-strap strong {
  font-weight: 700;
  color: var(--ink);
}
/* Chinese emphasis: switch to sans-serif PingFang SC so the bold weight
   actually renders visibly. Songti SC (the strap's serif Chinese fallback)
   barely shifts at 700 because Chinese serifs ship few weight variants.
   Sans + navy color makes the scope note pop without screaming red. */
.cover-strap strong.cover-emph {
  font-family: -apple-system, BlinkMacSystemFont,
    "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-weight: 700;
  color: var(--navy);
  letter-spacing: 0;
}

/* SECONDARY (18pt) — ticker plate. Same size as the hero subline but bold
   navy mono so it reads as the second visual hero, not the same voice. */
.cover-plate {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1mm 6mm;
  margin-bottom: 14mm;
}
.cover-plate-ticker {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 18pt;
  font-weight: 700;
  letter-spacing: 0.01em;
  color: var(--navy);
  text-decoration: none;
  padding: 2.5mm 0.5mm;
  border-bottom: 1px solid transparent;
  font-variant-numeric: tabular-nums;
  transition: color 0.15s, border-color 0.15s;
}
.cover-plate-ticker:hover {
  color: #6B1A1F;
  border-bottom-color: #6B1A1F;
}
.cover-plate-empty {
  font-style: italic;
  color: var(--muted);
  font-size: 11pt;
  grid-column: 1 / -1;
  font-family: "Iowan Old Style", "Hoefler Text", Georgia, "Songti SC", serif;
}

/* MICRO (8pt) labels + BODY (11pt) cat names + CAPTION (9pt) ticker lists */
.cover-section { margin: 0 0 8mm; }
.cover-section-head {
  display: flex;
  align-items: center;
  gap: 4mm;
  margin-bottom: 4mm;
}
.cover-section-label {
  font-size: 8pt;
  letter-spacing: 0.22em;
  color: var(--navy);
  font-weight: 700;
}
.cover-section-rule {
  flex: 1;
  height: 0.75px;
  background: #C9C2B5;
}
.cover-section-meta {
  font-size: 8pt;
  letter-spacing: 0.06em;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.cover-breakdown {
  display: flex;
  flex-direction: column;
  gap: 4mm;
}
.cover-cat { page-break-inside: avoid; }
.cover-cat-head {
  display: flex;
  align-items: baseline;
  gap: 3mm;
  margin-bottom: 1mm;
}
.cover-cat-name {
  font-family: "Iowan Old Style", "Hoefler Text", Georgia, serif;
  font-size: 11pt;
  font-weight: 600;
  color: var(--ink);
}
.cover-cat-rule {
  flex: 1;
  height: 0.75px;
  background: #C9C2B5;
  margin-bottom: 1mm;
}
.cover-cat-count {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 9pt;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.05em;
}
.cover-cat-tickers {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  font-size: 9pt;
  letter-spacing: 0.03em;
  color: var(--ink);
  line-height: 1.6;
}

/* Colophon — methodology block (BODY 10pt) + meta rows (CAPTION 9pt) */
.cover-colophon {
  margin-top: auto;
  padding-top: 8mm;
  border-top: 0.75px solid var(--navy);
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
  gap: 6mm 12mm;
}
.cover-colophon-key {
  font-size: 8pt;
  letter-spacing: 0.22em;
  color: var(--muted);
  font-weight: 700;
}
.cover-colophon-meth .cover-colophon-key {
  display: block;
  margin-bottom: 4mm;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
    "PingFang SC", sans-serif;
}
.cover-colophon-meth-list {
  display: flex;
  flex-direction: column;
  gap: 3mm;
  font-family: "Iowan Old Style", "Hoefler Text", Georgia,
    "Songti SC", "PingFang SC", serif;
  font-size: 10pt;
  line-height: 1.55;
  color: var(--ink-soft);
}
.cover-meth-author {
  font-weight: 600;
  color: var(--ink);
}
.cover-colophon-meta {
  display: flex;
  flex-direction: column;
  gap: 3mm;
}
.cover-colophon-meta .cover-colophon-row {
  display: flex;
  gap: 3mm;
  font-size: 9pt;
  line-height: 1.45;
  color: var(--ink-soft);
}
.cover-colophon-meta .cover-colophon-key {
  flex: 0 0 22mm;
  padding-top: 0.5mm;
}
.cover-colophon-val { flex: 1; }

/* Legal disclaimer — small italic, right-aligned at the very bottom */
.cover-disclaimer {
  margin: 6mm 0 0;
  font-family: "Iowan Old Style", "Hoefler Text", Georgia,
    "Songti SC", "PingFang SC", serif;
  font-size: 7.5pt;
  font-style: italic;
  line-height: 1.5;
  color: var(--faint);
  text-align: right;
  letter-spacing: 0.01em;
  max-width: 165mm;
  margin-left: auto;
}
.cover-disclaimer strong {
  color: var(--muted);
  font-weight: 600;
  font-style: italic;
}

/* Narrow screens — cover loses A4 framing, becomes fluid */
@media (max-width: 720px) {
  body { padding: 18px 12px 48px; font-size: 14px; }
  .sheet { padding: 0; }
  .quarterly { grid-template-columns: 1fr; gap: 12px; }
  .chart-row { grid-template-columns: 70px 1fr; gap: 8px; }
  .ticker-meta { margin-left: 0; flex-basis: 100%; }
  .cover {
    width: auto;
    min-height: auto;
    margin: -18px -12px 36px;
    padding: 14mm 8mm;
    box-shadow: none;
  }
  .cover-eyebrow {
    gap: 3mm;
    flex-wrap: wrap;
    margin-bottom: 10mm;
  }
  .cover-headline { font-size: 36pt; }
  .cover-strap { font-size: 10.5pt; }
  .cover-disclaimer { font-size: 7pt; max-width: none; }
  .cover-plate {
    grid-template-columns: repeat(3, 1fr);
    gap: 1mm 4mm;
  }
  .cover-plate-ticker { font-size: 14pt; padding: 2mm 0.5mm; }
  .cover-colophon { grid-template-columns: 1fr; gap: 4mm; }
}

@media print {
  .cover {
    background: #FFFFFF;
    min-height: auto;
    page-break-after: always;
    border-bottom: none;
  }
  .cover-inner { min-height: auto; }
}
"""


# --- Number formatting -------------------------------------------------------

def _fmt_money(v: float | None) -> str:
    # NaN slips through here when yfinance returns a float NaN (vs. None);
    # treat it the same as missing instead of rendering "$nan".
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:.2f}T"
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.1f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.2f}"


def _fmt_pct(v: float | None, sign: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    s = "+" if sign and v >= 0 else ""
    return f"{s}{v:.1f}%"


def _yoy_class(v: float | None) -> str:
    if v is None:
        return "null"
    return "positive" if v >= 0 else "negative"


# CANSLIM "C" growth thresholds. Latest-quarter YoY above these earns a
# visual highlight (bold in MD, "hot" CSS class in HTML).
REVENUE_HOT_PCT = 20.0   # CANSLIM threshold: quarterly revenue YoY > 20%
EPS_HOT_PCT = 25.0       # CANSLIM threshold: quarterly EPS YoY > 25%
# CANSLIM "L" — IBD's "Leadership" floor for institutional-grade ROE.
ROE_HOT_PCT = 17.0


def _is_hot(v: float | None, threshold: float) -> bool:
    """True iff `v` is a real number strictly greater than `threshold`."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return False
    return v > threshold


def _short_company(name: str | None, max_len: int = 38) -> str:
    if not name:
        return ""
    n = name.strip()
    return n if len(n) <= max_len else n[: max_len - 1].rstrip() + "…"


def _fmt_date(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            return "—"
    s = str(v)
    return s.split(" ")[0] if " " in s else s[:10]


# --- Bar chart SVG -----------------------------------------------------------

def _line_chart_svg(values: list[float | None], labels: list[str]) -> str:
    """Connected-dot line chart for YoY deltas.

    Layout uses three horizontal bands so labels never overlap dots:

        y= 0..VLAB_H       value-label band (top)   — for positive values
        y= VLAB_H..PLOT    plot area                — dots + connecting line
        y= PLOT..PLOT+VLAB value-label band (bot.)  — for negative values
        y= PLOT+VLAB..H    period-label band        — FY-3 / FY-2 / FY-1

    Y-axis auto-scales to the actual data range. When all values share a
    sign the baseline is parked at the relevant edge of the plot so the
    full plot height encodes magnitude (instead of wasting half the canvas
    on the empty negative half)."""
    n = len(values)
    if n == 0:
        return ""

    # Treat NaN like None — upstream sources (yfinance, EDGAR fallback) sometimes
    # return float('nan') for missing periods. Letting NaN through pollutes
    # min/max/span and renders y="nan" SVG attributes, which break the chart.
    values = [None if (isinstance(v, float) and math.isnan(v)) else v for v in values]

    # Compact horizontal footprint — sized to read 5 dots (annual) cleanly;
    # the 4-quarter trajectory chart shares the same canvas.
    width, height = 320, 96
    pad_x = 28
    vlab_h = 18           # value-label band height (top + bottom)
    period_h = 14         # period label band at bottom
    plot_top = vlab_h
    plot_bot = height - vlab_h - period_h
    plot_h = plot_bot - plot_top

    nonnull = [v for v in values if v is not None]
    if nonnull:
        v_max = max(max(nonnull), 0.0)
        v_min = min(min(nonnull), 0.0)
    else:
        v_max, v_min = 1.0, 0.0
    span = v_max - v_min or 1.0

    # Baseline (zero line) y-position — auto-fit to data sign distribution.
    if v_min >= 0:
        baseline = plot_bot                       # all non-negative → zero at bottom
    elif v_max <= 0:
        baseline = plot_top                       # all non-positive → zero at top
    else:
        baseline = plot_top + (v_max / span) * plot_h

    def x_for(i: int) -> float:
        if n == 1:
            return width / 2
        return pad_x + i * ((width - 2 * pad_x) / (n - 1))

    def y_for(v: float) -> float:
        # Linear scale; clamps to plot band defensively.
        return plot_top + ((v_max - v) / span) * plot_h

    parts: list[str] = []

    # Zero baseline (subtle dashed when not at an edge, solid when at edge).
    is_edge = baseline <= plot_top + 0.5 or baseline >= plot_bot - 0.5
    base_stroke = "#D8D8D8" if is_edge else "#CCCCCC"
    base_dash = "" if is_edge else ' stroke-dasharray="3 3"'
    parts.append(
        f'<line x1="{pad_x - 6:.1f}" y1="{baseline:.1f}" '
        f'x2="{width - pad_x + 6:.1f}" y2="{baseline:.1f}" '
        f'stroke="{base_stroke}" stroke-width="0.7"{base_dash}/>'
    )

    # Connecting polyline through consecutive non-null points only.
    last_xy: tuple[float, float] | None = None
    for i, v in enumerate(values):
        if v is None:
            last_xy = None
            continue
        x = x_for(i)
        y = y_for(v)
        if last_xy is not None:
            parts.append(
                f'<line x1="{last_xy[0]:.1f}" y1="{last_xy[1]:.1f}" '
                f'x2="{x:.1f}" y2="{y:.1f}" '
                'stroke="#111111" stroke-width="1.6" stroke-linecap="round"/>'
            )
        last_xy = (x, y)

    # Dots + value labels + period labels
    period_y = height - 3
    top_label_y = vlab_h - 5         # baseline of top value-label band (text)
    bot_label_y = plot_bot + vlab_h - 4  # baseline of bottom value-label band
    for i, v in enumerate(values):
        x = x_for(i)
        # Period label (FY-3 / FY-2 / FY-1) at the bottom edge.
        parts.append(
            f'<text x="{x:.1f}" y="{period_y:.1f}" font-size="10" '
            f'font-family="SF Mono,Menlo,monospace" fill="#777777" '
            f'text-anchor="middle">{labels[i]}</text>'
        )
        if v is None:
            # Hollow gray ring on baseline + "n/a" in the bottom label band.
            parts.append(
                f'<circle cx="{x:.1f}" cy="{baseline:.1f}" r="2.6" '
                'fill="#FFFFFF" stroke="#B5B5B5" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{x:.1f}" y="{bot_label_y:.1f}" font-size="10" '
                f'font-family="SF Mono,Menlo,monospace" fill="#999999" '
                f'text-anchor="middle">n/a</text>'
            )
            continue
        y = y_for(v)
        # Filled black dot — slightly larger than before so it reads at a glance.
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="#111111"/>'
        )
        # Value label always sits in a dedicated band, never on the dot:
        # positive → top band, negative → bottom band, zero → bottom band.
        sign = "+" if v >= 0 else ""
        label_y = top_label_y if v > 0 else bot_label_y
        color = "#0A0A0A" if v >= 0 else "#A02828"
        parts.append(
            f'<text x="{x:.1f}" y="{label_y:.1f}" font-size="11.5" '
            f'font-family="SF Mono,Menlo,monospace" fill="{color}" '
            f'text-anchor="middle" font-weight="700">{sign}{v:.1f}%</text>'
        )
    return (
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>'
    )


# Keep legacy name as alias so existing test imports still resolve.
_bar_chart_svg = _line_chart_svg


# --- Per-ticker HTML blocks --------------------------------------------------

def _render_ticker_header(idx: int, data: dict[str, Any]) -> str:
    ticker = data.get("ticker") or "?"
    group = data.get("group") or ""
    company = _short_company(data.get("company_name"), max_len=42)
    company_html = (
        f'<span class="ticker-company">· {company}</span>' if company else ""
    )
    return (
        f'<header class="ticker-head">'
        f'<span class="ticker-num">{idx:02d}</span>'
        f'<span class="ticker-symbol">{ticker}</span>'
        f'<span class="ticker-group">({group})</span>'
        f"{company_html}"
        f"</header>"
    )


def _render_snapshot(data: dict[str, Any]) -> str:
    """Vertical 2-column Snapshot table (指标 | 数值, zebra-striped) — same
    shape as the markdown-emitted table the user liked, but with deterministic
    Python-formatted numbers and per-row CSS classes for sign coloring."""
    rows: list[tuple[str, str, str]] = []
    sector = data.get("sector") or ""
    industry = data.get("industry") or ""
    sector_str = " / ".join(filter(None, [sector, industry])) or "—"
    rows.append(("Sector / Industry", sector_str, ""))
    gap = data.get("gap_pct")
    if gap is None:
        gap_val = "—"
    else:
        prev = data.get("prev_close")
        last = data.get("last_price")
        if isinstance(prev, (int, float)) and isinstance(last, (int, float)):
            gap_val = f"{_fmt_pct(gap)}  (prev close ${prev:,.2f} → ${last:,.2f})"
        else:
            gap_val = _fmt_pct(gap)
    rows.append(("Gap", gap_val, _yoy_class(gap) if gap is not None else ""))
    rs = data.get("rs_percentile")
    rows.append(("RS Percentile", str(rs) if rs is not None else "—", ""))
    inst = data.get("institutional_holdings_pct")
    inst_str = f"{inst:.1f}%" if isinstance(inst, (int, float)) else "—"
    rows.append(("Inst. Hold", inst_str, ""))
    roe = data.get("roe_pct")
    if isinstance(roe, (int, float)) and not (isinstance(roe, float) and math.isnan(roe)):
        roe_str = f"{roe:.1f}%"
        roe_cls = "hot" if _is_hot(roe, ROE_HOT_PCT) else ""
    else:
        roe_str = "—"
        roe_cls = ""
    rows.append(("ROE", roe_str, roe_cls))
    rows.append(("Latest earnings date",
                 _fmt_date(data.get("latest_earnings_date")), ""))

    body_html = "".join(
        f'<tr><td class="snap-label">{lab}</td>'
        f'<td class="snap-value{(" " + cls) if cls else ""}">{val}</td></tr>'
        for lab, val, cls in rows
    )
    return (
        '<section class="snapshot">'
        '<table class="snapshot-table">'
        '<thead><tr><th>指标</th><th>数值</th></tr></thead>'
        f'<tbody>{body_html}</tbody>'
        '</table>'
        '</section>'
    )


EPS_DUAL_DIFF_THRESHOLD = 0.05  # show both GAAP and Adj when relative diff > 5%
EPS_DUAL_GAAP_FLOOR = 0.01      # avoid div-by-near-zero when GAAP ≈ 0


def _eps_usable(v: float | None) -> bool:
    return isinstance(v, (int, float)) and not (
        isinstance(v, float) and math.isnan(v)
    )


def _format_eps_dual(
    gaap: float | None,
    gaap_yoy: float | None,
    adj: float | None,
    adj_yoy: float | None,
) -> tuple[str, str]:
    """Return (value_str, yoy_str) for the Latest Quarter EPS row.

    Branching:
      - both usable + materially different → "$G GAAP / $A Adj" + "YoY GAAP X / Adj Y"
      - both usable + close                → "$G" + "YoY X"  (no GAAP/Adj suffix)
      - only GAAP                          → "$G" + "YoY X"  (no suffix — status quo)
      - only Adj                           → "$A Adj" + "YoY X (Adj)"
      - neither                            → "—" + "YoY —"
    """
    g_ok, a_ok = _eps_usable(gaap), _eps_usable(adj)
    if not g_ok and not a_ok:
        return ("—", "YoY —")
    if g_ok and not a_ok:
        return (f"${gaap:,.2f}", f"YoY {_fmt_pct(gaap_yoy)}")
    if a_ok and not g_ok:
        return (f"${adj:,.2f} Adj", f"YoY {_fmt_pct(adj_yoy)} (Adj)")
    denom = max(abs(gaap), EPS_DUAL_GAAP_FLOOR)
    if abs(adj - gaap) / denom <= EPS_DUAL_DIFF_THRESHOLD:
        return (f"${gaap:,.2f}", f"YoY {_fmt_pct(gaap_yoy)}")
    val_str = f"${gaap:,.2f} GAAP / ${adj:,.2f} Adj"
    yoy_str = f"YoY GAAP {_fmt_pct(gaap_yoy)} / Adj {_fmt_pct(adj_yoy)}"
    return (val_str, yoy_str)


def _is_eps_dual(d: dict[str, Any]) -> bool:
    """True iff this ticker's data would render as the dual GAAP / Adj form."""
    g = d.get("eps_latest_q")
    a = d.get("eps_latest_q_adj")
    if not _eps_usable(g) or not _eps_usable(a):
        return False
    denom = max(abs(g), EPS_DUAL_GAAP_FLOOR)
    return abs(a - g) / denom > EPS_DUAL_DIFF_THRESHOLD


def _render_quarterly(data: dict[str, Any]) -> str:
    eps_gaap = data.get("eps_latest_q")
    eps_gaap_yoy = data.get("eps_latest_q_yoy_pct")
    eps_adj = data.get("eps_latest_q_adj")
    eps_adj_yoy = data.get("eps_latest_q_adj_yoy_pct")
    # Yahoo's pre-computed MRQ YoY is the coarsest fallback — only used when
    # neither EDGAR (GAAP) nor yfinance earnings_dates (Adj) gave us a YoY.
    eps_yoy_src = ""
    if eps_gaap_yoy is None and not _eps_usable(eps_adj_yoy):
        eps_gaap_yoy = data.get("yahoo_earnings_growth_yoy_pct")
        eps_yoy_src = " (Yahoo)" if eps_gaap_yoy is not None else ""

    rev = data.get("revenue_latest_q")
    rev_yoy = data.get("revenue_latest_q_yoy_pct")
    if rev_yoy is None:
        rev_yoy = data.get("yahoo_revenue_growth_yoy_pct")
        rev_yoy_src = " (Yahoo)" if rev_yoy is not None else ""
    else:
        rev_yoy_src = ""

    eps_val_str, eps_yoy_str = _format_eps_dual(
        eps_gaap, eps_gaap_yoy, eps_adj, eps_adj_yoy
    )
    rev_str = _fmt_money(rev)

    # Hot pill triggers if EITHER GAAP or Adj YoY clears the threshold —
    # don't drown a hot Adj signal because GAAP looks tame, or vice versa.
    eps_hot = " hot" if (
        _is_hot(eps_gaap_yoy, EPS_HOT_PCT) or _is_hot(eps_adj_yoy, EPS_HOT_PCT)
    ) else ""
    rev_hot = " hot" if _is_hot(rev_yoy, REVENUE_HOT_PCT) else ""

    # Pill positive/negative class follows the larger-magnitude YoY so a hot
    # Adj number colours the pill correctly even when GAAP YoY is None.
    pill_yoy = eps_gaap_yoy if eps_gaap_yoy is not None else eps_adj_yoy
    if (
        _eps_usable(eps_adj_yoy) and _eps_usable(eps_gaap_yoy)
        and abs(eps_adj_yoy) > abs(eps_gaap_yoy)
    ):
        pill_yoy = eps_adj_yoy
    eps_pill = (
        f'<div class="yoy {_yoy_class(pill_yoy)}{eps_hot}">{eps_yoy_str}{eps_yoy_src}</div>'
    )
    rev_pill = (
        f'<div class="yoy {_yoy_class(rev_yoy)}{rev_hot}">YoY {_fmt_pct(rev_yoy)}{rev_yoy_src}</div>'
    )
    return (
        f'<section class="quarterly">'
        f'<div><div class="qtr-label">Latest Quarter — EPS</div>'
        f'<div class="metric-value{eps_hot}">{eps_val_str}</div>{eps_pill}</div>'
        f'<div><div class="qtr-label">Latest Quarter — Revenue</div>'
        f'<div class="metric-value{rev_hot}">{rev_str}</div>{rev_pill}</div>'
        f"</section>"
    )


def _render_quarterly_trend(data: dict[str, Any]) -> str:
    """Past 4 quarters of YoY growth — shows whether quarterly EPS / Revenue
    YoY is accelerating, decelerating, or rolling over. EDGAR provides up to
    8 quarterly periods (= 4 YoY pairs); yfinance fallback fills less."""
    eps = data.get("quarterly_eps_yoy_4q") or [None] * 4
    rev = data.get("quarterly_revenue_yoy_4q") or [None] * 4
    eps_lbl = data.get("quarterly_eps_yoy_4q_labels") or [""] * 4
    rev_lbl = data.get("quarterly_revenue_yoy_4q_labels") or [""] * 4
    # HK tickers don't go through EDGAR and yfinance's HK quarterly statements
    # carry < 8 quarters, so 4-quarter YoY is unreachable. Rather than render a
    # row of empty "n/a" placeholders, drop the section entirely — the latest-
    # quarter Yahoo YoY box above already covers the quarterly signal.
    if all(v is None for v in eps) and all(v is None for v in rev):
        return ""
    labels = []
    for i, l in enumerate(eps_lbl):
        if l:
            labels.append(l)
        elif rev_lbl[i]:
            labels.append(rev_lbl[i])
        else:
            n = len(eps_lbl) - 1 - i
            labels.append("Latest" if n == 0 else f"−{n}Q")
    return (
        f'<section class="annual">'
        f'<div class="annual-title">Past 4 Quarters — YoY Growth</div>'
        f'<div class="chart-row">'
        f'<div class="chart-name">EPS YoY</div>'
        f'{_line_chart_svg(eps, labels)}</div>'
        f'<div class="chart-row">'
        f'<div class="chart-name">Rev. YoY</div>'
        f'{_line_chart_svg(rev, labels)}</div>'
        f"</section>"
    )


def _render_annual_yoy(data: dict[str, Any]) -> str:
    eps = data.get("annual_eps_yoy_5y") or [None] * 5
    rev = data.get("annual_revenue_yoy_5y") or [None] * 5
    labels = ["FY-5", "FY-4", "FY-3", "FY-2", "FY-1"]
    return (
        f'<section class="annual">'
        f'<div class="annual-title">5-Year Annual Earnings Increases (YoY)</div>'
        f'<div class="chart-row">'
        f'<div class="chart-name">EPS YoY</div>'
        f'{_line_chart_svg(eps, labels)}</div>'
        f'<div class="chart-row">'
        f'<div class="chart-name">Rev. YoY</div>'
        f'{_line_chart_svg(rev, labels)}</div>'
        f"</section>"
    )


# Section names whose H3 should be marked with an "emphasis" class so the CSS
# can highlight them. These map to the user-prioritized analysis legs:
# fundamentals, policy/government support, bottom-line CANSLIM judgement.
_EMPHASIS_HEADINGS = ("基本面", "政策", "综合判断")


def _emphasize_prose_html(html: str) -> str:
    """Wrap heading + following content in <section class="emphasis"> for the
    user-prioritized prose legs. Done by string replacement on the rendered
    H3s — the markdown package doesn't expose a hook for per-heading classes."""
    import re
    # Find each emphasized H3 and inject a class on the heading element.
    # Markdown lib emits `<h3>基本面 / 财报</h3>` (no attributes), so a simple
    # regex is safe.
    for needle in _EMPHASIS_HEADINGS:
        pattern = re.compile(rf'<h3>([^<]*{re.escape(needle)}[^<]*)</h3>')
        html = pattern.sub(r'<h3 class="emph">\1</h3>', html)
    return html


def _has_no_fundamentals(d: dict[str, Any]) -> bool:
    """True when EVERY EDGAR/yfinance fundamentals field is None or all-None.
    Used to swap the empty Latest-Q + 4q + 5y tables for a friendly banner
    on fresh-IPO tickers that have no usable financial history yet."""
    scalars = ("eps_latest_q", "revenue_latest_q",
               "eps_latest_q_yoy_pct", "revenue_latest_q_yoy_pct",
               "yahoo_revenue_growth_yoy_pct", "yahoo_earnings_growth_yoy_pct")
    if any(d.get(k) is not None for k in scalars):
        return False
    for k in ("annual_eps_yoy_5y", "annual_revenue_yoy_5y",
              "quarterly_eps_yoy_4q", "quarterly_revenue_yoy_4q"):
        arr = d.get(k) or []
        if any(v is not None for v in arr):
            return False
    return True


def _render_ipo_no_data_banner(d: dict[str, Any]) -> str:
    ipo_iso = d.get("ipo_date") or ""
    year = ipo_iso[:4] if len(ipo_iso) >= 4 and ipo_iso[:4].isdigit() else None
    when = f"{year} 年" if year else "新"
    date_suffix = f"(首日交易 {ipo_iso})" if ipo_iso else ""
    return (
        f'<section class="ipo-no-data">'
        f'<strong>{when}上市</strong>的 IPO 公司{date_suffix}, '
        f'暂无可用的 EPS / Revenue 历史数据.'
        f'</section>'
    )


def _render_md_ipo_no_data_banner(d: dict[str, Any]) -> str:
    ipo_iso = d.get("ipo_date") or ""
    year = ipo_iso[:4] if len(ipo_iso) >= 4 and ipo_iso[:4].isdigit() else None
    when = f"**{year} 年**" if year else "**新**"
    date_suffix = f"(首日交易 {ipo_iso})" if ipo_iso else ""
    return f"{when}上市的 IPO 公司{date_suffix}, 暂无可用的 EPS / Revenue 历史数据.\n\n"


def _render_prose(prose_md: str) -> str:
    if not prose_md or not prose_md.strip():
        return '<div class="failure">[no prose returned]</div>'
    if "[配置错误" in prose_md or "[分析失败" in prose_md:
        return f'<div class="failure">{prose_md}</div>'
    body = md_lib.markdown(
        prose_md,
        extensions=["fenced_code", "sane_lists"],
    )
    body = _emphasize_prose_html(body)
    return f'<div class="prose">{body}</div>'


def _render_ticker_block(idx: int, data: dict[str, Any], prose_md: str) -> str:
    ticker = data.get("ticker") or "?"
    is_ipo_no_data = (data.get("group") == "IPO") and _has_no_fundamentals(data)
    if is_ipo_no_data:
        fundamentals_html = _render_ipo_no_data_banner(data)
    else:
        fundamentals_html = (
            f'{_render_quarterly(data)}'
            f'{_render_quarterly_trend(data)}'
            f'{_render_annual_yoy(data)}'
        )
    return (
        f'<article class="ticker" id="t-{ticker}">'
        f'{_render_ticker_header(idx, data)}'
        f'<div class="ticker-body">'
        f'{_render_snapshot(data)}'
        f'{fundamentals_html}'
        f'{_render_prose(prose_md)}'
        f"</div>"
        f"</article>"
    )


def _render_index(enriched: list[dict[str, Any]]) -> str:
    if not enriched:
        return ""
    parts: list[str] = []
    for i, d in enumerate(enriched, 1):
        ticker = d.get("ticker") or "?"
        group = d.get("group") or ""
        parts.append(
            f'<a href="#t-{ticker}">'
            f'<span class="num">{i:02d}</span>'
            f'<span class="sym">{ticker}</span>'
            f'<span class="grp">{group}</span></a>'
        )
    return f'<nav class="index">{"".join(parts)}</nav>'


def _render_truncated(truncated: list[tuple[str, str]]) -> str:
    if not truncated:
        return ""
    items = "".join(
        f"<div>{t} <span>{g}</span></div>" for t, g in truncated
    )
    return (
        f'<section class="truncated">'
        f'<h2>Truncated (cap = 50)</h2>'
        f'<div class="lst">{items}</div>'
        f"</section>"
    )


# --- Cover page --------------------------------------------------------------

# Pretty labels for the per-category breakdown on the cover. Entries not
# listed fall back to the raw key (e.g. "Shorts" stays "Shorts"). Keep this
# in sync with the [futu.groups] keys / Finviz screener names.
_GROUP_DISPLAY_LABEL: dict[str, str] = {
    "EarningsGap": "Earnings Gap",
    "HighVolume": "High Volume",
    "GapUp": "Gap Up",
    "NewHigh52W": "52-Week High",
    "TopGainers": "Top Gainers",
    "Leaders": "Leaders",
    "Shorts": "Shorts",
    "RS": "Relative Strength",
    "IPO": "IPO",
    "HKEarningsGap": "Earnings Gap",
    "HKHighVolume": "High Volume",
    "HKGapUp": "Gap Up",
    "HKLeaders": "Leaders",
    "HKRS": "Relative Strength",
    "HKShorts": "Shorts",
    "HKIPO": "IPO",
    "MorningGap": "Morning Gap",
    "MorningGapPre": "Pre-Market Gap",
}

_MARKET_FULL_NAME: dict[str, str] = {
    "us": "美股",
    "hk": "港股",
}

# Map Python weekday() (0=Monday) to single-character Chinese day-of-week.
_WEEKDAY_CN = "一二三四五六日"


def _group_breakdown(
    enriched: list[dict[str, Any]],
) -> list[tuple[str, list[str]]]:
    """Preserve the first-seen order from `enriched` (the ranker has
    already arranged it in canonical priority order). Empty groups are
    naturally absent because they contribute no rows."""
    groups: dict[str, list[str]] = {}
    for d in enriched:
        g = d.get("group") or "Other"
        groups.setdefault(g, []).append(d.get("ticker") or "?")
    return [
        (_GROUP_DISPLAY_LABEL.get(g, g), tickers)
        for g, tickers in groups.items()
    ]


def _render_cover(
    market: str,
    date_iso: str,
    enriched: list[dict[str, Any]],
    truncated: list[tuple[str, str]],
    generated_at: datetime,
    model_label: str | None,
) -> str:
    """Editorial cover (Chinese with English proper nouns). Methodology
    authors and screen category names stay in English; framework and
    structural copy is in Chinese. Sits flush to the viewport edges.

    Hierarchy: methodology + curated stock count = hero (the actual
    selling point). Date is demoted to the eyebrow strip + colophon
    timestamp; it's metadata, not the value proposition.
    """
    try:
        report_date = datetime.fromisoformat(date_iso).date()
    except (ValueError, TypeError):
        report_date = generated_at.date()

    market_full = _MARKET_FULL_NAME.get(market.lower(), market.upper())
    issue_no = report_date.timetuple().tm_yday
    weekday_cn = "周" + _WEEKDAY_CN[report_date.weekday()]
    eyebrow_date = (
        f"{report_date.year}.{report_date.month:02d}.{report_date.day:02d} · {weekday_cn}"
    )

    breakdown = _group_breakdown(enriched)
    cats_count = len(breakdown)
    sec_count = len(enriched)

    headline = "新晋强势榜"

    # The actual selling point gets a dedicated visual plate — large
    # monospace tickers, click-to-anchor into the per-ticker section.
    plate_items = [d_.get("ticker") or "?" for d_ in enriched]
    if plate_items:
        plate_html = "".join(
            f'<a href="#t-{t}" class="cover-plate-ticker">{t}</a>'
            for t in plate_items
        )
    else:
        plate_html = (
            '<span class="cover-plate-empty">本期暂无符合条件的股票</span>'
        )

    breakdown_html = "".join(
        f'<div class="cover-cat">'
        f'<div class="cover-cat-head">'
        f'<span class="cover-cat-name">{label}</span>'
        f'<span class="cover-cat-rule"></span>'
        f'<span class="cover-cat-count">{len(tickers)} 个</span>'
        f'</div>'
        f'<div class="cover-cat-tickers">{" · ".join(tickers)}</div>'
        f'</div>'
        for label, tickers in breakdown
    )
    if not breakdown_html:
        breakdown_html = (
            '<div class="cover-cat-tickers" style="color:var(--muted)">—</div>'
        )

    gen_time_str = generated_at.strftime("%Y-%m-%d %H:%M %Z")
    model_line = model_label or "AI 分析模型"
    if model_label:
        strap = (
            "基于 <strong>Oliver Kell</strong> 股价阶段分析与 "
            "<strong>Kristjan Kullamägi</strong> 1-6 个月强势表现股票 "
            "+ 抛物线做空策略筛选，经跨日去重，<strong class='cover-emph'>仅收录首次入选</strong>，"
            f"由 <strong>{model_label}</strong> 撰写 CANSLIM 基本面简报。"
        )
    else:
        strap = (
            "基于 <strong>Oliver Kell</strong> 股价阶段分析与 "
            "<strong>Kristjan Kullamägi</strong> 1-6 个月强势表现股票 "
            "+ 抛物线做空策略筛选，经跨日去重，<strong class='cover-emph'>仅收录首次入选</strong>，"
            "逐个生成 CANSLIM 基本面简报。"
        )

    return (
        '<section class="cover">'
        '<div class="cover-inner">'
        # --- top eyebrow: small Daily-Equities-Scan label, hairline,
        #     issue number + date stamp on the right
        '<header class="cover-eyebrow">'
        f'<span class="cover-eyebrow-label">每日股票精选 · {market_full}</span>'
        '<span class="cover-eyebrow-rule"></span>'
        f'<span class="cover-eyebrow-issue">第 {issue_no:03d} 期 · {eyebrow_date}</span>'
        '</header>'
        # --- hero: methodology headline + descriptive strap. The
        #     headline names the two methodology authors (this is the
        #     value prop); the strap expands with full descriptors and
        #     the LLM model attribution.
        '<div class="cover-hero">'
        f'<h1 class="cover-headline">{headline}</h1>'
        f'<p class="cover-strap">{strap}</p>'
        '</div>'
        # --- ticker plate: the actual deliverable, foregrounded as the
        #     visual second hero. Each ticker links to its in-document
        #     anchor for click-through.
        f'<div class="cover-plate">{plate_html}</div>'
        # --- per-category breakdown ---
        '<section class="cover-section">'
        '<header class="cover-section-head">'
        '<span class="cover-section-label">本期清单</span>'
        '<span class="cover-section-rule"></span>'
        f'<span class="cover-section-meta">{sec_count} 个 · {cats_count} 类</span>'
        '</header>'
        f'<div class="cover-breakdown">{breakdown_html}</div>'
        '</section>'
        # --- colophon: methodology attribution (left, larger) plus
        #     metadata rows (right, smaller). Reads like a publication
        #     masthead: gives the buyer the full provenance trail.
        '<footer class="cover-colophon">'
        '<div class="cover-colophon-meth">'
        '<div class="cover-colophon-key">选股方法</div>'
        '<div class="cover-colophon-meth-list">'
        '<div><span class="cover-meth-author">Oliver Kell</span> · '
        'Stages of Stock Movement，五种 long-side 形态'
        '（Earnings Gap、High Volume、Gap Up、52-Week High、Top Gainers），'
        '叠加弱市 RS 领涨股扫描。</div>'
        '<div><span class="cover-meth-author">Kristjan Kullamägi</span> · '
        '1-6 个月的强势增长股票 Leaders + 抛物线 blow-off 做空规则，'
        '应用于 Shorts 类别。</div>'
        '<div><span class="cover-meth-author">William O&apos;Neil</span> · '
        'CANSLIM 成长股框架，驱动每个股票的基本面简报。</div>'
        '</div>'
        '</div>'
        '<div class="cover-colophon-meta">'
        '<div class="cover-colophon-row">'
        '<span class="cover-colophon-key">数据来源</span>'
        '<span class="cover-colophon-val">SEC EDGAR · yfinance · '
        'IBD RS · Futu OpenAPI</span>'
        '</div>'
        '<div class="cover-colophon-row">'
        '<span class="cover-colophon-key">生成模型</span>'
        f'<span class="cover-colophon-val">{model_line}</span>'
        '</div>'
        '<div class="cover-colophon-row">'
        '<span class="cover-colophon-key">生成时间</span>'
        f'<span class="cover-colophon-val">{gen_time_str}</span>'
        '</div>'
        '</div>'
        '</footer>'
        # --- legal disclaimer (small italic, bottom-right).
        '<p class="cover-disclaimer">'
        '本报告由算法与 AI 模型自动生成，内容仅供研究与参考，'
        '<strong>不构成任何投资建议或要约</strong>。'
        '股市有风险，决策需谨慎；读者应自行承担投资决策的全部责任，'
        '本作者不对因使用本内容而产生的任何损失负责。'
        '</p>'
        '</div>'
        '</section>'
    )


# --- Document-level renderers ------------------------------------------------

def render_html_document(
    market: str,
    date_iso: str,
    enriched: list[dict[str, Any]],
    prose_sections: list[str],
    truncated: list[tuple[str, str]],
    generated_at: datetime,
    model_label: str | None = None,
) -> str:
    market_label = market.upper()
    analyzed_count = len(enriched)
    total = analyzed_count + len(truncated)
    blocks = [
        _render_ticker_block(i + 1, d, p)
        for i, (d, p) in enumerate(zip(enriched, prose_sections))
    ]
    index_html = _render_index(enriched)
    truncated_html = _render_truncated(truncated)
    masthead = (
        f'<header class="masthead">'
        f'<h1 class="masthead-title">Daily Scan · {date_iso}</h1>'
        f'<div class="masthead-meta">'
        f'<span><strong>{market_label}</strong></span>'
        f'<span>analyzed <strong>{analyzed_count}</strong> · '
        f'truncated <strong>{len(truncated)}</strong> · total <strong>{total}</strong></span>'
        f'<span>{generated_at.strftime("%Y-%m-%d %H:%M %Z")}</span>'
        f"</div>"
        f"</header>"
    )
    footer_html = (
        f'<footer class="report-footer">'
        f'Generated by {model_label} · {date_iso}'
        f'</footer>'
    ) if model_label else ""
    eps_footnote_html = (
        '<p class="eps-footnote" style="font-size:0.85em;color:#666;'
        'margin-top:1em;">EPS shows GAAP / Adjusted when they differ '
        'materially. GAAP is from SEC 10-Q. Adjusted is the consensus '
        'headline (yfinance "Reported EPS").</p>'
    ) if any(_is_eps_dual(d) for d in enriched) else ""
    cover_html = _render_cover(
        market=market,
        date_iso=date_iso,
        enriched=enriched,
        truncated=truncated,
        generated_at=generated_at,
        model_label=model_label,
    )
    body = (
        f'{cover_html}'
        f'<div class="sheet">{masthead}{index_html}'
        f'{"".join(blocks)}{truncated_html}{eps_footnote_html}{footer_html}</div>'
    )
    title = f"Daily Scan — {date_iso} ({market_label})"
    return (
        f"<!doctype html>\n<html lang=\"zh\">\n<head>\n"
        f'  <meta charset="utf-8">\n'
        f'  <meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"  <title>{title}</title>\n"
        f"  <style>{INLINE_CSS}</style>\n"
        f"</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def render_markdown_document(
    market: str,
    date_iso: str,
    enriched: list[dict[str, Any]],
    prose_sections: list[str],
    truncated: list[tuple[str, str]],
    generated_at: datetime,
    model_label: str | None = None,
) -> str:
    """Plain-text Markdown twin of the HTML report. Suitable for grep / diff /
    archival; the .html file is the rendered artifact."""
    market_label = market.upper()
    analyzed_count = len(enriched)
    total = analyzed_count + len(truncated)
    parts: list[str] = []
    parts.append(f"# Daily Scan — {date_iso} ({market_label})\n\n")
    parts.append(
        f"Total: {total}  ·  Analyzed: {analyzed_count}  ·  Truncated: {len(truncated)}\n"
    )
    parts.append(
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
    )
    parts.append("---\n\n")
    for i, (d, prose) in enumerate(zip(enriched, prose_sections), 1):
        parts.append(_render_md_ticker(i, d, prose))
        parts.append("\n---\n\n")
    if truncated:
        parts.append("## Truncated (cap = 50)\n\n")
        for t, g in truncated:
            parts.append(f"- {t} ({g})\n")
    if model_label:
        parts.append(f"\n---\n\n*Generated by {model_label} · {date_iso}*\n")
    return "".join(parts)


def _render_md_ticker(idx: int, d: dict[str, Any], prose: str) -> str:
    ticker = d.get("ticker") or "?"
    company = d.get("company_name") or ""
    exchange = d.get("exchange") or ""
    group = d.get("group") or ""
    sector = d.get("sector") or ""
    industry = d.get("industry") or ""
    head = f"## {idx:02d}. {ticker}"
    if company:
        head += f" — {company}"
    head += f"  ({exchange} · {group}"
    if sector or industry:
        head += f" · {' / '.join(filter(None, [sector, industry]))}"
    head += ")\n\n"

    inst = d.get("institutional_holdings_pct")
    inst_usable = isinstance(inst, (int, float)) and not (isinstance(inst, float) and math.isnan(inst))
    inst_str = f"{inst:.1f}%" if inst_usable else "—"
    roe = d.get("roe_pct")
    roe_usable = isinstance(roe, (int, float)) and not (isinstance(roe, float) and math.isnan(roe))
    if roe_usable:
        roe_str = f"**{roe:.1f}%**" if _is_hot(roe, ROE_HOT_PCT) else f"{roe:.1f}%"
    else:
        roe_str = "—"
    snap = (
        f"| Market Cap | Price | Gap | RS | Inst. Hold | ROE | Earnings Date |\n"
        f"|---|---|---|---|---|---|---|\n"
        f"| {_fmt_money(d.get('market_cap'))} "
        f"| {_fmt_money(d.get('last_price'))} "
        f"| {_fmt_pct(d.get('gap_pct'))} "
        f"| {d.get('rs_percentile') if d.get('rs_percentile') is not None else '—'} "
        f"| {inst_str} "
        f"| {roe_str} "
        f"| {_fmt_date(d.get('latest_earnings_date'))} |\n\n"
    )

    if d.get("group") == "IPO" and _has_no_fundamentals(d):
        return head + snap + _render_md_ipo_no_data_banner(d) + (prose.rstrip() + "\n")

    eps_gaap = d.get("eps_latest_q")
    eps_gaap_yoy = d.get("eps_latest_q_yoy_pct")
    eps_adj = d.get("eps_latest_q_adj")
    eps_adj_yoy = d.get("eps_latest_q_adj_yoy_pct")
    if eps_gaap_yoy is None and not _eps_usable(eps_adj_yoy):
        eps_gaap_yoy = d.get("yahoo_earnings_growth_yoy_pct")
        eps_yoy_src = " (Yahoo)" if eps_gaap_yoy is not None else ""
    else:
        eps_yoy_src = ""
    rev_str = _fmt_money(d.get("revenue_latest_q"))
    rev_yoy = d.get("revenue_latest_q_yoy_pct")
    if rev_yoy is None:
        rev_yoy = d.get("yahoo_revenue_growth_yoy_pct")
        rev_src = " (Yahoo)" if rev_yoy is not None else ""
    else:
        rev_src = ""
    eps_val_str, eps_yoy_str = _format_eps_dual(
        eps_gaap, eps_gaap_yoy, eps_adj, eps_adj_yoy
    )
    eps_seg = f"EPS {eps_val_str} ({eps_yoy_str}{eps_yoy_src})"
    rev_seg = f"Revenue {rev_str} (YoY {_fmt_pct(rev_yoy)}{rev_src})"
    if _is_hot(eps_gaap_yoy, EPS_HOT_PCT) or _is_hot(eps_adj_yoy, EPS_HOT_PCT):
        eps_seg = f"**{eps_seg}**"
    if _is_hot(rev_yoy, REVENUE_HOT_PCT):
        rev_seg = f"**{rev_seg}**"
    qtr = f"**Latest Quarter:**  {eps_seg}  ·  {rev_seg}\n\n"

    eps_5y = d.get("annual_eps_yoy_5y") or [None] * 5
    rev_5y = d.get("annual_revenue_yoy_5y") or [None] * 5
    annual = (
        "| Year | FY−5 | FY−4 | FY−3 | FY−2 | FY−1 |\n"
        "|---|---|---|---|---|---|\n"
        f"| EPS YoY | {_fmt_pct(eps_5y[0])} | {_fmt_pct(eps_5y[1])} | {_fmt_pct(eps_5y[2])} | {_fmt_pct(eps_5y[3])} | {_fmt_pct(eps_5y[4])} |\n"
        f"| Rev. YoY | {_fmt_pct(rev_5y[0])} | {_fmt_pct(rev_5y[1])} | {_fmt_pct(rev_5y[2])} | {_fmt_pct(rev_5y[3])} | {_fmt_pct(rev_5y[4])} |\n\n"
    )

    quarterly_md = _render_md_quarterly_trend(d)

    return head + snap + qtr + annual + quarterly_md + (prose.rstrip() + "\n")


def _render_md_quarterly_trend(d: dict[str, Any]) -> str:
    """Markdown 4-quarter YoY table (oldest→newest). Bolds the rightmost cell
    of either row when its YoY exceeds the CANSLIM "C" hot threshold
    (Rev > 20%, EPS > 25%). Returns "" when the row is unrecoverable
    (HK tickers with sparse statements; matches HTML behavior)."""
    eps = d.get("quarterly_eps_yoy_4q") or [None] * 4
    rev = d.get("quarterly_revenue_yoy_4q") or [None] * 4
    eps_lbl = d.get("quarterly_eps_yoy_4q_labels") or [""] * 4
    rev_lbl = d.get("quarterly_revenue_yoy_4q_labels") or [""] * 4
    if all(v is None for v in eps) and all(v is None for v in rev):
        return ""
    headers: list[str] = []
    for i in range(4):
        h = eps_lbl[i] or rev_lbl[i]
        if not h:
            n = 3 - i
            h = "Latest" if n == 0 else f"−{n}Q"
        headers.append(h)

    def cell(value: float | None, threshold: float, is_latest: bool) -> str:
        s = _fmt_pct(value)
        if is_latest and _is_hot(value, threshold):
            return f"**{s}**"
        return s

    eps_cells = " | ".join(cell(eps[i], EPS_HOT_PCT, i == 3) for i in range(4))
    rev_cells = " | ".join(cell(rev[i], REVENUE_HOT_PCT, i == 3) for i in range(4))
    sep = "|".join(["---"] * 5)
    return (
        f"| Quarter | {' | '.join(headers)} |\n"
        f"|{sep}|\n"
        f"| EPS YoY | {eps_cells} |\n"
        f"| Rev. YoY | {rev_cells} |\n\n"
    )


# --- File output -------------------------------------------------------------

def write_report_files(
    out_dir: Path,
    date_stem: str,
    market: str,
    enriched: list[dict[str, Any]],
    prose_sections: list[str],
    truncated: list[tuple[str, str]],
    generated_at: datetime,
    date_iso: str,
    model_label: str | None = None,
) -> Path:
    """Write the .html report (the only deliverable); return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{date_stem}_{market}.html"
    html_text = render_html_document(
        market=market, date_iso=date_iso, enriched=enriched,
        prose_sections=prose_sections, truncated=truncated,
        generated_at=generated_at, model_label=model_label,
    )
    html_path.write_text(html_text, encoding="utf-8")
    return html_path


# --- Backwards-compat shims (older tests still call these) -------------------

def render_markdown(
    market: str,
    date_iso: str,
    analyzed_count: int,
    truncated: list[tuple[str, str]],
    sections: list[str],
    generated_at: datetime,
) -> str:
    """Legacy: header + flat sections + truncated. Kept so older tests pass.
    New code should call render_markdown_document(...)."""
    market_label = market.upper()
    total = analyzed_count + len(truncated)
    parts: list[str] = []
    parts.append(f"# Scan Report — {date_iso} ({market_label})\n")
    parts.append(
        f"Total new tickers: {total} "
        f"(analyzed {analyzed_count}, truncated {len(truncated)})\n"
    )
    parts.append(f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    parts.append("\n---\n")
    for s in sections:
        parts.append(s.rstrip() + "\n")
        parts.append("\n---\n")
    if truncated:
        parts.append("\n## Truncated (cap = 50)\n")
        for t, g in truncated:
            parts.append(f"- {t} ({g})\n")
    return "".join(parts)


def markdown_to_html(markdown_text: str, page_title: str) -> str:
    """Legacy: render free-form Markdown into the editorial HTML shell."""
    body = md_lib.markdown(
        markdown_text, extensions=["tables", "fenced_code", "sane_lists"]
    )
    return (
        f"<!doctype html>\n<html lang=\"zh\">\n<head>\n"
        f'  <meta charset="utf-8">\n'
        f"  <title>{page_title}</title>\n"
        f"  <style>{INLINE_CSS}</style>\n"
        f"</head>\n<body><div class=\"sheet prose\">{body}</div></body>\n</html>\n"
    )
