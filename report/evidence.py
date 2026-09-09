"""Pre-fetched evidence bundle for the CANSLIM report.

Replaces model-driven web search as the *primary* source of qualitative
context. Four independent sources — yfinance news, yfinance analyst
consensus, yfinance earnings calendar, SEC EDGAR recent filings — are
fetched by code, each soft-failing on its own, and handed to the LLM as
one JSON block. Tavily search stays available only as a conditional
fallback (see `search_budget`).

Spec: docs/superpowers/specs/2026-09-09-report-evidence-prefetch-design.md
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from report import edgar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceConfig:
    """Tunables from `[report.evidence]` (EOD defaults). Phase 2 builds a
    second instance from `[morning_gap_catalyst]` with tighter windows."""

    enabled: bool = True
    news_max_items: int = 10
    news_max_age_hours: int = 720  # 30 days
    news_summary_chars: int = 300
    filings_days: int = 60
    filings_max_items: int = 8
    analyst_grades_days: int = 90
    search_fallback: bool = True
    min_items_for_no_search: int = 3
    fallback_search_calls: int = 1
    timeout_seconds: float = 20.0

    @classmethod
    def from_dict(cls, raw: dict | None) -> "EvidenceConfig":
        """Build from a TOML table; unknown keys ignored, values coerced to
        the field's declared type so `timeout_seconds = 5` works."""
        raw = raw or {}
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if f.type == "bool" or f.type is bool:
                kwargs[f.name] = bool(value)
            elif f.type == "float" or f.type is float:
                kwargs[f.name] = float(value)
            else:
                kwargs[f.name] = int(value)
        return cls(**kwargs)


def empty_evidence(as_of: date, errors: list[str] | None = None) -> dict:
    """The all-sources-failed (or not-yet-fetched) bundle. Every consumer
    can rely on these keys existing."""
    return {
        "as_of": as_of.isoformat(),
        "news": [],
        "analyst": None,
        "calendar": None,
        "filings": [],
        "form4_count": 0,
        "news_count": 0,
        "filings_count": 0,
        "errors": list(errors or []),
    }


def search_budget(evidence: dict | None, cfg: EvidenceConfig) -> int | None:
    """How many web_search calls the LLM may issue for this ticker.
    None = feature disabled, let the backend use its own default (legacy).
    0 = evidence sufficient, single no-tool call.
    >0 = fallback budget."""
    if not cfg.enabled:
        return None
    if not cfg.search_fallback:
        return 0
    items = 0
    if evidence:
        items = int(evidence.get("news_count") or 0) + int(evidence.get("filings_count") or 0)
    return 0 if items >= cfg.min_items_for_no_search else cfg.fallback_search_calls


def summarize_for_log(evidence: dict | None) -> str:
    if not evidence:
        return "news=0 filings=0 analyst=no calendar=no"
    yn = lambda v: "yes" if v else "no"  # noqa: E731
    return (
        f"news={evidence.get('news_count', 0)} filings={evidence.get('filings_count', 0)} "
        f"analyst={yn(evidence.get('analyst'))} calendar={yn(evidence.get('calendar'))}"
    )


# --- Source extractors -------------------------------------------------------

def _parse_pubdate(s: Any) -> datetime | None:
    """yfinance pubDate is ISO-8601 with trailing Z; return aware UTC or None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract_news(raw_news: list, *, now: datetime, cfg: EvidenceConfig) -> list[dict]:
    """Flatten `Ticker.news` (list of {content: {...}}) into compact rows,
    drop items older than `news_max_age_hours` or without title/date, newest
    first, capped at `news_max_items`."""
    cutoff_seconds = cfg.news_max_age_hours * 3600
    rows: list[tuple[datetime, dict]] = []
    for item in raw_news or []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, dict):
            continue
        title = (content.get("title") or "").strip()
        published = _parse_pubdate(content.get("pubDate"))
        if not title or published is None:
            continue
        if (now - published).total_seconds() > cutoff_seconds:
            continue
        provider = content.get("provider") or {}
        source = provider.get("displayName") if isinstance(provider, dict) else None
        url = None
        for key in ("canonicalUrl", "clickThroughUrl"):
            u = content.get(key)
            if isinstance(u, dict) and u.get("url"):
                url = u["url"]
                break
        summary = (content.get("summary") or "").strip() or None
        if summary and len(summary) > cfg.news_summary_chars:
            summary = summary[: cfg.news_summary_chars]
        rows.append((published, {
            "date": published.date().isoformat(),
            "title": title,
            "source": source or None,
            "summary": summary,
            "url": url,
        }))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r[1] for r in rows[: cfg.news_max_items]]


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN → None


def _extract_analyst(t: Any, *, now: datetime, cfg: EvidenceConfig) -> dict | None:
    """Price targets + rating distribution + recent grade changes. Each of
    the three yfinance properties fails independently; None only when all
    three fail."""
    out: dict[str, Any] = {"price_target": None, "ratings": None, "recent_grades": None}
    failures = 0

    try:
        pt = t.analyst_price_targets or {}
        out["price_target"] = {k: _num(pt.get(k)) for k in ("current", "mean", "high", "low")}
    except Exception:
        failures += 1

    try:
        rec = t.recommendations_summary
        row = None
        if isinstance(rec, pd.DataFrame) and not rec.empty and "period" in rec.columns:
            cur = rec[rec["period"] == "0m"]
            if not cur.empty:
                row = cur.iloc[0]
        if row is not None:
            out["ratings"] = {k: int(_num(row.get(k)) or 0)
                              for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception:
        failures += 1

    try:
        ud = t.upgrades_downgrades
        grades: list[dict] = []
        if isinstance(ud, pd.DataFrame) and not ud.empty:
            cutoff = now - pd.Timedelta(days=cfg.analyst_grades_days)
            for ts, row in ud.sort_index(ascending=False).iterrows():
                ts = pd.Timestamp(ts)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                if ts < cutoff:
                    continue
                grades.append({
                    "date": ts.date().isoformat(),
                    "firm": str(row.get("Firm") or ""),
                    "action": str(row.get("Action") or ""),
                    "from": str(row.get("FromGrade") or ""),
                    "to": str(row.get("ToGrade") or ""),
                })
                if len(grades) >= 5:
                    break
        out["recent_grades"] = grades
    except Exception:
        failures += 1

    return None if failures == 3 else out


def _extract_calendar(t: Any) -> dict | None:
    cal = t.calendar
    if not isinstance(cal, dict) or not cal:
        return None
    dates = cal.get("Earnings Date") or []
    first = dates[0] if isinstance(dates, (list, tuple)) and dates else None
    next_date = (
        (first.date() if isinstance(first, datetime) else first).isoformat()
        if isinstance(first, date)
        else None
    )
    out = {
        "next_earnings_date": next_date,
        "eps_estimate": _num(cal.get("Earnings Average")),
        "revenue_estimate": _num(cal.get("Revenue Average")),
    }
    return out if any(v is not None for v in out.values()) else None


# --- Entry point -------------------------------------------------------------

def fetch_evidence(
    yf_symbol: str,
    market: str,
    cfg: EvidenceConfig,
    *,
    as_of: date | None = None,
) -> dict:
    """Build the evidence bundle for one ticker. Synchronous (yfinance is
    sync); callers wrap it in `asyncio.to_thread` + `wait_for`. Never raises."""
    today = as_of or date.today()
    now = datetime.now(timezone.utc)
    ev = empty_evidence(today)

    try:
        t = yf.Ticker(yf_symbol)
    except Exception as e:
        ev["errors"].append(f"ticker: {type(e).__name__}: {e}")
        if market.lower() != "us":
            ev["filings"] = None
        return ev

    try:
        ev["news"] = _extract_news(t.news, now=now, cfg=cfg)
    except Exception as e:
        ev["errors"].append(f"news: {type(e).__name__}: {e}")
    ev["news_count"] = len(ev["news"])

    try:
        ev["analyst"] = _extract_analyst(t, now=now, cfg=cfg)
    except Exception as e:
        ev["errors"].append(f"analyst: {type(e).__name__}: {e}")

    try:
        ev["calendar"] = _extract_calendar(t)
    except Exception as e:
        ev["errors"].append(f"calendar: {type(e).__name__}: {e}")

    if market.lower() == "us":
        try:
            res = edgar.fetch_recent_filings(
                yf_symbol, days=cfg.filings_days, max_items=cfg.filings_max_items, as_of=today,
            )
            if res is not None:
                ev["filings"], ev["form4_count"] = res
        except Exception as e:
            ev["errors"].append(f"filings: {type(e).__name__}: {e}")
        ev["filings_count"] = len(ev["filings"])
    else:
        ev["filings"] = None
        ev["filings_count"] = 0
    return ev
