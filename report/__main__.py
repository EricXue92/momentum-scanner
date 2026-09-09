"""Orchestrator for `python -m report` / `main.py --mode report`.

Read dated .txt files for the given market+date, prioritize and cap, enrich each
ticker with yfinance + RS table, prefetch the evidence bundle per ticker
(report/evidence.py), fan out async Claude calls, render and write the .html
artifact to output/Reports/PostMarket/. Soft-fail on any unexpected error."""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from report import analyst, enrich, evidence, ranker, renderer
from report.llm import build_backend
from report.state import (
    MAX_TICKERS_PER_REPORT,
    POSTMARKET_DIR,
    PROJECT_ROOT,
    groups_for_market,
    input_dir_for_market,
    load_dotenv,
    load_report_config,
)

logger = logging.getLogger(__name__)
HKT = ZoneInfo("Asia/Hong_Kong")
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "prompts" / "canslim_system.md"


def _split_exchange_ticker(qualified: str) -> tuple[str, str]:
    """`NASDAQ:AAPL` -> `('NASDAQ', 'AAPL')`. HK format `HKEX:00700` keeps the leading zeros."""
    if ":" in qualified:
        ex, sym = qualified.split(":", 1)
        return ex, sym
    return ("", qualified)


def _yf_ticker(symbol: str, market: str) -> str:
    """yfinance expects bare US tickers and `<4-digit>.HK` for Hong Kong (NB: differs from Futu's 5-digit `HK.<5-digit>` format)."""
    if market == "hk":
        return f"{symbol.lstrip('0').zfill(4)}.HK"
    return symbol


def _normalize_rs_key(raw_symbol: str, market: str) -> str:
    """Canonicalize an RS-table ticker key to match the yfinance form used at lookup time.
    US: uppercase symbol. HK: 'HK.00700' → '0700.HK'."""
    sym = raw_symbol.strip().upper()
    if market == "hk" and sym.startswith("HK."):
        sym = sym[3:]
        sym = sym.lstrip("0") or "0"
        sym = f"{sym.zfill(4)}.HK"
    return sym


def _load_rs_lookup(market: str, date_stem: str):
    """Return a callable `(ticker) -> percentile or None` reading the cached
    RS table written by rs_rating.py / hk_rs.py during the EOD run.
    Missing cache → all-None lookup (the report still runs, just without RS field)."""
    state_dir = PROJECT_ROOT / "output" / "state"
    candidates = [
        state_dir / f"rs_rating_{date_stem.replace('_', '-')}.csv",
        state_dir / f"rs_rating_{date_stem}.csv",
        state_dir / f"hk_rs_rating_{date_stem.replace('_', '-')}.csv",
        state_dir / f"hk_rs_rating_{date_stem}.csv",
    ]
    table: dict[str, int] = {}
    for path in candidates:
        if not path.is_file():
            continue
        if (market == "us" and "hk_rs_rating" in path.name) or (
            market == "hk" and path.name.startswith("rs_rating_")
        ):
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    sym = row.get("ticker") or row.get("Ticker") or row.get("code")
                    pct = (
                        row.get("percentile")
                        or row.get("Percentile")
                        or row.get("rs_percentile")
                    )
                    if not (sym and pct):
                        continue
                    try:
                        score = int(float(pct))
                    except ValueError:
                        continue
                    key = _normalize_rs_key(sym, market)
                    table[key] = score
            break
        except Exception as e:
            logger.warning(f"[report] failed to read RS cache {path}: {e}")
    if not table:
        logger.info(f"[report] no RS cache found for {market} {date_stem}; field will be null")
    return lambda t: table.get(t.upper())


_EMPTY_DATA_TEMPLATE: dict = {
    "ticker": None, "group": None, "exchange": None, "company_name": None,
    "sector": None, "industry": None,
    "market_cap": None, "last_price": None, "prev_close": None, "gap_pct": None,
    "institutional_holdings_pct": None,
    "roe_pct": None,
    "eps_latest_q": None, "eps_latest_q_yoy_pct": None,
    "revenue_latest_q": None, "revenue_latest_q_yoy_pct": None,
    "annual_eps_yoy_5y": [None, None, None, None, None],
    "annual_revenue_yoy_5y": [None, None, None, None, None],
    "quarterly_eps_yoy_4q": [None, None, None, None],
    "quarterly_eps_yoy_4q_labels": ["", "", "", ""],
    "quarterly_revenue_yoy_4q": [None, None, None, None],
    "quarterly_revenue_yoy_4q_labels": ["", "", "", ""],
    "latest_earnings_date": None, "rs_percentile": None,
    "yahoo_revenue_growth_yoy_pct": None,
    "yahoo_earnings_growth_yoy_pct": None,
    "ipo_date": None,
}


def _empty_data(ticker: str, group: str, exchange: str) -> dict:
    return {**_EMPTY_DATA_TEMPLATE, "ticker": ticker, "group": group, "exchange": exchange}


def _load_evidence_config(report_cfg: dict) -> evidence.EvidenceConfig:
    return evidence.EvidenceConfig.from_dict((report_cfg or {}).get("evidence"))


async def _prefetch_evidence(
    yf_sym: str, market: str, cfg: evidence.EvidenceConfig, as_of: date
) -> dict:
    """Run the sync yfinance/EDGAR fetch off-loop with a hard timeout. A
    timeout is treated as all-sources-failed so the search fallback can
    kick in; it must never abort the report.

    Orphan-thread note: `asyncio.wait_for` cancels *our* await on timeout,
    but `asyncio.to_thread` runs on the default executor, which does not
    support cancellation — the worker thread keeps running `fetch_evidence`
    to completion in the background even after we've moved on. This is
    tolerable because yfinance/httpx calls inside it carry their own ~10s
    timeouts, so the orphan does eventually finish; `asyncio.run` (the
    process's outermost loop) waits for all executor threads to drain at
    shutdown, so the process won't exit until it does. A dedicated bounded
    executor (so a stuck orphan can't pile up and block shutdown) is
    deferred to phase 2."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(evidence.fetch_evidence, yf_sym, market, cfg, as_of=as_of),
            timeout=cfg.timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[evidence] {yf_sym}: prefetch timed out after {cfg.timeout_seconds}s")
        ev = evidence.empty_evidence(as_of, errors=["timeout"])
        if market.lower() != "us":
            ev["filings"] = None
        return ev
    except Exception as e:  # fetch_evidence never raises, but to_thread plumbing might
        logger.warning(f"[evidence] {yf_sym}: prefetch failed: {type(e).__name__}: {e}")
        ev = evidence.empty_evidence(as_of, errors=[f"prefetch: {type(e).__name__}"])
        if market.lower() != "us":
            ev["filings"] = None
        return ev


async def _run_async(market: str, date_stem: str, date_iso: str) -> int:
    # Direct `uv run main.py --mode report ...` invocations don't go through the
    # wrapper script's `source .env`, so try to fill the gap here. No-op if the
    # env var is already set or .env is absent.
    load_dotenv()
    report_cfg = load_report_config()
    try:
        backend = build_backend(report_cfg)
    except (RuntimeError, ValueError) as e:
        logger.warning(f"[report] backend init failed; skipping: {e}")
        return 0

    input_dir = input_dir_for_market(market)
    groups = ranker.collect_market_groups(input_dir, date_stem, market)
    total = sum(len(v) for v in groups.values())
    if total == 0:
        logger.info(f"[report] no tickers in any {market.upper()} group for {date_stem}; skip")
        return 0

    analyzed_entries, truncated_entries = ranker.rank_and_cap(
        groups, cap=MAX_TICKERS_PER_REPORT
    )
    logger.info(
        f"[report] {market.upper()} {date_stem}: "
        f"{len(analyzed_entries)} to analyze, {len(truncated_entries)} truncated"
    )

    rs_lookup = _load_rs_lookup(market, date_stem)
    ev_cfg = _load_evidence_config(report_cfg)

    as_of = date.fromisoformat(date_iso)

    # Enrich (sequential — yfinance is the bottleneck and parallel pulls trip rate limits).
    enriched: list[dict] = []
    evidences: list[dict | None] = []
    budgets: list[int | None] = []
    for qualified, group in analyzed_entries:
        exchange, symbol = _split_exchange_ticker(qualified)
        yf_sym = _yf_ticker(symbol, market)
        try:
            data = enrich.fetch_ticker_data(yf_sym, group, exchange, rs_lookup, as_of_date=as_of)
        except Exception as e:
            logger.warning(f"[report] enrich failed for {qualified}: {e}")
            data = _empty_data(yf_sym, group, exchange)
        enriched.append(data)
        if ev_cfg.enabled:
            ev = await _prefetch_evidence(yf_sym, market, ev_cfg, as_of)
            budget = evidence.search_budget(ev, ev_cfg)
            logger.info(
                f"[evidence] {yf_sym}: {evidence.summarize_for_log(ev)} search={budget}"
                + (f" errors={ev['errors']}" if ev.get("errors") else "")
            )
        else:
            ev, budget = None, None
        evidences.append(ev)
        budgets.append(budget)

    if not SYSTEM_PROMPT_PATH.is_file():
        logger.error(f"[report] system prompt missing at {SYSTEM_PROMPT_PATH}")
        return 0
    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    # 5 was too aggressive — web_search calls compete for Anthropic-side queues
    # and the tail of each batch hit our 90s timeout. 3 keeps wall-clock under
    # 5 minutes for 14 tickers without timing out.
    logger.info(f"[report] using backend: {backend.name}")
    semaphore = asyncio.Semaphore(3)
    try:
        coroutines = [
            analyst.analyze_ticker(
                backend, system_prompt, data, semaphore,
                evidence=ev, max_search_calls=budget,
            )
            for data, ev, budget in zip(enriched, evidences, budgets)
        ]
        sections = await asyncio.gather(*coroutines)
    finally:
        await backend.aclose()

    html_path = renderer.write_report_files(
        out_dir=POSTMARKET_DIR,
        date_stem=date_stem,
        market=market,
        enriched=enriched,
        prose_sections=sections,
        truncated=[(q.split(":", 1)[-1], g) for q, g in truncated_entries],
        generated_at=datetime.now(HKT),
        date_iso=date_iso,
        model_label=backend.model_label(),
        evidence_meta=[
            None if ev is None else {
                "news_count": ev.get("news_count", 0),
                "filings_count": None if ev.get("filings") is None else ev.get("filings_count", 0),
                "search_budget": budget,
            }
            for ev, budget in zip(evidences, budgets)
        ],
    )
    logger.info(f"[report] wrote {html_path}")
    return 0


_DATED_FILENAME = re.compile(r"^(\d{4})_(\d{2})_(\d{2})_(.+)$")


def _latest_available_date(market: str) -> date | None:
    """Scan the market's input dir for the most recent date_stem that has at
    least one non-empty group file. Returns None when the dir is empty.

    Lets ad-hoc `--mode report` runs surface the *last* trading day's picks
    instead of demanding today's files exist (HK EOD only writes at 20:00 HKT,
    and weekend / holiday runs would otherwise hit an empty 'today')."""
    input_dir = input_dir_for_market(market)
    if not input_dir.is_dir():
        return None
    valid_groups = set(groups_for_market(market))
    latest: tuple[int, int, int] | None = None
    for path in input_dir.glob("*.txt"):
        m = _DATED_FILENAME.match(path.stem)
        if not m or m.group(4) not in valid_groups:
            continue
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            continue
        ymd = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if latest is None or ymd > latest:
            latest = ymd
    if latest is None:
        return None
    try:
        return date(*latest)
    except ValueError:
        return None


def run(market: str, override_date: str | None = None) -> int:
    """Entry point invoked by main.py. Soft-fails on any exception."""
    try:
        if override_date:
            d = date.fromisoformat(override_date)
        else:
            today = datetime.now(HKT).date()
            d = _latest_available_date(market.lower()) or today
            if d != today:
                logger.info(
                    f"[report] no {market.upper()} files for {today}; using latest available {d}"
                )
        date_stem = d.strftime("%Y_%m_%d")
        date_iso = d.isoformat()
        return asyncio.run(_run_async(market.lower(), date_stem, date_iso))
    except Exception as e:
        logger.exception(f"[report] aborted: {e}")
        return 0


def _cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["us", "hk"], required=True)
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to today HKT")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    return run(args.market, args.date)


if __name__ == "__main__":
    sys.exit(_cli())
