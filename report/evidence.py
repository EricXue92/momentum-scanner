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
from datetime import date
from typing import Any

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
