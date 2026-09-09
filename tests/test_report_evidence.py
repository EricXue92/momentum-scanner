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


from datetime import datetime, timezone  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pandas as pd  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _news_item(title: str, pub: str, summary: str = "s", provider: str = "Reuters", url: str = "https://x/y"):
    return {
        "id": title,
        "content": {
            "title": title,
            "summary": summary,
            "pubDate": pub,
            "provider": {"displayName": provider},
            "canonicalUrl": {"url": url},
        },
    }


def test_extract_news_filters_age_sorts_and_truncates():
    cfg = evidence.EvidenceConfig(news_max_items=2, news_max_age_hours=24 * 30, news_summary_chars=5)
    raw = [
        _news_item("old", "2026-07-01T00:00:00Z"),                       # > 30 days → dropped
        _news_item("mid", "2026-09-01T10:00:00Z", summary="abcdefgh"),
        _news_item("new", "2026-09-08T10:00:00Z"),
        _news_item("newest", "2026-09-09T01:00:00Z"),
    ]
    out = evidence._extract_news(raw, now=NOW, cfg=cfg)
    assert [n["title"] for n in out] == ["newest", "new"]           # newest first, capped at 2
    assert out[0] == {
        "date": "2026-09-09", "title": "newest", "source": "Reuters",
        "summary": "s", "url": "https://x/y",
    }


def test_extract_news_truncates_summary_and_tolerates_missing_fields():
    cfg = evidence.EvidenceConfig(news_summary_chars=5)
    raw = [
        {"content": {"title": "t", "pubDate": "2026-09-08T10:00:00Z", "summary": "abcdefgh"}},
        {"content": {"title": "bad date", "pubDate": "not-a-date"}},   # dropped
        {"content": {"pubDate": "2026-09-08T10:00:00Z"}},               # no title → dropped
        {"no_content": True},                                            # dropped
    ]
    out = evidence._extract_news(raw, now=NOW, cfg=cfg)
    assert len(out) == 1
    assert out[0]["summary"] == "abcde"
    assert out[0]["source"] is None and out[0]["url"] is None


def _ticker_mock(*, targets=None, rec=None, grades=None, calendar=None) -> MagicMock:
    t = MagicMock()
    t.analyst_price_targets = targets if targets is not None else {
        "current": 69.56, "high": 125.0, "low": 55.0, "mean": 91.6, "median": 93.0}
    t.recommendations_summary = rec if rec is not None else pd.DataFrame(
        [{"period": "0m", "strongBuy": 1, "buy": 3, "hold": 1, "sell": 0, "strongSell": 0},
         {"period": "-1m", "strongBuy": 1, "buy": 3, "hold": 1, "sell": 0, "strongSell": 0}])
    t.upgrades_downgrades = grades if grades is not None else pd.DataFrame(
        {"Firm": ["Needham", "B. Riley"], "ToGrade": ["Buy", "Neutral"],
         "FromGrade": ["Hold", "Buy"], "Action": ["up", "down"]},
        index=pd.to_datetime(["2026-08-20 13:00:00", "2025-01-01 10:00:00"]))
    t.calendar = calendar if calendar is not None else {
        "Earnings Date": [date(2026, 10, 30)], "Earnings Average": 0.308,
        "Revenue Average": 66004800}
    return t


def test_extract_analyst_full():
    cfg = evidence.EvidenceConfig(analyst_grades_days=90)
    out = evidence._extract_analyst(_ticker_mock(), now=NOW, cfg=cfg)
    assert out["price_target"] == {"current": 69.56, "mean": 91.6, "high": 125.0, "low": 55.0}
    assert out["ratings"] == {"strongBuy": 1, "buy": 3, "hold": 1, "sell": 0, "strongSell": 0}
    assert out["recent_grades"] == [
        {"date": "2026-08-20", "firm": "Needham", "action": "up", "from": "Hold", "to": "Buy"}
    ]  # the 2025 downgrade is outside the 90-day window


def test_extract_analyst_partial_failure_keeps_other_keys():
    t = _ticker_mock()
    type(t).recommendations_summary = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    out = evidence._extract_analyst(t, now=NOW, cfg=evidence.EvidenceConfig())
    assert out["ratings"] is None
    assert out["price_target"]["mean"] == 91.6


def test_extract_analyst_all_fail_returns_none():
    t = MagicMock()
    # MagicMock gives every instance its own subclass, so properties set on
    # type(t) affect only this mock.
    for attr in ("analyst_price_targets", "recommendations_summary", "upgrades_downgrades"):
        setattr(type(t), attr, property(lambda self: (_ for _ in ()).throw(RuntimeError("x"))))
    assert evidence._extract_analyst(t, now=NOW, cfg=evidence.EvidenceConfig()) is None


def test_extract_calendar():
    out = evidence._extract_calendar(_ticker_mock())
    assert out == {"next_earnings_date": "2026-10-30", "eps_estimate": 0.308, "revenue_estimate": 66004800}
    assert evidence._extract_calendar(_ticker_mock(calendar={})) is None


def test_fetch_evidence_us_assembles_all_sources(monkeypatch):
    t = _ticker_mock()
    t.news = [_news_item("n1", "2026-09-08T10:00:00Z"), _news_item("n2", "2026-09-07T10:00:00Z")]
    monkeypatch.setattr(evidence.yf, "Ticker", lambda sym: t)
    monkeypatch.setattr(
        evidence.edgar, "fetch_recent_filings",
        lambda ticker, **kw: ([{"form": "8-K", "date": "2026-07-30", "items": "2.02", "description": "8-K", "url": "u"}], 2),
    )
    ev = evidence.fetch_evidence("AXTI", "us", evidence.EvidenceConfig(), as_of=date(2026, 9, 9))
    assert ev["as_of"] == "2026-09-09"
    assert ev["news_count"] == 2 and len(ev["news"]) == 2
    assert ev["filings_count"] == 1 and ev["form4_count"] == 2
    assert ev["analyst"]["price_target"]["mean"] == 91.6
    assert ev["calendar"]["next_earnings_date"] == "2026-10-30"
    assert ev["errors"] == []


def test_fetch_evidence_hk_skips_filings(monkeypatch):
    t = _ticker_mock()
    t.news = []
    monkeypatch.setattr(evidence.yf, "Ticker", lambda sym: t)
    called = []
    monkeypatch.setattr(evidence.edgar, "fetch_recent_filings", lambda *a, **kw: called.append(1))
    ev = evidence.fetch_evidence("0700.HK", "hk", evidence.EvidenceConfig(), as_of=date(2026, 9, 9))
    assert ev["filings"] is None and ev["filings_count"] == 0 and called == []


def test_fetch_evidence_single_source_failure_is_isolated(monkeypatch):
    t = _ticker_mock()
    type(t).news = property(lambda self: (_ for _ in ()).throw(RuntimeError("yf down")))
    monkeypatch.setattr(evidence.yf, "Ticker", lambda sym: t)
    monkeypatch.setattr(evidence.edgar, "fetch_recent_filings", lambda ticker, **kw: None)
    ev = evidence.fetch_evidence("AXTI", "us", evidence.EvidenceConfig(), as_of=date(2026, 9, 9))
    assert ev["news"] == [] and ev["news_count"] == 0
    assert any(e.startswith("news:") for e in ev["errors"])
    assert ev["filings"] == [] and ev["filings_count"] == 0     # None from edgar → [] + no error
    assert ev["analyst"] is not None


def test_fetch_evidence_never_raises_when_ticker_ctor_fails(monkeypatch):
    def boom(sym):
        raise RuntimeError("ctor")
    monkeypatch.setattr(evidence.yf, "Ticker", boom)
    ev = evidence.fetch_evidence("AXTI", "us", evidence.EvidenceConfig(), as_of=date(2026, 9, 9))
    assert ev["news_count"] == 0 and ev["analyst"] is None
    assert any("ticker:" in e for e in ev["errors"])
