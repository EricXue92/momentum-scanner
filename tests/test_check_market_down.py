"""check_market_down reads the day's % change from finviz get_stock().

Finviz renamed the ETF quote field `Change` → `Change %` (seen 2026-10-15):
the old lookup KeyError'd, the RS block's `except` swallowed it as
`[RS] Failed: 'Change'`, and the conditional RS group silently skipped on
2026-10-14 (SPY -1.08%, QQQ -1.97%).
"""

import pytest

import main


def _patch_quotes(monkeypatch, quotes):
    monkeypatch.setattr(main, "get_stock", lambda t: quotes[t])


@pytest.mark.parametrize("key", ["Change %", "Change"])
def test_reads_either_change_key(monkeypatch, key):
    _patch_quotes(
        monkeypatch,
        {"SPY": {key: "-1.08%"}, "QQQ": {key: "-1.97%"}},
    )
    assert main.check_market_down() is True


def test_falls_back_to_price_vs_prev_close(monkeypatch):
    _patch_quotes(
        monkeypatch,
        {
            "SPY": {"Price": "98.00", "Prev Close": "100.00"},
            "QQQ": {"Price": "97.00", "Prev Close": "100.00"},
        },
    )
    assert main.check_market_down() is True


def test_one_index_not_down_enough(monkeypatch):
    _patch_quotes(
        monkeypatch,
        {"SPY": {"Change %": "-0.21%"}, "QQQ": {"Change %": "-1.97%"}},
    )
    assert main.check_market_down() is False
