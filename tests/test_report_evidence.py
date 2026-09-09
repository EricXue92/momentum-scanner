"""Tests for report/evidence.py — pre-fetched evidence bundle."""
from __future__ import annotations

from datetime import date

import pytest

from report import evidence


def test_config_from_dict_defaults():
    cfg = evidence.EvidenceConfig.from_dict(None)
    assert cfg.enabled is True
    assert cfg.news_max_items == 10
    assert cfg.news_max_age_hours == 720
    assert cfg.min_items_for_no_search == 3
    assert cfg.fallback_search_calls == 1
    assert cfg.timeout_seconds == 20.0


def test_config_from_dict_overrides_and_ignores_unknown_keys():
    cfg = evidence.EvidenceConfig.from_dict(
        {"enabled": False, "news_max_items": 3, "timeout_seconds": 5, "bogus": 1}
    )
    assert cfg.enabled is False
    assert cfg.news_max_items == 3
    assert cfg.timeout_seconds == 5.0


def test_empty_evidence_shape():
    ev = evidence.empty_evidence(date(2026, 9, 9), errors=["timeout"])
    assert ev["as_of"] == "2026-09-09"
    assert ev["news"] == [] and ev["filings"] == []
    assert ev["analyst"] is None and ev["calendar"] is None
    assert ev["news_count"] == 0 and ev["filings_count"] == 0
    assert ev["form4_count"] == 0
    assert ev["errors"] == ["timeout"]


def _ev(news: int, filings: int) -> dict:
    ev = evidence.empty_evidence(date(2026, 9, 9))
    ev["news_count"] = news
    ev["filings_count"] = filings
    return ev


def test_search_budget_disabled_returns_none():
    cfg = evidence.EvidenceConfig(enabled=False)
    assert evidence.search_budget(_ev(0, 0), cfg) is None
    assert evidence.search_budget(None, cfg) is None


def test_search_budget_no_fallback_returns_zero():
    cfg = evidence.EvidenceConfig(search_fallback=False)
    assert evidence.search_budget(_ev(0, 0), cfg) == 0


def test_search_budget_enough_items_returns_zero():
    cfg = evidence.EvidenceConfig(min_items_for_no_search=3)
    assert evidence.search_budget(_ev(2, 1), cfg) == 0
    assert evidence.search_budget(_ev(10, 0), cfg) == 0


def test_search_budget_too_few_items_returns_fallback():
    cfg = evidence.EvidenceConfig(min_items_for_no_search=3, fallback_search_calls=1)
    assert evidence.search_budget(_ev(2, 0), cfg) == 1
    # None evidence (prefetch failed entirely) counts as zero items.
    assert evidence.search_budget(None, cfg) == 1


def test_summarize_for_log():
    ev = _ev(10, 6)
    ev["analyst"] = {"price_target": {"mean": 1.0}}
    assert evidence.summarize_for_log(ev) == "news=10 filings=6 analyst=yes calendar=no"
    assert evidence.summarize_for_log(None) == "news=0 filings=0 analyst=no calendar=no"
