"""Every Finviz call site retries on a transient network error, not just the
conditional RS group. A DNS blip during a Longs / Leaders / Shorts screener
lost that list exactly like the 2026-10-17 RS loss; the retry belongs inside
`run_screener` / `check_market_down`. A deterministic Finviz 4xx
(requests.HTTPError subclasses OSError) must NOT be retried.
"""
from __future__ import annotations

import pytest
import requests

import main


class _FakeScreener:
    def __init__(self, data):
        self.data = data
        self._total_rows = len(data)


def _rows(*tickers: str) -> list[dict]:
    return [{"Ticker": t, "Company": f"{t} Inc"} for t in tickers]


@pytest.fixture(autouse=True)
def _instant_wait(monkeypatch):
    monkeypatch.setattr(main, "wait_for_network", lambda *a, **k: True)


def test_run_screener_retries_connection_error(monkeypatch):
    calls = {"n": 0}

    def fake_screener(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("NameResolutionError")
        return _FakeScreener(_rows("AAPL", "MSFT"))

    monkeypatch.setattr(main, "Screener", fake_screener)
    assert main.run_screener(["f1"]) == ["AAPL", "MSFT"]
    assert calls["n"] == 2


def test_run_screener_does_not_retry_http_4xx(monkeypatch):
    calls = {"n": 0}

    def fake_screener(**kwargs):
        calls["n"] += 1
        resp = requests.Response()
        resp.status_code = 403
        raise requests.exceptions.HTTPError("403 Client Error", response=resp)

    monkeypatch.setattr(main, "Screener", fake_screener)
    with pytest.raises(requests.exceptions.HTTPError):
        main.run_screener(["f1"])
    assert calls["n"] == 1


def test_run_screener_retries_http_5xx(monkeypatch):
    calls = {"n": 0}

    def fake_screener(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            resp = requests.Response()
            resp.status_code = 502
            raise requests.exceptions.HTTPError("502 Bad Gateway", response=resp)
        return _FakeScreener(_rows("NVDA"))

    monkeypatch.setattr(main, "Screener", fake_screener)
    assert main.run_screener(["f1"]) == ["NVDA"]
    assert calls["n"] == 2


def test_check_market_down_retries_connection_error(monkeypatch):
    calls = {"n": 0}

    def fake_get_stock(ticker):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("down")
        return {"Change %": "-1.50%"}

    monkeypatch.setattr(main, "get_stock", fake_get_stock)
    assert main.check_market_down() is True
    assert calls["n"] == 3  # SPY failed once, then SPY + QQQ


def test_run_screener_retries_http_429(monkeypatch):
    """429 is Finviz's rate limit on the redesigned screener — transient."""
    calls = {"n": 0}

    def fake_screener(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            resp = requests.Response()
            resp.status_code = 429
            raise requests.exceptions.HTTPError("429 Too Many Requests", response=resp)
        return _FakeScreener(_rows("NVDA"))

    monkeypatch.setattr(main, "Screener", fake_screener)
    assert main.run_screener(["f1"]) == ["NVDA"]
    assert calls["n"] == 2
