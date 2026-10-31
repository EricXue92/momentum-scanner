"""Per-ETF "strongest holdings this month" line for the ETF RS snapshots.

For every ETF in ``[etf_rs.tickers]`` the us-eod ETF step appends one
indented continuation line under the ETF's row in both
``<date>_ETF_rs_3m.txt`` and ``<date>_ETF_rs_1m.txt``::

    9. OIH 新 - 油服 | SLB、BKR、...
       1M强: SEI > RIG > NE > SLB > HAL

— the ETF's **US-listed** constituents ranked by the same 1M weekly ladder
the ``_1m`` file uses (``us_rs_3m.WEIGHTS_1M_BARS``, benchmark-relative to
SPY), strongest first, top ``top_n`` (5). Ranking is *within* the ETF's own
constituents (no percentile — a dozen names is not a distribution).

Constituent source chain, per ETF, first hit wins (``fetch_constituents``):

1. today's cache ``output/state/etf_holdings_<YYYY_MM_DD>.json`` (a
   same-day rerun never goes online);
2. stockanalysis.com ``/etf/<slug>/holdings/`` — SSR table, the free tier
   shows the top 25 by weight (``parse_holdings_html``);
3. the most recent earlier cache within ``cache_max_age_days`` (7);
4. yfinance ``funds_data.top_holdings`` (top 10) — flagged ``yf_top10`` so
   the line can say ``(前十大)``;
5. nothing → no continuation line for that ETF.

Only stockanalysis results are cached (a fallback must never be walked back
to as if it were the real list). Foreign constituents (``TLV:NXSN``,
``688836.SZ`` …) are kept in the cache but dropped before ranking
(``is_us_symbol``). ``BRK.B`` is requested from yfinance as ``BRK-B`` and
displayed as ``BRK.B``.

Soft by design: ``etf_rs.run_etf_rs`` wraps ``compute_extras`` in a
try/except — any failure here costs only the continuation lines, never the
ranking files or the EOD exit code. The cloud Pages build passes
``holdings_rs=False`` and never runs this module.
Spec: docs/superpowers/specs/2026-10-31-etf-holdings-1m-rs-design.md
"""

from __future__ import annotations

import html as _html
import logging
import re
from typing import NamedTuple

logger = logging.getLogger("momentum_scanner")

_LABEL = "ETF 持仓RS"

#: Plain US listing: 1-5 capitals, optional one-letter share class (``BRK.B``).
US_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


class Constituents(NamedTuple):
    symbols: list[str]   # as parsed (page order, foreign codes included)
    source: str          # "stockanalysis" | "stockanalysis-cache:<YYYY-MM-DD>" | "yf_top10"


# --- parsing --------------------------------------------------------------

_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _cell_text(raw: str) -> str:
    return _html.unescape(_TAG_RE.sub("", raw)).strip()


def parse_holdings_html(page: str) -> list[str] | None:
    """Symbol column of the first ``<table>`` on a stockanalysis holdings
    page, in page order (weight-descending). ``None`` when there is no table
    or no data row — callers must treat that as a failed fetch, never as an
    empty ETF."""
    m = _TABLE_RE.search(page or "")
    if not m:
        return None
    rows = _ROW_RE.findall(m.group(0))
    symbol_col = 1
    symbols: list[str] = []
    for row in rows:
        cells = _CELL_RE.findall(row)
        if not cells:
            continue
        texts = [_cell_text(c) for c in cells]
        if "<th" in row.lower():
            if "Symbol" in texts:
                symbol_col = texts.index("Symbol")
            continue
        if len(texts) <= symbol_col:
            continue
        sym = texts[symbol_col]
        if sym:
            symbols.append(sym)
    return symbols or None


# --- symbols --------------------------------------------------------------

def is_us_symbol(sym: str) -> bool:
    return bool(US_SYMBOL_RE.match(sym or ""))


def to_yf_symbol(sym: str) -> str:
    """Display → yfinance request symbol (``BRK.B`` → ``BRK-B``)."""
    return sym.replace(".", "-")


def from_yf_symbol(sym: str) -> str:
    """yfinance → display symbol (``BRK-B`` → ``BRK.B``)."""
    return sym.replace("-", ".")
