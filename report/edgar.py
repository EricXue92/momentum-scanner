"""SEC EDGAR XBRL companyfacts fetcher for US-report fundamentals.

Public API: fetch_edgar_fundamentals(ticker). Returns the 8 EDGAR-owned
fields when available, None on any failure. Caller (report/enrich.py)
falls back to yfinance on None or per-field for partial dicts.

Cache layout under output/state/edgar_cache/:
  - company_tickers.json (TTL: 1 day)
  - CIK{0:010d}.json     (TTL: 7 days)

SEC requires a User-Agent containing an email; we hard-code the
project owner's address per CLAUDE.md / memory.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date as _date
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

CACHE_DIR = Path("output/state/edgar_cache")
COMPANY_TICKERS_TTL = 86_400              # 1 day
COMPANYFACTS_TTL = 7 * 86_400             # 7 days
USER_AGENT = "momentum-scanner xuelong0208@gmail.com"
HTTP_TIMEOUT = 10.0
HTTP_RETRY_DELAY = 0.5

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


# --- Cache TTL --------------------------------------------------------------

def _is_fresh(path: Path, ttl_seconds: int) -> bool:
    """True if `path` exists and its mtime is within `ttl_seconds` of now."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return False
    return (time.time() - mtime) < ttl_seconds


def _save_json_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def _load_json_cache(path: Path) -> dict | None:
    """Read and parse JSON from `path`. Returns None on missing file or
    corrupt JSON; on corruption the file is deleted so the next refresh
    overwrites cleanly."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(f"[edgar] corrupt cache at {path} ({e}); deleting")
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _http_get_json(url: str) -> dict | None:
    """GET `url`, return parsed JSON or None on failure.
    - 5xx / 429 / network error: retry once after HTTP_RETRY_DELAY.
    - 4xx other than 429: no retry, log info, return None.
    Never raises."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for attempt in (1, 2):
        try:
            resp = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            code = resp.status_code
            if code == 200:
                return resp.json()
            if code in (429,) or code >= 500:
                if attempt == 1:
                    logger.warning(f"[edgar] HTTP {code} from {url}, retry in {HTTP_RETRY_DELAY}s")
                    time.sleep(HTTP_RETRY_DELAY)
                    continue
                logger.warning(f"[edgar] HTTP {code} from {url}, giving up after retry")
                return None
            logger.info(f"[edgar] HTTP {code} from {url}, not retrying")
            return None
        except (httpx.HTTPError, ValueError) as e:
            if attempt == 1:
                logger.warning(f"[edgar] {type(e).__name__} from {url}, retry: {e}")
                time.sleep(HTTP_RETRY_DELAY)
                continue
            logger.warning(f"[edgar] {type(e).__name__} from {url}, giving up: {e}")
            return None
    return None


# --- Ticker → CIK lookup ----------------------------------------------------

# In-memory memo across calls within one process (rebuilt on first miss).
_cached_ticker_map: dict[str, str] | None = None


def _parse_ticker_cik_map(raw: dict) -> dict[str, str]:
    """Convert SEC's int-keyed dict to {TICKER: 10-digit CIK string}."""
    out: dict[str, str] = {}
    for entry in raw.values():
        try:
            ticker = str(entry["ticker"]).upper()
            cik = f"{int(entry['cik_str']):010d}"
        except (KeyError, TypeError, ValueError):
            continue
        out[ticker] = cik
    return out


def _refresh_ticker_cik_map() -> dict[str, str] | None:
    """Read cache → if stale or missing, fetch from SEC → write cache → parse."""
    cache_path = CACHE_DIR / "company_tickers.json"
    if _is_fresh(cache_path, COMPANY_TICKERS_TTL):
        cached = _load_json_cache(cache_path)
        if cached:
            return _parse_ticker_cik_map(cached)
    fresh = _http_get_json(COMPANY_TICKERS_URL)
    if fresh:
        _save_json_cache(cache_path, fresh)
        return _parse_ticker_cik_map(fresh)
    # Fall back to stale cache rather than nothing.
    cached = _load_json_cache(cache_path)
    if cached:
        logger.info("[edgar] using stale company_tickers.json (network failed)")
        return _parse_ticker_cik_map(cached)
    return None


def _get_cik(ticker: str) -> str | None:
    """Resolve `ticker` to a 10-digit CIK string; None if not found."""
    global _cached_ticker_map
    if _cached_ticker_map is None:
        _cached_ticker_map = _refresh_ticker_cik_map()
    if _cached_ticker_map is None:
        return None
    return _cached_ticker_map.get(ticker.upper())


# --- companyfacts fetch -----------------------------------------------------

def _fetch_companyfacts(cik: str) -> dict | None:
    """Return the parsed companyfacts JSON for `cik` (10-digit string).
    Uses the 7-day TTL cache; on cache miss, fetches and writes the cache.
    Returns None if both cache and network fail."""
    cache_path = CACHE_DIR / f"CIK{cik}.json"
    if _is_fresh(cache_path, COMPANYFACTS_TTL):
        cached = _load_json_cache(cache_path)
        if cached:
            return cached
    fresh = _http_get_json(COMPANYFACTS_URL.format(cik=cik))
    if fresh:
        _save_json_cache(cache_path, fresh)
        return fresh
    cached = _load_json_cache(cache_path)
    if cached:
        logger.info(f"[edgar] using stale companyfacts for CIK {cik}")
        return cached
    return None


# --- XBRL concept constants -------------------------------------------------

# Order matters: Apple/most large issuers expose only sparse segment-level
# entries under `Revenues` (~11 facts) while the consolidated number lives in
# `RevenueFromContractWithCustomerExcludingAssessedTax` (~100+ facts post-ASC 606).
# Tried `Revenues`-first during smoke-test on AAPL → garbage YoY% (>+400%).
REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "Revenues",
)
EPS_CONCEPTS = (
    "EarningsPerShareDiluted",
    "EarningsPerShareBasic",
)
USD = "USD"
USD_PER_SHARE = "USD/shares"


def _match_concept_facts(
    companyfacts: dict, candidates: tuple[str, ...], unit: str
) -> list[dict] | None:
    """Pick the candidate concept whose latest quarterly fact is most recent.

    Naive priority order (return first concept with any facts) breaks when an
    issuer switches XBRL tagging mid-history: the abandoned concept still has
    older facts, so first-priority wins despite being stale. Verified against
    SSR Mining (SSRM) which migrated from
    `RevenueFromContractWithCustomerExcludingAssessedTax` to `Revenues` at
    end of 2024 — naive selection returned Q4 2023 ($414M) instead of the
    actual Q1 2026 ($582M) that lives under `Revenues`.

    Selection rule:
    1. For each candidate concept present with `unit`, find its latest
       quarterly survivor's `end` date (after `_select_quarterly_facts`).
    2. Pick the concept with the most-recent latest-quarterly. Ties go to
       the candidate earlier in `candidates` (preserves the original
       priority intent for cases where AAPL's sparse `Revenues` would
       otherwise have to coexist with the consolidated RFCWCEAT — sparse
       `Revenues` produces zero quarterly survivors, so RFCWCEAT wins
       whenever a real history exists).
    3. When no candidate has any quarterly survivor (annual-only / pre-IPO
       data), fall back to the first candidate that has any facts.
    Returns None if no candidate concept is present with `unit`.
    """
    try:
        gaap = companyfacts["facts"]["us-gaap"]
    except (KeyError, TypeError):
        return None

    candidate_facts: dict[str, list[dict]] = {}
    candidate_latest_q: dict[str, str] = {}
    for name in candidates:
        concept = gaap.get(name)
        if not concept:
            continue
        units = concept.get("units") or {}
        facts = units.get(unit)
        if not facts:
            continue
        candidate_facts[name] = facts
        quarterly = _select_quarterly_facts(facts)
        if quarterly:
            candidate_latest_q[name] = quarterly[-1].get("end") or ""

    if not candidate_facts:
        return None

    if candidate_latest_q:
        # max key: (latest_end, -priority_index) so a more-recent end wins
        # outright, ties broken by lower index in `candidates` (= higher
        # priority).
        best = max(
            candidate_latest_q,
            key=lambda n: (candidate_latest_q[n], -candidates.index(n)),
        )
        return candidate_facts[best]

    for name in candidates:
        if name in candidate_facts:
            return candidate_facts[name]
    return None


# --- Annual fact selection + YoY extraction ----------------------------------

def _compute_yoy(current: float | None, prior: float | None) -> float | None:
    """Mirror of enrich.compute_yoy. Uses `abs(prior)` so loss-bearing
    companies show meaningful improvement/worsening % without sign-flip noise.
    Returns None only on missing input or `prior == 0`."""
    if current is None or prior is None:
        return None
    if prior == 0:
        return None
    return (current - prior) / abs(prior) * 100.0


def _parse_iso_date(s: str) -> _date | None:
    try:
        return _date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _period_label(end_date_iso: str) -> str:
    """'2024-12-30' -> \"Dec'24\". Empty string on parse failure."""
    d = _parse_iso_date(end_date_iso)
    if d is None:
        return ""
    return d.strftime("%b'%y")


def _period_days(f: dict) -> int | None:
    """Length in days of an XBRL fact's reporting period, or None if unparseable.
    XBRL files often expose multiple period lengths under the same `fp`
    (e.g. Apple files BOTH a 90-day Q2 and a 181-day H1 with `fp=Q2`).
    Callers filter by length to keep the right one (3-month for Q1/Q2/Q3,
    12-month for FY)."""
    s = _parse_iso_date(f.get("start", ""))
    e = _parse_iso_date(f.get("end", ""))
    if s is None or e is None:
        return None
    return (e - s).days


# Period-length windows tolerate the ±5 day fiscal-quarter wobble that hits
# 4-4-5 / 4-5-4 / 5-4-4 / 52-53-week calendars in practice.
QUARTER_DAYS_RANGE = (80, 100)
ANNUAL_DAYS_RANGE = (350, 380)


def _is_quarter_period(f: dict) -> bool:
    days = _period_days(f)
    return days is not None and QUARTER_DAYS_RANGE[0] <= days <= QUARTER_DAYS_RANGE[1]


def _is_annual_period(f: dict) -> bool:
    days = _period_days(f)
    return days is not None and ANNUAL_DAYS_RANGE[0] <= days <= ANNUAL_DAYS_RANGE[1]


def _select_annual_facts(facts: list[dict]) -> list[dict]:
    """Return only 10-K/10-K/A FY filings with a 12-month reporting period,
    deduped by `end` date (latest filed wins so amendments override originals),
    sorted oldest→newest by `end`. The period filter rejects partial-year
    transition reports and the occasional accidentally-tagged shorter period."""
    by_end: dict[str, dict] = {}
    for f in facts:
        if f.get("fp") != "FY":
            continue
        if not str(f.get("form", "")).startswith("10-K"):
            continue
        if not _is_annual_period(f):
            continue
        end = f.get("end")
        if not end:
            continue
        existing = by_end.get(end)
        if existing is None or str(f.get("filed", "")) > str(existing.get("filed", "")):
            by_end[end] = f
    return sorted(by_end.values(), key=lambda f: f["end"])


def _extract_annual_yoy(facts: list[dict] | None, years_back: int = 5) -> list[float | None]:
    """Up to `years_back` YoY % datapoints in oldest→newest order. Pads
    leading slots with None when history is short."""
    if not facts:
        return [None] * years_back
    annual = _select_annual_facts(facts)
    values = [f.get("val") for f in annual]
    out: list[float | None] = []
    # We want the most recent `years_back + 1` values to compute `years_back` YoY.
    needed_pairs = years_back
    for i in range(-needed_pairs, 0):
        try:
            current = values[i]
            prior = values[i - 1]
        except IndexError:
            out.append(None)
            continue
        out.append(_compute_yoy(current, prior))
    return out


# --- Quarterly fact selection + YoY extraction --------------------------------

def _select_quarterly_facts(facts: list[dict]) -> list[dict]:
    """Combine 10-Q reported quarters (Q1/Q2/Q3) with derived Q4
    (= FY value − sum of same-FY Q1+Q2+Q3). Q4 derivation skipped
    when any component is missing. Returned list sorted oldest→newest by `end`.

    Period-length filter: Q1/Q2/Q3 must report a 3-month window and FY must
    report a 12-month window. XBRL files multiple period lengths under the
    same `fp` tag — e.g. Apple files BOTH a 90-day Q2 and a 181-day H1 with
    `fp=Q2`. Without this filter the latest-filed-wins dedup arbitrarily
    picks one and inflates derived values.

    Dedup key is `end` (the period end date), NOT `(fy, fp)`. The XBRL `fy`
    field is the FILING's fiscal year, not the data point's fiscal year — when
    a 10-Q includes the prior-year comparative period, BOTH that comparative
    fact and the current-period fact carry the same `fy` and `fp`, so
    `(fy, fp)` collides and silently drops the newer quarter (verified against
    INOD's 2026 Q1 10-Q where last-year Q1 ended up beating current Q1)."""
    quarterly_by_end: dict[str, dict] = {}
    annual_by_end: dict[str, dict] = {}
    for f in facts:
        fp = f.get("fp")
        form = str(f.get("form", ""))
        end = f.get("end")
        if not end:
            continue
        if fp in ("Q1", "Q2", "Q3"):
            if not form.startswith("10-Q") or not _is_quarter_period(f):
                continue
            existing = quarterly_by_end.get(end)
            if existing is None or str(f.get("filed", "")) > str(existing.get("filed", "")):
                quarterly_by_end[end] = f
        elif fp == "FY":
            if not form.startswith("10-K") or not _is_annual_period(f):
                continue
            existing = annual_by_end.get(end)
            if existing is None or str(f.get("filed", "")) > str(existing.get("filed", "")):
                annual_by_end[end] = f

    out: list[dict] = list(quarterly_by_end.values())

    # Derive Q4 per fiscal year. Match the FY fact to its component quarters
    # via the FY fact's [start, end] range (handles non-calendar fiscal years
    # like Apple's Sep year-end without trusting the `fy` field).
    for fy_end, fy_fact in annual_by_end.items():
        fy_start = fy_fact.get("start")
        if not fy_start:
            continue
        components = {
            q["fp"]: q
            for q in quarterly_by_end.values()
            if q.get("fp") in ("Q1", "Q2", "Q3")
            and q.get("end")
            and fy_start <= q["end"] <= fy_end
        }
        if set(components) != {"Q1", "Q2", "Q3"}:
            continue
        try:
            q4_val = float(fy_fact["val"]) - sum(
                float(c["val"]) for c in components.values()
            )
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "end": fy_end,
            "val": q4_val,
            "fy": fy_fact.get("fy"),
            "fp": "Q4",
            "form": "derived",
            "filed": fy_fact.get("filed", ""),
        })
    out.sort(key=lambda f: f.get("end") or "")
    return out


def _extract_quarterly_yoy(
    facts: list[dict] | None, n_quarters: int = 4
) -> tuple[list[float | None], list[str]]:
    """Return (yoy_pct list, labels list), each length `n_quarters`,
    oldest→newest. Each YoY pairs each quarter with the same calendar
    quarter one year prior (4 quarters back in the chronological list).
    Leading slots are None when history is too short."""
    quarters = _select_quarterly_facts(facts) if facts else []
    values = [q.get("val") for q in quarters]
    ends = [q.get("end") or "" for q in quarters]

    yoy: list[float | None] = []
    labels: list[str] = []
    for i in range(-n_quarters, 0):
        try:
            current = values[i]
            prior = values[i - 4]
        except IndexError:
            yoy.append(None)
        else:
            yoy.append(_compute_yoy(current, prior))
        try:
            labels.append(_period_label(ends[i]))
        except IndexError:
            labels.append("")
    return yoy, labels


def _latest_quarter_with_yoy(
    facts: list[dict] | None,
) -> tuple[float | None, float | None]:
    """Most recent quarterly value + YoY vs same calendar quarter one year prior."""
    if not facts:
        return (None, None)
    quarters = _select_quarterly_facts(facts)
    if not quarters:
        return (None, None)
    latest = quarters[-1]
    val = latest.get("val")
    prior_val = quarters[-5].get("val") if len(quarters) >= 5 else None
    return (val, _compute_yoy(val, prior_val))


def fetch_edgar_fundamentals(ticker: str) -> dict | None:
    """Return EDGAR-sourced fundamentals dict for `ticker`, or None on failure.
    Caller should fall back to yfinance on None and per-field on partials."""
    cik = _get_cik(ticker)
    if cik is None:
        logger.info(f"[edgar] no CIK for {ticker}; skipping EDGAR")
        return None
    cf = _fetch_companyfacts(cik)
    if cf is None:
        logger.warning(f"[edgar] companyfacts unavailable for {ticker} (CIK {cik})")
        return None

    rev_facts = _match_concept_facts(cf, REVENUE_CONCEPTS, USD)
    eps_facts = _match_concept_facts(cf, EPS_CONCEPTS, USD_PER_SHARE)
    if rev_facts is None and eps_facts is None:
        logger.warning(f"[edgar] no matching revenue or EPS concepts for {ticker}")
        return None

    rev_q_yoy, rev_labels = _extract_quarterly_yoy(rev_facts, 4)
    eps_q_yoy, eps_labels = _extract_quarterly_yoy(eps_facts, 4)
    rev_latest, rev_latest_yoy = _latest_quarter_with_yoy(rev_facts)
    eps_latest, eps_latest_yoy = _latest_quarter_with_yoy(eps_facts)

    return {
        "eps_latest_q": eps_latest,
        "eps_latest_q_yoy_pct": eps_latest_yoy,
        "revenue_latest_q": rev_latest,
        "revenue_latest_q_yoy_pct": rev_latest_yoy,
        "annual_eps_yoy_5y": _extract_annual_yoy(eps_facts, 5),
        "annual_revenue_yoy_5y": _extract_annual_yoy(rev_facts, 5),
        "quarterly_eps_yoy_4q": eps_q_yoy,
        "quarterly_eps_yoy_4q_labels": eps_labels,
        "quarterly_revenue_yoy_4q": rev_q_yoy,
        "quarterly_revenue_yoy_4q_labels": rev_labels,
    }


# --- Recent filings (evidence bundle) ---------------------------------------

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SUBMISSIONS_TTL = 86_400  # 1 day; the pre-market path (phase 2) passes 6h

# Material-event / periodic / offering / ownership forms. Form 4 and 144
# (insider trades) are counted, not listed — too noisy for the LLM.
FILING_FORMS_OF_INTEREST: frozenset[str] = frozenset({
    "8-K", "8-K/A", "6-K", "10-Q", "10-Q/A", "10-K", "10-K/A",
    "S-1", "S-1/A", "S-3", "S-3/A",
})
_FILING_FORM_PREFIXES = ("424B", "SC 13D", "SC 13G")
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"


def _form_of_interest(form: str) -> bool:
    return form in FILING_FORMS_OF_INTEREST or form.startswith(_FILING_FORM_PREFIXES)


def _fetch_submissions(cik: str, ttl_seconds: int) -> dict | None:
    cache_path = CACHE_DIR / f"submissions_CIK{cik}.json"
    if _is_fresh(cache_path, ttl_seconds):
        cached = _load_json_cache(cache_path)
        if cached:
            return cached
    fresh = _http_get_json(SUBMISSIONS_URL.format(cik=cik))
    if fresh:
        _save_json_cache(cache_path, fresh)
        return fresh
    cached = _load_json_cache(cache_path)
    if cached:
        logger.info(f"[edgar] using stale submissions cache for CIK{cik} (network failed)")
        return cached
    return None


def fetch_recent_filings(
    ticker: str,
    *,
    days: int,
    max_items: int,
    as_of: _date | None = None,
    ttl_seconds: int = SUBMISSIONS_TTL,
) -> tuple[list[dict], int] | None:
    """Recent filings of interest for `ticker` within the last `days` days
    (newest first, at most `max_items`) plus the count of Form 4 filings in
    the same window. Returns None when the ticker has no CIK or the
    submissions feed is unavailable. Never raises."""
    cik = _get_cik(ticker)
    if not cik:
        return None
    raw = _fetch_submissions(cik, ttl_seconds)
    if not raw:
        return None
    recent = ((raw.get("filings") or {}).get("recent")) or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items = recent.get("items") or []
    descs = recent.get("primaryDocDescription") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []

    today = as_of or _date.today()
    cutoff = today.fromordinal(today.toordinal() - days)
    cik_int = int(cik)

    def at(seq: list, i: int) -> str:
        return str(seq[i]) if i < len(seq) and seq[i] is not None else ""

    filings: list[dict] = []
    form4_count = 0
    for i in range(min(len(forms), len(dates))):
        form = at(forms, i).strip()
        filed = _parse_iso_date(at(dates, i))
        if filed is None or filed < cutoff or filed > today:
            continue
        if form == "4":
            form4_count += 1
            continue
        if not _form_of_interest(form):
            continue
        acc = at(accs, i).replace("-", "")
        doc = at(docs, i)
        url = ARCHIVE_URL.format(cik_int=cik_int, acc_nodash=acc, doc=doc) if acc and doc else ""
        filings.append({
            "form": form,
            "date": filed.isoformat(),
            "items": at(items, i),
            "description": at(descs, i),
            "url": url,
        })
    filings.sort(key=lambda f: f["date"], reverse=True)
    return filings[:max_items], form4_count
