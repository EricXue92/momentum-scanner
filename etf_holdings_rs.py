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
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, NamedTuple

import pandas as pd

import us_rs_3m

logger = logging.getLogger("momentum_scanner")

_LABEL = "ETF 持仓RS"

#: Plain US listing: 1-5 capitals, optional one-letter share class (``BRK.B``).
US_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
#: 5 letters ending in X = mutual / money-market fund (``AGPXX`` sat in
#: BWET's collateral and got ranked on 2026-10-31), never a listed stock.
_FUND_SYMBOL_RE = re.compile(r"^[A-Z]{4}X$")


class Constituents(NamedTuple):
    symbols: list[str]        # as parsed (page order, foreign / cash codes included)
    source: str               # "stockanalysis" | "stockanalysis-cache:<YYYY-MM-DD>" | "yf_top10"
    us: list[str] | None = None   # symbols the page linked as US stocks (/stocks/<sym>/);
                                  # None = unknown (yfinance fallback, legacy cache) → regex


# --- parsing --------------------------------------------------------------

_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _cell_text(raw: str) -> str:
    return _html.unescape(_TAG_RE.sub("", raw)).strip()


_STOCK_LINK_RE = re.compile(r'href="/stocks/[^"/]+/"')


def _symbol_cells(page: str) -> list[str] | None:
    """Raw ``<td>`` HTML of the Symbol column, one per data row of the first
    ``<table>`` whose header row contains ``Symbol``. ``None`` when no such
    table / header exists (page redesign, Pro wall) or no data row follows —
    callers treat that as a failed fetch, never as an empty ETF."""
    for m in _TABLE_RE.finditer(page or ""):
        rows = _ROW_RE.findall(m.group(0))
        symbol_col: int | None = None
        cells_out: list[str] = []
        for row in rows:
            cells = _CELL_RE.findall(row)
            if not cells:
                continue
            if "<th" in row.lower():
                texts = [_cell_text(c) for c in cells]
                if "Symbol" in texts:
                    symbol_col = texts.index("Symbol")
                continue
            if symbol_col is None or len(cells) <= symbol_col:
                continue
            if _cell_text(cells[symbol_col]):
                cells_out.append(cells[symbol_col])
        if symbol_col is not None:
            return cells_out or None
    return None


def parse_holdings_html(page: str) -> list[str] | None:
    """Symbol column of the holdings table, in page order (weight-descending):
    US (``NVDA``, ``BRK.B``), foreign (``KRX:005930``, ``285A.JP``), cash /
    futures / swap rows (``KRW``, ``IXPZ6``) alike. ``None`` = no usable
    table (see ``_symbol_cells``)."""
    cells = _symbol_cells(page)
    if cells is None:
        return None
    return [_cell_text(c) for c in cells]


def parse_us_symbols(page: str) -> list[str]:
    """The subset of ``parse_holdings_html`` whose Symbol cell links to
    ``/stocks/<sym>/`` — stockanalysis' own "US-listed stock" marker (foreign
    lines link to ``/quote/…``, cash / futures / swaps have no link). The
    regex in ``is_us_symbol`` cannot tell ``KRW`` from a ticker, and ``HKD``
    / ``USD`` *are* tickers."""
    cells = _symbol_cells(page) or []
    return [_cell_text(c) for c in cells if _STOCK_LINK_RE.search(c)]


# --- symbols --------------------------------------------------------------

def is_us_symbol(sym: str) -> bool:
    sym = sym or ""
    return bool(US_SYMBOL_RE.match(sym)) and not _FUND_SYMBOL_RE.match(sym)


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
#: After this many consecutive transient stockanalysis failures (timeout,
#: 403/429 block, 5xx) the run stops going online: politeness under a block,
#: and a hung host must not push the 3M/1M files towards the EOD watchdog.
_MAX_CONSECUTIVE_FAILURES = 3


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
                us = rec.get("us")
                out[str(etf)] = Constituents(
                    [str(x) for x in syms], str(rec.get("source", _SRC_SA)),
                    [str(x) for x in us] if isinstance(us, list) else None)
        return out
    except FileNotFoundError:
        return {}
    except Exception as e:  # corrupt JSON, wrong shape
        logger.warning(f"[{_LABEL}] ignoring unreadable cache {p.name}: {e}")
        return {}


def save_cache(output_dir: Path, day: date, data: dict[str, Constituents]) -> None:
    """Persist only the stockanalysis-sourced entries (a fallback list must
    never be walked back to as the real one). Soft: write failure = WARNING."""
    keep = {etf: {"symbols": c.symbols, "source": c.source, "us": c.us}
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
    """Page HTML; ``""`` on HTTP 404 (definitive: no such ETF page — DXYZ,
    IBIT); ``None`` on any transient failure (timeout, reset, 5xx, 403/429,
    ``http.client`` protocol errors — ``IncompleteRead`` is NOT an OSError,
    same trap as ``rs_rating.fetch_rs_table``)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"[{_LABEL}] {url} -> HTTP {resp.status}")
                return None
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.warning(f"[{_LABEL}] {url} -> HTTP {e.code}")
        return "" if e.code == 404 else None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
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
    failures = 0           # consecutive transient failures; 404 and parse misses don't count
    for etf in etfs:
        if etf in today_cache:
            result[etf] = today_cache[etf]
            logger.info(f"[{_LABEL}] {etf}: {len(result[etf].symbols)} symbols (cache, today)")
            continue
        # 2. live — one ETF's surprise must never cost the others or the cache write
        syms: list[str] | None = None
        page: str | None = None
        if failures < _MAX_CONSECUTIVE_FAILURES:
            if fetched_live and delay > 0:
                sleep(delay)
            fetched_live = True
            try:
                page = fetch_page(HOLDINGS_URL.format(slug=etf.lower()))
                syms = parse_holdings_html(page) if page else None
            except Exception as e:
                logger.warning(f"[{_LABEL}] {etf}: stockanalysis fetch raised: {e}")
                page, syms = None, None
            if page is None:
                failures += 1
                if failures == _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(f"[{_LABEL}] {failures} consecutive stockanalysis failures; "
                                   f"no more live requests this run (cache / top-10 fallback only)")
            else:
                failures = 0
        if syms:
            result[etf] = Constituents(syms, _SRC_SA, parse_us_symbols(page or ""))
            logger.info(f"[{_LABEL}] {etf}: {len(syms)} symbols (stockanalysis)")
            continue
        if page is not None:
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
            result[etf] = Constituents(c.symbols, f"{_SRC_SA}-cache:{day.isoformat()}", c.us)
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


# --- scoring ---------------------------------------------------------------

def _fetch_klines(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """3mo daily closes for the constituents (the 1M ladder needs 21 bars;
    3mo leaves slack for holidays / fresh listings). Same retrying batch
    fetcher and NaN-Close fill as the ETF ranking. Monkeypatched in tests."""
    return us_rs_3m.fetch_us_klines_yf(symbols, period="3mo", fill_last_close=True)


def _score_1m(df: pd.DataFrame | None) -> float | None:
    score, reason = us_rs_3m._score_from_kline_bars(df, us_rs_3m.WEIGHTS_1M_BARS)
    return score if reason == "ok" else None


def rank_holdings(
    constituents: dict[str, Constituents],
    klines: dict[str, pd.DataFrame],
    benchmark_kline: pd.DataFrame | None,
    top_n: int,
) -> dict[str, list[str]]:
    """``{etf: [top_n display symbols]}`` — each ETF's US-listed constituents
    ordered by 1M excess score (constituent − benchmark) descending; names
    without klines / < 21 bars / foreign codes do not take part; an ETF
    with nothing scorable is omitted. ``klines`` is keyed by *display*
    symbol."""
    bench = _score_1m(benchmark_kline) if benchmark_kline is not None else None
    if bench is None:
        logger.warning(f"[{_LABEL}] no benchmark 1M score; ranking holdings on absolute scores")
        bench = 0.0
    scores: dict[str, float] = {}
    for sym, df in klines.items():
        sc = _score_1m(df)
        if sc is not None:
            scores[sym] = sc - bench
    out: dict[str, list[str]] = {}
    for etf, c in constituents.items():
        pool = c.symbols if c.us is None else c.us
        us = [s for s in dict.fromkeys(pool) if is_us_symbol(s)]
        scored = [s for s in us if s in scores]
        scored.sort(key=lambda s: scores[s], reverse=True)
        top = scored[:max(int(top_n), 0)]
        logger.info(f"[{_LABEL}] {etf}: 成分 {len(c.symbols)} ({c.source}) / 美股 {len(us)} / "
                    f"打分 {len(scored)} / 前{top_n}: " + (" > ".join(top) if top else "-"))
        if top:
            out[etf] = top
    return out


def format_extra(top: list[str], source: str) -> str:
    line = "1M强: " + " > ".join(top)
    return line + " (前十大)" if source == _SRC_YF else line


def compute_extras(
    cfg: dict,
    etfs: list[str],
    benchmark_kline: pd.DataFrame | None,
    today: date,
    output_dir: Path,
) -> dict[str, str]:
    """Entry point for ``etf_rs.run_etf_rs``: ``{etf: continuation line}``
    for the ETFs that have a scorable US constituent. ``{}`` when disabled,
    when no constituents were found, or when the kline batch came back
    empty (WARNING) — the caller then writes the rankings without the
    extra lines. May raise on programming errors; the caller is soft."""
    if not cfg.get("enabled", True):
        logger.info(f"[{_LABEL}] disabled in config; skipping")
        return {}
    top_n = int(cfg.get("top_n", 5))
    cons = fetch_constituents(etfs, today, output_dir, cfg)
    if not cons:
        logger.warning(f"[{_LABEL}] no constituents for any ETF; no 1M lines today")
        return {}
    us_symbols = sorted({s for c in cons.values()
                         for s in (c.symbols if c.us is None else c.us) if is_us_symbol(s)})
    if not us_symbols:
        logger.warning(f"[{_LABEL}] no US-listed constituents; no 1M lines today")
        return {}
    logger.info(f"[{_LABEL}] fetching 3mo closes for {len(us_symbols)} constituents "
                f"of {len(cons)} ETFs...")
    raw = _fetch_klines([to_yf_symbol(s) for s in us_symbols]) or {}
    klines = {from_yf_symbol(k): v for k, v in raw.items()}
    if not klines:
        logger.warning(f"[{_LABEL}] yfinance returned nothing for the constituents; no 1M lines today")
        return {}
    logger.info(f"[{_LABEL}] K 线覆盖 {len(klines)}/{len(us_symbols)}")
    ranked = rank_holdings(cons, klines, benchmark_kline, top_n)
    return {etf: format_extra(top, cons[etf].source) for etf, top in ranked.items()}
