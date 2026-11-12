"""Tests for us_rs_3m._fetch_cloud_csv and the rewritten build_3m_table.

These cover the HTTP-fetcher path, the 4-day stale-fallback walk
(today → 1d → 2d → 3d), the local-cache short-circuit on same-day
reruns, and the final passthrough when all fetches fail.

The yfinance/Fred6725 dependencies are gone from build_3m_table after
the rewrite; only HTTP and disk I/O remain.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError

import pandas as pd
import pytest

import us_rs_3m


_FIXTURE_CSV = "ticker,raw_score,rs_percentile\nAAPL,0.12,95\nMSFT,0.08,80\n"


class _FakeResponse:
    """Minimal stand-in for urlopen's return value: context manager + read()."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _fake_urlopen_returning(body: str):
    def _fn(req, timeout=30):
        return _FakeResponse(body.encode("utf-8"))
    return _fn


def _fake_urlopen_raising(exc: Exception):
    def _fn(req, timeout=30):
        raise exc
    return _fn


# ── _fetch_cloud_csv ─────────────────────────────────────────────────────

def test_fetch_cloud_csv_success_returns_dataframe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us_rs_3m, "urlopen", _fake_urlopen_returning(_FIXTURE_CSV))
    table = us_rs_3m._fetch_cloud_csv("https://example.invalid/today.csv")
    assert table is not None
    assert list(table.index) == ["AAPL", "MSFT"]
    assert table.loc["AAPL", "rs_percentile"] == 95


def test_fetch_cloud_csv_404_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        us_rs_3m, "urlopen",
        _fake_urlopen_raising(HTTPError("https://example.invalid/missing.csv", 404, "Not Found", {}, None)),
    )
    assert us_rs_3m._fetch_cloud_csv("https://example.invalid/missing.csv") is None


def test_fetch_cloud_csv_network_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us_rs_3m, "urlopen", _fake_urlopen_raising(URLError("dns failure")))
    assert us_rs_3m._fetch_cloud_csv("https://example.invalid/today.csv") is None


def test_fetch_cloud_csv_malformed_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed body should not raise — caller falls back day-by-day."""
    monkeypatch.setattr(us_rs_3m, "urlopen", _fake_urlopen_returning("not,a,valid\ncsv\n\"unterminated"))
    assert us_rs_3m._fetch_cloud_csv("https://example.invalid/bad.csv") is None


# ── build_3m_table (rewritten fetcher) ───────────────────────────────────

def test_build_3m_table_uses_today_cloud_csv_and_mirrors_to_local_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    today = date(2026, 5, 22)
    fixture = pd.read_csv(io.StringIO(_FIXTURE_CSV), index_col="ticker")

    def _fake_fetch(url, timeout=30):
        assert "2026-05-22.csv" in url, url
        return fixture

    monkeypatch.setattr(us_rs_3m, "_fetch_cloud_csv", _fake_fetch)
    table = us_rs_3m.build_3m_table(tmp_path, today)
    assert table is not None
    assert list(table.index) == ["AAPL", "MSFT"]
    # Local cache mirror should exist for same-day rerun short-circuit
    cache = tmp_path / "state" / "rs_rating_3m_2026-05-22.csv"
    assert cache.exists()


def test_build_3m_table_walks_back_to_stale_fallback_and_does_not_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    today = date(2026, 5, 22)
    fixture = pd.read_csv(io.StringIO(_FIXTURE_CSV), index_col="ticker")
    calls: list[str] = []

    def _fake_fetch(url, timeout=30):
        calls.append(url)
        # Today, 1d, 2d all 404; 3d-old succeeds.
        if "2026-05-19" in url:
            return fixture
        return None

    monkeypatch.setattr(us_rs_3m, "_fetch_cloud_csv", _fake_fetch)
    table = us_rs_3m.build_3m_table(tmp_path, today)
    assert table is not None
    assert len(calls) == 4  # today, -1, -2, -3
    assert "2026-05-22" in calls[0]
    assert "2026-05-19" in calls[3]
    # Stale fallbacks must NOT write the local cache (would mask staleness on next rerun)
    cache = tmp_path / "state" / "rs_rating_3m_2026-05-22.csv"
    assert not cache.exists()


def test_build_3m_table_returns_none_when_all_fetches_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    today = date(2026, 5, 22)
    monkeypatch.setattr(us_rs_3m, "_fetch_cloud_csv", lambda *a, **kw: None)
    table = us_rs_3m.build_3m_table(tmp_path, today)
    assert table is None


def test_build_3m_table_local_cache_short_circuit_skips_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If output/state/rs_rating_3m_<today>.csv exists, no HTTP at all."""
    today = date(2026, 5, 22)
    fixture = pd.read_csv(io.StringIO(_FIXTURE_CSV), index_col="ticker")
    us_rs_3m.save_cache(fixture, today, tmp_path)

    def _fail_on_fetch(*a, **kw):
        pytest.fail("_fetch_cloud_csv should not be called when local cache hits")

    monkeypatch.setattr(us_rs_3m, "_fetch_cloud_csv", _fail_on_fetch)
    table = us_rs_3m.build_3m_table(tmp_path, today)
    assert table is not None
    assert list(table.index) == ["AAPL", "MSFT"]


def test_build_3m_table_saturday_walkback_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
) -> None:
    """The Tue-Sat launchd dispatch publishes a Saturday-stamped CSV (since
    2026-06-16), so a Saturday run that walks back to Friday's file means the
    trigger failed — a stale table like any other weekday, logged at WARNING
    (it used to be INFO "cloud cron runs Mon-Fri", which hid the failure)."""
    import logging

    saturday = date(2026, 5, 23)
    assert saturday.weekday() == 5, "test relies on 2026-05-23 being a Saturday"
    fixture = pd.read_csv(io.StringIO(_FIXTURE_CSV), index_col="ticker")

    def _fake_fetch(url, timeout=30):
        if "2026-05-22" in url:  # Friday's date — succeeds
            return fixture
        return None  # Saturday's URL — 404

    monkeypatch.setattr(us_rs_3m, "_fetch_cloud_csv", _fake_fetch)
    with caplog.at_level(logging.INFO, logger="us_rs_3m"):
        table = us_rs_3m.build_3m_table(tmp_path, saturday)

    assert table is not None
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("stale fallback" in m and "2026-05-22" in m for m in warnings), warnings


def test_build_3m_table_non_saturday_walkback_still_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
) -> None:
    """Regression guard: a 1-day walkback on a NON-Saturday (e.g. cloud cron
    missed Monday) should still log WARNING — that case is genuinely stale."""
    import logging

    tuesday = date(2026, 5, 26)
    assert tuesday.weekday() == 1, "test relies on 2026-05-26 being a Tuesday"
    fixture = pd.read_csv(io.StringIO(_FIXTURE_CSV), index_col="ticker")

    def _fake_fetch(url, timeout=30):
        if "2026-05-25" in url:  # Monday — succeeds
            return fixture
        return None  # Tuesday 404 (cron failed)

    monkeypatch.setattr(us_rs_3m, "_fetch_cloud_csv", _fake_fetch)
    with caplog.at_level(logging.INFO, logger="us_rs_3m"):
        table = us_rs_3m.build_3m_table(tmp_path, tuesday)

    assert table is not None
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("stale fallback" in m for m in warnings), f"expected stale warning, got: {warnings}"
