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
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, NamedTuple

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


# --- constituent sources --------------------------------------------------

HOLDINGS_URL = "https://stockanalysis.com/etf/{slug}/holdings/"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
_SRC_SA = "stockanalysis"
_SRC_YF = "yf_top10"


def cache_path(output_dir: Path, day: date) -> Path:
    return output_dir / "state" / f"etf_holdings_{day.strftime('%Y_%m_%d')}.json"


def load_cache(output_dir: Path, day: date) -> dict[str, Constituents]:
    """``{etf: Constituents}`` from that day's cache; missing / unreadable /
    malformed → ``{}`` (the chain just moves on)."""
    p = cache_path(output_dir, day)
    try:
        raw = json.loads(p.read_text())
        out: dict[str, Constituents] = {}
        for etf, rec in raw.items():
            syms = rec.get("symbols")
            if isinstance(syms, list) and syms:
                out[str(etf)] = Constituents([str(x) for x in syms], str(rec.get("source", _SRC_SA)))
        return out
    except FileNotFoundError:
        return {}
    except Exception as e:  # corrupt JSON, wrong shape
        logger.warning(f"[{_LABEL}] ignoring unreadable cache {p.name}: {e}")
        return {}


def save_cache(output_dir: Path, day: date, data: dict[str, Constituents]) -> None:
    """Persist only the stockanalysis-sourced entries (a fallback list must
    never be walked back to as the real one). Soft: write failure = WARNING."""
    keep = {etf: {"symbols": c.symbols, "source": c.source}
            for etf, c in data.items() if c.source == _SRC_SA and c.symbols}
    if not keep:
        return
    p = cache_path(output_dir, day)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(keep, ensure_ascii=False, indent=0))
    except OSError as e:
        logger.warning(f"[{_LABEL}] cannot write cache {p}: {e}")


def _fetch_page(url: str, timeout: float = 15.0) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"[{_LABEL}] {url} -> HTTP {resp.status}")
                return None
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning(f"[{_LABEL}] {url} failed: {e}")
        return None


def _yf_top_holdings(etf: str) -> list[str] | None:
    import yfinance as yf
    df = yf.Ticker(etf).funds_data.top_holdings
    if df is None or df.empty:
        return None
    return [str(s) for s in df.index]


def fetch_constituents(
    etfs: list[str],
    today: date,
    output_dir: Path,
    cfg: dict,
    *,
    fetch_page: Callable[[str], str | None] | None = None,
    yf_top10: Callable[[str], list[str] | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Constituents]:
    """Source chain per ETF (first hit wins): today's cache → stockanalysis
    live → earlier cache ≤ ``cache_max_age_days`` → yfinance top-10 → absent.
    Writes today's cache (stockanalysis entries only) once at the end."""
    fetch_page = fetch_page or _fetch_page
    yf_top10 = yf_top10 or _yf_top_holdings
    max_age = int(cfg.get("cache_max_age_days", 7))
    delay = float(cfg.get("request_delay_sec", 0.8))

    today_cache = load_cache(output_dir, today)
    older: dict[int, dict[str, Constituents]] = {}   # lazily loaded, keyed by days back
    result: dict[str, Constituents] = {}
    fetched_live = False
    for etf in etfs:
        if etf in today_cache:
            result[etf] = today_cache[etf]
            logger.info(f"[{_LABEL}] {etf}: {len(result[etf].symbols)} symbols (cache, today)")
            continue
        # 2. live
        if fetched_live and delay > 0:
            sleep(delay)
        page = fetch_page(HOLDINGS_URL.format(slug=etf.lower()))
        fetched_live = True
        syms = parse_holdings_html(page) if page else None
        if syms:
            result[etf] = Constituents(syms, _SRC_SA)
            logger.info(f"[{_LABEL}] {etf}: {len(syms)} symbols (stockanalysis)")
            continue
        logger.warning(f"[{_LABEL}] {etf}: stockanalysis gave no holdings table")
        # 3. walk back
        hit = None
        for k in range(1, max_age + 1):
            if k not in older:
                older[k] = load_cache(output_dir, today - timedelta(days=k))
            c = older[k].get(etf)
            if c and c.source == _SRC_SA:
                hit = (today - timedelta(days=k), c)
                break
        if hit:
            day, c = hit
            result[etf] = Constituents(c.symbols, f"{_SRC_SA}-cache:{day.isoformat()}")
            logger.info(f"[{_LABEL}] {etf}: {len(c.symbols)} symbols (cache from {day.isoformat()})")
            continue
        # 4. yfinance top-10
        try:
            top = yf_top10(etf)
        except Exception as e:
            logger.warning(f"[{_LABEL}] {etf}: yfinance top holdings failed: {e}")
            top = None
        if top:
            result[etf] = Constituents(list(top), _SRC_YF)
            logger.info(f"[{_LABEL}] {etf}: {len(top)} symbols (yfinance top-10 fallback)")
            continue
        logger.warning(f"[{_LABEL}] {etf}: no constituents from any source; no 1M line today")
    save_cache(output_dir, today, {**today_cache, **result})
    return result
