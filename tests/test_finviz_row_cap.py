"""`run_screener` must not silently stop at finviz's 1000-row page cap.

Finviz 403s every page past r=1000 for a non-Elite session and the finviz
library swallows it, returning the first 1000 rows of a larger result
(2026-10-15: a 1864-row screen came back as 1000). The fix re-queries in
descending ticker order to fetch the tail and unions the two halves."""
import logging

import main

CAP = 1000


def _rows(tickers: list[str]) -> list[dict]:
    return [{"No.": str(i + 1), "Ticker": t, "Company": f"{t} Inc"}
            for i, t in enumerate(tickers)]


def _fake_screener(universe: list[str], calls: list[dict]):
    """Finviz-like: honours `order`, reports the true total, caps at 1000."""

    class FakeScreener:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            ordered = sorted(universe, reverse=kwargs.get("order") == "-ticker")
            self._total_rows = len(universe)
            self.data = _rows(ordered[:CAP])

    return FakeScreener


def _universe(n: int) -> list[str]:
    return [f"T{i:04d}" for i in range(n)]


def test_under_cap_is_a_single_query(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(main, "Screener", _fake_screener(_universe(400), calls))
    assert len(main.run_screener(["f1"])) == 400
    assert len(calls) == 1
    assert "order" not in calls[0]


def test_over_cap_fetches_tail_in_descending_order(monkeypatch):
    calls: list[dict] = []
    universe = _universe(1864)
    monkeypatch.setattr(main, "Screener", _fake_screener(universe, calls))
    got = main.run_screener(["f1"], signal="sig")
    assert sorted(got) == universe
    assert len(got) == len(set(got))
    assert [c.get("order") for c in calls] == [None, "-ticker"]
    assert calls[1]["filters"] == ["f1"] and calls[1]["signal"] == "sig"


def test_over_two_caps_warns_incomplete(monkeypatch, caplog):
    calls: list[dict] = []
    monkeypatch.setattr(main, "Screener", _fake_screener(_universe(2500), calls))
    with caplog.at_level(logging.WARNING):
        got = main.run_screener(["f1"])
    assert len(got) == 2000
    assert "2000/2500" in caplog.text
