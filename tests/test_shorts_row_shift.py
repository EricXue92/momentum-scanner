"""`filter_shorts` must survive the finviz>=2.0.0 row-parse shift.

Regression: the 2026-07-23 fix (`_finviz_row_shift`) only covered
`run_screener`; `filter_shorts` kept reading `stock["Ticker"]` /
`stock["Market Cap"]` raw, so every row's cap failed to parse and the
pipeline returned 0 before any filter ran (observed 2026-09-12: "Found 15
tickers" → "Final: 0" with no intermediate log lines)."""
import pandas as pd
import pytest

import main


def _shifted_rows() -> list[dict]:
    # Real shape of a finviz 2.0.0 Ownership row today: every value sits one
    # column right of its header; 'Ticker' keeps only the first letter.
    return [
        {"No.": "1", "Ticker": "A", "Market Cap": "ACVA", "Outstanding": "1.77B",
         "Float": "169.12M", "Insider Own": "158.72M", "Insider Trans": "6.53%",
         "Inst Own": "0.00%", "Inst Trans": "97.09%", "Short Float": "0.94%",
         "Short Ratio": "9.85%", "Avg Volume": "2.75", "Price": "5.69M",
         "Change %": "10.42", "Volume": "0.10%"},
        {"No.": "2", "Ticker": "S", "Market Cap": "SMMT", "Outstanding": "12.5B",
         "Float": "80M", "Insider Own": "60M", "Insider Trans": "1%",
         "Inst Own": "0.00%", "Inst Trans": "50%", "Short Float": "2%",
         "Short Ratio": "3%", "Avg Volume": "1.5", "Price": "3M",
         "Change %": "150", "Volume": "1%"},
    ]


def _unshifted_rows() -> list[dict]:
    return [
        {"No.": "1", "Ticker": "ACVA", "Market Cap": "1.77B", "Outstanding": "169.12M"},
        {"No.": "2", "Ticker": "SMMT", "Market Cap": "12.5B", "Outstanding": "80M"},
    ]


class _FakeScreener:
    rows: list[dict] = []
    calls: list[dict] = []

    def __init__(self, **kwargs):
        type(self).calls.append(kwargs)
        self.data = list(type(self).rows)


@pytest.fixture
def capture(monkeypatch):
    """Stub finviz + yfinance; return the ticker list handed to yfinance."""
    seen: dict = {}

    def fake_download(tickers, **kwargs):
        seen["tickers"] = list(tickers)
        # Empty frame → every downstream filter drops; we only assert on
        # what reached the download step.
        return pd.DataFrame()

    _FakeScreener.calls = []
    monkeypatch.setattr(main, "Screener", _FakeScreener)
    monkeypatch.setattr(main, "_yf_download_with_retry", fake_download)
    return seen


def _run():
    return main.filter_shorts(
        ["ind_stocksonly"], None,
        perf_large_cap=50, perf_mid_cap=200, perf_small_cap=300,
        min_dollar_volume=0, min_consecutive_up_days=0,
    )


def test_shifted_rows_yield_real_tickers_and_caps(capture):
    _FakeScreener.rows = _shifted_rows()
    total, _ = _run()
    assert total == 2
    assert capture.get("tickers") == ["ACVA", "SMMT"], (
        "shifted rows must reach yfinance with real tickers, not return early"
    )
    assert _FakeScreener.calls[0].get("table") == "Ownership"


def test_unshifted_rows_still_work(capture):
    _FakeScreener.rows = _unshifted_rows()
    total, _ = _run()
    assert total == 2
    assert capture.get("tickers") == ["ACVA", "SMMT"]
