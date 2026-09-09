import json
import os
import time
from pathlib import Path

import pytest

from report import edgar


def test_is_fresh_returns_false_when_file_missing(tmp_path: Path):
    assert edgar._is_fresh(tmp_path / "missing.json", ttl_seconds=10) is False


def test_is_fresh_returns_true_for_fresh_file(tmp_path: Path):
    p = tmp_path / "fresh.json"
    p.write_text("{}")
    assert edgar._is_fresh(p, ttl_seconds=86400) is True


def test_is_fresh_returns_false_for_stale_file(tmp_path: Path):
    p = tmp_path / "stale.json"
    p.write_text("{}")
    old = time.time() - 100
    os.utime(p, (old, old))
    assert edgar._is_fresh(p, ttl_seconds=10) is False


def test_save_and_load_json_cache_roundtrip(tmp_path: Path):
    p = tmp_path / "data.json"
    edgar._save_json_cache(p, {"hello": 1})
    assert edgar._load_json_cache(p) == {"hello": 1}


def test_load_json_cache_returns_none_for_corrupt_file_and_deletes_it(tmp_path: Path):
    p = tmp_path / "corrupt.json"
    p.write_text("{not json")
    assert edgar._load_json_cache(p) is None
    assert not p.exists()


def test_load_json_cache_returns_none_when_missing(tmp_path: Path):
    assert edgar._load_json_cache(tmp_path / "missing.json") is None


def test_http_get_json_returns_payload_on_200(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"ok": True}
        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResp()

    monkeypatch.setattr(edgar.httpx, "get", fake_get)
    assert edgar._http_get_json("https://x") == {"ok": True}
    assert calls["n"] == 1


def test_http_get_json_retries_once_on_5xx(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, code):
            self.status_code = code
        def json(self):
            return {"ok": True}
        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResp(500 if calls["n"] == 1 else 200)

    monkeypatch.setattr(edgar.httpx, "get", fake_get)
    monkeypatch.setattr(edgar.time, "sleep", lambda s: None)
    assert edgar._http_get_json("https://x") == {"ok": True}
    assert calls["n"] == 2


def test_http_get_json_returns_none_after_two_failures(monkeypatch):
    class FakeResp:
        status_code = 503
        def raise_for_status(self):
            pass

    monkeypatch.setattr(edgar.httpx, "get", lambda *a, **kw: FakeResp())
    monkeypatch.setattr(edgar.time, "sleep", lambda s: None)
    assert edgar._http_get_json("https://x") is None


def test_http_get_json_returns_none_on_404_no_retry(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        status_code = 404
        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return FakeResp()

    monkeypatch.setattr(edgar.httpx, "get", fake_get)
    assert edgar._http_get_json("https://x") is None
    assert calls["n"] == 1   # 404 = "company not in EDGAR", do not retry


FIXTURES = Path(__file__).parent / "fixtures" / "edgar"


def test_parse_ticker_cik_map_zero_pads_cik():
    raw = json.loads((FIXTURES / "company_tickers.json").read_text())
    table = edgar._parse_ticker_cik_map(raw)
    assert table["AAPL"] == "0000320193"
    assert table["V"] == "0001403161"
    assert table["GOOGL"] == "0000001652044"[-10:]   # 10-digit zero-padded


def test_parse_ticker_cik_map_uppercases_ticker():
    raw = {"0": {"cik_str": 1, "ticker": "tsla", "title": "Tesla"}}
    table = edgar._parse_ticker_cik_map(raw)
    assert "TSLA" in table


def test_get_cik_uses_cache_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    cache_path = tmp_path / "company_tickers.json"
    cache_path.write_text(
        (FIXTURES / "company_tickers.json").read_text()
    )
    # _get_cik should not hit the network when cache is fresh
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: pytest.fail("network hit"))
    edgar._cached_ticker_map = None    # reset module-level memo
    assert edgar._get_cik("AAPL") == "0000320193"


def test_get_cik_returns_none_for_unknown_ticker(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    (tmp_path / "company_tickers.json").write_text(
        (FIXTURES / "company_tickers.json").read_text()
    )
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: None)
    edgar._cached_ticker_map = None
    assert edgar._get_cik("ZZZZ") is None


def test_get_cik_returns_none_when_network_and_cache_both_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: None)
    edgar._cached_ticker_map = None
    assert edgar._get_cik("AAPL") is None


def test_fetch_companyfacts_uses_cache_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    cik = "0000320193"
    cache_path = tmp_path / f"CIK{cik}.json"
    cache_path.write_text('{"facts": {"us-gaap": {}}}')
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: pytest.fail("network hit"))
    assert edgar._fetch_companyfacts(cik) == {"facts": {"us-gaap": {}}}


def test_fetch_companyfacts_fetches_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    cik = "0000320193"
    payload = {"facts": {"us-gaap": {"Revenues": {}}}}
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: payload)
    got = edgar._fetch_companyfacts(cik)
    assert got == payload
    # Cache should now exist.
    assert (tmp_path / f"CIK{cik}.json").is_file()


def test_fetch_companyfacts_returns_none_when_404(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: None)
    assert edgar._fetch_companyfacts("0000000001") is None


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_match_concept_returns_first_match(monkeypatch):
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    out = edgar._match_concept_facts(
        facts, ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"), "USD"
    )
    assert out is not None
    assert any(f["fy"] == 2024 and f["fp"] == "FY" for f in out)


def test_match_concept_falls_back_when_first_missing():
    facts = _load_fixture("companyfacts_v_alt_revenue.json")
    out = edgar._match_concept_facts(
        facts, ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"), "USD"
    )
    assert out is not None
    # Should have used the alt concept.
    assert any(f["fy"] == 2024 and f["fp"] == "FY" for f in out)


def test_match_concept_returns_none_when_no_match():
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    assert edgar._match_concept_facts(facts, ("NoSuchConcept",), "USD") is None


def test_match_concept_handles_missing_unit():
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    # Revenues exists but only with USD unit; asking for EUR returns None.
    assert edgar._match_concept_facts(facts, ("Revenues",), "EUR") is None


def test_select_annual_facts_filters_to_10k_fy_and_sorts():
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    raw = edgar._match_concept_facts(facts, ("Revenues",), "USD")
    annual = edgar._select_annual_facts(raw)
    # 6 fiscal years, oldest first
    assert len(annual) == 6
    assert annual[0]["fy"] == 2020
    assert annual[-1]["fy"] == 2025


def test_select_annual_facts_dedupes_amendments(tmp_path):
    raw = [
        {"start": "2023-10-01", "end": "2024-09-28", "val": 100, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"},
        {"start": "2023-10-01", "end": "2024-09-28", "val": 105, "fy": 2024, "fp": "FY", "form": "10-K/A", "filed": "2025-02-01"},
    ]
    out = edgar._select_annual_facts(raw)
    assert len(out) == 1
    # Latest filed wins (the amendment).
    assert out[0]["val"] == 105


def test_extract_annual_yoy_5y_full_history():
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    raw = edgar._match_concept_facts(facts, ("Revenues",), "USD")
    yoy = edgar._extract_annual_yoy(raw, years_back=5)
    assert len(yoy) == 5
    # Oldest first: FY2021 vs FY2020 = (365817 - 274515) / 274515 = +33.26%
    assert yoy[0] == pytest.approx(33.26, rel=0.01)
    # Newest: FY2025 vs FY2024 = (416000 - 391035) / 391035 = +6.38%
    assert yoy[-1] == pytest.approx(6.38, rel=0.01)


def test_extract_annual_yoy_pads_with_none_when_history_short():
    raw = [
        {"start": "2023-10-01", "end": "2024-09-28", "val": 100, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"},
        {"start": "2024-09-29", "end": "2025-09-27", "val": 110, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-11-01"},
    ]
    yoy = edgar._extract_annual_yoy(raw, years_back=5)
    assert yoy == [None, None, None, None, pytest.approx(10.0, rel=0.01)]


def test_extract_annual_yoy_handles_negative_prior_via_abs_denominator():
    raw = [
        {"start": "2022-10-02", "end": "2023-09-30", "val": -10, "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-03"},
        {"start": "2023-10-01", "end": "2024-09-28", "val":  20, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"},
        {"start": "2024-09-29", "end": "2025-09-27", "val":  30, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-11-01"},
    ]
    yoy = edgar._extract_annual_yoy(raw, years_back=5)
    # FY24 vs FY23: loss -10 → profit +20 = (20 - -10)/abs(-10) = +300% (turned positive)
    assert yoy[-2] == pytest.approx(300.0, rel=0.01)
    # FY25 vs FY24: 20 → 30 = +50%
    assert yoy[-1] == pytest.approx(50.0, rel=0.01)


def test_select_quarterly_facts_keeps_current_quarter_when_comparative_shares_fy():
    """Reproduces the INOD 2026-Q1 bug: when a 10-Q is filed, XBRL embeds
    the prior-year comparative period under the SAME fy (the FILING's fy,
    not the period's fy) and SAME fp as the current period. Both rows have
    identical `filed`. The old `(fy, fp)` dedup key collided and silently
    picked the comparative — i.e. the report froze on last year's Q1 even
    though this year's Q1 was already in EDGAR. Dedup must key on `end`."""
    raw = [
        # Comparative period (last year Q1) embedded in this year's 10-Q
        {"start": "2025-01-01", "end": "2025-03-31", "val": 0.22,
         "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2026-05-07"},
        # Actual current period (this year Q1)
        {"start": "2026-01-01", "end": "2026-03-31", "val": 0.42,
         "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2026-05-07"},
    ]
    quarters = edgar._select_quarterly_facts(raw)
    # Both quarters should survive — different `end` dates uniquely identify them.
    assert len(quarters) == 2
    by_end = {q["end"]: q["val"] for q in quarters}
    assert by_end["2025-03-31"] == 0.22
    assert by_end["2026-03-31"] == 0.42
    # Latest-quarter helper should return the newer one.
    val, _ = edgar._latest_quarter_with_yoy(raw)
    assert val == 0.42


def test_select_quarterly_facts_rejects_ytd_periods_under_q_tag():
    """Apple files BOTH 90-day Q2 (the actual quarter) AND 181-day Q2 (H1 YTD)
    under fp=Q2. Without a period-length filter, dedup-by-(fy,fp) picks one
    arbitrarily and the YTD value inflates derived Q4. Verify only the
    3-month period survives."""
    raw = [
        {"start": "2023-12-31", "end": "2024-03-30", "val": 90, "fy": 2024, "fp": "Q2", "form": "10-Q", "filed": "2024-05-02"},  # 90 days, real quarter
        {"start": "2023-09-30", "end": "2024-03-30", "val": 200, "fy": 2024, "fp": "Q2", "form": "10-Q", "filed": "2024-05-02"},  # 182 days, H1 YTD
    ]
    quarters = edgar._select_quarterly_facts(raw)
    assert len(quarters) == 1
    assert quarters[0]["val"] == 90


def test_select_quarterly_facts_uses_10q_directly_for_q1_q2_q3():
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    raw = edgar._match_concept_facts(facts, ("Revenues",), "USD")
    quarters = edgar._select_quarterly_facts(raw)
    # Should include Apple's Q1 2024 = 119575000000
    found = [q for q in quarters if q["end"] == "2023-12-30"]
    assert len(found) == 1
    assert found[0]["val"] == 119575000000


def test_select_quarterly_facts_derives_q4_from_fy_minus_q1q2q3():
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    raw = edgar._match_concept_facts(facts, ("Revenues",), "USD")
    quarters = edgar._select_quarterly_facts(raw)
    # FY2024 Q4 = 391035 - (119575 + 90753 + 85777) = 94930 (in millions: 94930000000)
    fy24_q4 = [q for q in quarters if q.get("fy") == 2024 and q.get("fp") == "Q4"]
    assert len(fy24_q4) == 1
    assert fy24_q4[0]["val"] == 391035000000 - (119575000000 + 90753000000 + 85777000000)


def test_select_quarterly_facts_skips_q4_when_a_component_missing():
    facts = _load_fixture("companyfacts_q2_gap.json")
    raw = edgar._match_concept_facts(facts, ("Revenues",), "USD")
    quarters = edgar._select_quarterly_facts(raw)
    # FY2024 Q2 missing → no FY2024 Q4 emitted.
    fy24_q4 = [q for q in quarters if q.get("fy") == 2024 and q.get("fp") == "Q4"]
    assert fy24_q4 == []
    # FY2023 Q4 should still be present (220 + 240 + 260 = 720; FY=1000; Q4=280).
    fy23_q4 = [q for q in quarters if q.get("fy") == 2023 and q.get("fp") == "Q4"]
    assert len(fy23_q4) == 1
    assert fy23_q4[0]["val"] == 280


def test_extract_quarterly_yoy_4q_with_full_history():
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    raw = edgar._match_concept_facts(facts, ("Revenues",), "USD")
    yoy, labels = edgar._extract_quarterly_yoy(raw, n_quarters=4)
    assert len(yoy) == 4
    assert len(labels) == 4
    # Fixture has FY2025 10-K → Q4 FY2025 is derived (110.6B vs Q4 FY2024 94.93B ≈ +16.5%).
    # The last 4 quarters chronologically: Q1/Q2/Q3 FY2025 + derived Q4 FY2025.
    # Q3 FY2025 (Jun'25): 85.7B vs Q3 FY2024 (Jun'24): 85.777B ≈ -0.09%
    assert yoy[-2] == pytest.approx(-0.09, abs=0.5)
    # Q4 FY2025 (Sep'25): derived 110.6B vs Q4 FY2024 94.93B ≈ +16.5%
    assert yoy[-1] == pytest.approx(16.5, abs=0.5)
    # Each label is "Mon'YY"
    for lbl in labels:
        assert len(lbl) == 6
        assert lbl[3] == "'"


def test_extract_quarterly_yoy_pads_when_short_history():
    raw = [
        {"start": "2024-10-01", "end": "2024-12-31", "val": 100, "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-02-01"},
    ]
    yoy, labels = edgar._extract_quarterly_yoy(raw, n_quarters=4)
    assert yoy == [None, None, None, None]
    assert labels[-1] == "Dec'24"


def test_latest_quarter_value_returns_newest_quarter():
    facts = _load_fixture("companyfacts_aapl_minimal.json")
    raw = edgar._match_concept_facts(facts, ("Revenues",), "USD")
    val, yoy = edgar._latest_quarter_with_yoy(raw)
    # Newest after Q4 derivation is FY2025 Q4 (end 2025-09-27)
    # = 416000M - (124300 + 95400 + 85700)M = 110600M
    assert val == 110_600_000_000
    # Q4 YoY: (110600 - 94930) / 94930 ≈ +16.51%
    assert yoy == pytest.approx(16.51, abs=0.5)


def test_latest_quarter_value_returns_none_for_empty():
    val, yoy = edgar._latest_quarter_with_yoy([])
    assert val is None and yoy is None


def test_fetch_edgar_fundamentals_full_path(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    edgar._cached_ticker_map = {"AAPL": "0000320193"}
    cf = _load_fixture("companyfacts_aapl_minimal.json")

    def fake_companyfacts(cik):
        assert cik == "0000320193"
        return cf

    monkeypatch.setattr(edgar, "_fetch_companyfacts", fake_companyfacts)
    out = edgar.fetch_edgar_fundamentals("AAPL")
    assert out is not None
    # Latest quarter is derived Q4 FY2025 (end 2025-09-27):
    # Revenue = 416000M - (124300+95400+85700)M = 110600M
    assert out["revenue_latest_q"] == 110_600_000_000
    # EPS Q4 FY2025 = 7.40 - (2.40+1.65+1.55) = 1.80
    assert out["eps_latest_q"] == pytest.approx(1.80)
    assert len(out["annual_revenue_yoy_5y"]) == 5
    assert len(out["quarterly_revenue_yoy_4q"]) == 4
    assert len(out["quarterly_revenue_yoy_4q_labels"]) == 4
    assert out["annual_revenue_yoy_5y"][0] == pytest.approx(33.26, rel=0.01)


def test_fetch_edgar_fundamentals_returns_none_when_cik_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    edgar._cached_ticker_map = {}   # no tickers known
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: {})  # also empty over network
    assert edgar.fetch_edgar_fundamentals("ZZZZ") is None


def test_fetch_edgar_fundamentals_returns_none_when_companyfacts_404(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    edgar._cached_ticker_map = {"AAPL": "0000320193"}
    monkeypatch.setattr(edgar, "_fetch_companyfacts", lambda cik: None)
    assert edgar.fetch_edgar_fundamentals("AAPL") is None


def test_fetch_edgar_fundamentals_alt_revenue_concept(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    edgar._cached_ticker_map = {"V": "0001403161"}
    cf = _load_fixture("companyfacts_v_alt_revenue.json")
    monkeypatch.setattr(edgar, "_fetch_companyfacts", lambda cik: cf)
    out = edgar.fetch_edgar_fundamentals("V")
    assert out is not None
    # 5 fiscal years → 4 YoY pairs filled, oldest slot None.
    assert out["annual_revenue_yoy_5y"][0] is None
    assert out["annual_revenue_yoy_5y"][-1] == pytest.approx(10.02, rel=0.01)


def _build_concept(facts: list[dict]) -> dict:
    return {"units": {"USD": facts}}


def test_match_concept_picks_fresher_when_first_priority_concept_is_stale():
    """SSRM-pattern: company switched XBRL tagging mid-history, so the
    first-priority concept (RFCWCEAT) has stale quarterly facts and the
    second-priority concept (Revenues) has the current ones. Selector must
    pick whichever concept has the more-recent quarterly survivor — not
    the first-priority concept by default. Verified against SSR Mining
    where naive priority ordering gave Q4 2023 ($414M) instead of TV's
    Q1 2026 ($582M)."""
    stale_facts = [
        {"end": "2023-06-30", "start": "2023-04-01", "val": 301_000_000,
         "fy": 2023, "fp": "Q2", "form": "10-Q", "filed": "2023-08-02"},
        {"end": "2023-09-30", "start": "2023-07-01", "val": 385_000_000,
         "fy": 2023, "fp": "Q3", "form": "10-Q", "filed": "2023-11-01"},
    ]
    fresh_facts = [
        {"end": "2025-06-30", "start": "2025-04-01", "val": 405_000_000,
         "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-08-05"},
        {"end": "2025-09-30", "start": "2025-07-01", "val": 385_000_000,
         "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-11-04"},
        {"end": "2026-03-31", "start": "2026-01-01", "val": 581_778_000,
         "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": "2026-05-05"},
    ]
    cf = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": _build_concept(stale_facts),
        "Revenues": _build_concept(fresh_facts),
    }}}
    out = edgar._match_concept_facts(
        cf,
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
        "USD",
    )
    assert out is not None
    assert any(f["end"] == "2026-03-31" for f in out), \
        "must pick the concept with the fresh 2026-03-31 fact"


def test_match_concept_keeps_first_priority_when_second_has_no_quarterly_survivors():
    """AAPL-pattern: `Revenues` exists with sparse FY-only / segment facts
    that don't survive the quarterly period-length filter, while RFCWCEAT
    has the full quarterly history. The original priority order was set
    precisely to avoid `Revenues` here — confirm it still wins when
    `Revenues` has 0 quarterly survivors."""
    rfc_facts = [
        {"end": "2026-03-28", "start": "2025-12-29", "val": 111_184_000_000,
         "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-05-01"},
    ]
    # `Revenues` only has annual / segment-level entries — no 80-100 day
    # quarterly survivor.
    sparse_revenues = [
        {"end": "2024-09-28", "start": "2023-09-30", "val": 391_000_000_000,
         "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"},
    ]
    cf = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": _build_concept(rfc_facts),
        "Revenues": _build_concept(sparse_revenues),
    }}}
    out = edgar._match_concept_facts(
        cf,
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
        "USD",
    )
    assert out is not None
    assert any(f["val"] == 111_184_000_000 for f in out), \
        "must keep RFCWCEAT when Revenues has no quarterly survivors"


def test_match_concept_falls_back_to_priority_order_when_no_quarterly_anywhere():
    """All concepts have only annual / pre-IPO facts. Quarterly-recency rule
    can't pick a winner — fall back to priority order so older annual code
    paths still get the first-priority concept."""
    annual_only_a = [
        {"end": "2024-12-31", "start": "2024-01-01", "val": 1_000_000_000,
         "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-15"},
    ]
    annual_only_b = [
        {"end": "2024-12-31", "start": "2024-01-01", "val": 999_999_999,
         "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-15"},
    ]
    cf = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": _build_concept(annual_only_a),
        "Revenues": _build_concept(annual_only_b),
    }}}
    out = edgar._match_concept_facts(
        cf,
        ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
        "USD",
    )
    assert out is not None
    assert any(f["val"] == 1_000_000_000 for f in out), \
        "first-priority concept wins when neither has quarterly survivors"


# --- fetch_recent_filings ----------------------------------------------------

from datetime import date as _date  # noqa: E402


def _submissions_payload() -> dict:
    return {
        "cik": 1051627,
        "filings": {
            "recent": {
                "form": ["4", "4", "144", "10-Q", "8-K", "SC 13G/A", "8-K", "4"],
                "filingDate": [
                    "2026-08-19", "2026-08-19", "2026-08-17", "2026-08-13",
                    "2026-07-30", "2026-08-12", "2026-05-01", "2026-05-02",
                ],
                "items": ["", "", "", "", "2.02,9.01", "", "5.02", ""],
                "primaryDocDescription": ["FORM 4", "FORM 4", "", "FORM 10-Q", "8-K", "", "8-K", "FORM 4"],
                "accessionNumber": [
                    "0001051627-26-000010", "0001051627-26-000011", "0001051627-26-000012",
                    "0001051627-26-000013", "0001051627-26-000009", "0001051627-26-000014",
                    "0001051627-26-000005", "0001051627-26-000006",
                ],
                "primaryDocument": [
                    "f4.xml", "f4.xml", "f144.pdf", "axti-10q.htm",
                    "axti-8k.htm", "sc13g.htm", "axti-8k-may.htm", "f4.xml",
                ],
            }
        },
    }


def test_fetch_recent_filings_filters_forms_window_and_counts_form4(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0001051627")
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: _submissions_payload())

    result = edgar.fetch_recent_filings(
        "AXTI", days=60, max_items=8, as_of=_date(2026, 9, 9)
    )
    assert result is not None
    filings, form4_count = result
    # Window = 2026-07-11..2026-09-09: drops the two May filings.
    forms = [f["form"] for f in filings]
    assert forms == ["10-Q", "SC 13G/A", "8-K"]          # newest first, Form 4 / 144 excluded
    assert form4_count == 2                              # only the two August Form 4s
    eightk = filings[-1]
    assert eightk["date"] == "2026-07-30"
    assert eightk["items"] == "2.02,9.01"
    assert eightk["description"] == "8-K"
    assert eightk["url"] == (
        "https://www.sec.gov/Archives/edgar/data/1051627/000105162726000009/axti-8k.htm"
    )
    # Cache written for reuse.
    assert (tmp_path / "submissions_CIK0001051627.json").is_file()


def test_fetch_recent_filings_respects_max_items(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0001051627")
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: _submissions_payload())
    filings, _ = edgar.fetch_recent_filings("AXTI", days=60, max_items=2, as_of=_date(2026, 9, 9))
    assert [f["form"] for f in filings] == ["10-Q", "SC 13G/A"]


def test_fetch_recent_filings_uses_fresh_cache_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0001051627")
    edgar._save_json_cache(tmp_path / "submissions_CIK0001051627.json", _submissions_payload())
    calls = []
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: calls.append(url) or None)
    result = edgar.fetch_recent_filings("AXTI", days=60, max_items=8, as_of=_date(2026, 9, 9))
    assert result is not None and calls == []


def test_fetch_recent_filings_returns_none_when_cik_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: None)
    assert edgar.fetch_recent_filings("ZZZZ", days=60, max_items=8) is None


def test_fetch_recent_filings_returns_none_when_network_and_cache_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0001051627")
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: None)
    assert edgar.fetch_recent_filings("AXTI", days=60, max_items=8) is None


def test_fetch_recent_filings_tolerates_ragged_arrays(tmp_path, monkeypatch):
    """SEC occasionally ships arrays of unequal length; zip must not blow up
    and missing `items` must render as empty string."""
    monkeypatch.setattr(edgar, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(edgar, "_get_cik", lambda t: "0000000001")
    payload = {"filings": {"recent": {
        "form": ["8-K", "10-K"], "filingDate": ["2026-09-01", "2026-08-01"],
        "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002"],
        "primaryDocument": ["a.htm", "b.htm"],
        # no `items`, and description shorter than the others
        "primaryDocDescription": ["8-K"],
    }}}
    monkeypatch.setattr(edgar, "_http_get_json", lambda url: payload)
    filings, form4 = edgar.fetch_recent_filings("X", days=60, max_items=8, as_of=_date(2026, 9, 9))
    assert [f["form"] for f in filings] == ["8-K", "10-K"]
    assert filings[0]["items"] == "" and filings[1]["description"] == ""
    assert form4 == 0
