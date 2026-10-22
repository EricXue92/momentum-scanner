"""Trend-gate behavior for morning-gap: live-price basis + big-gap SMA50 bypass.

Fixtures mirror the real 2026-08-12 pre-market scan, where the gate dropped 67
of 85 survivors — including NBIS and CRWV, both of which gapped hard and
reclaimed their moving averages on the very bar `_trim_today` removes.
"""

import pandas as pd
import pytest

from main import _filter_sma_trend

TODAY = pd.Timestamp("2026-08-12").date()


def _frame(ticker: str, closes: list[float]) -> pd.DataFrame:
    """yfinance batch shape: MultiIndex columns (ticker, field).

    The last row is dated TODAY so `_trim_today` drops it, exactly as it does
    during a live intraday scan.
    """
    idx = pd.date_range(end="2026-08-12", periods=len(closes), freq="B")
    cols = pd.MultiIndex.from_product([[ticker], ["Close"]])
    return pd.DataFrame({(ticker, "Close"): closes}, index=idx, columns=cols)


def _flat(ticker: str, level: float, last_completed: float, today: float):
    """250 bars: a flat run at `level`, yesterday's close, then today's partial
    bar. SMA50 and SMA200 both come out at ~`level`."""
    closes = [level] * 248 + [last_completed, today]
    return _frame(ticker, closes)


def test_live_price_rescues_nbis_shape():
    """NBIS 2026-08-12: prev close 193.23 under SMA50, live 238 over both."""
    data = _flat("NBIS", 200.0, 193.23, 238.25)
    kept = _filter_sma_trend(
        ["NBIS"], data, TODAY, single=False,
        live_prices={"NBIS": 238.25},
    )
    assert kept == ["NBIS"]


def test_live_price_rescues_crwv_shape():
    """CRWV 2026-08-12: prev close 90.32 under both MAs, live 106 over both."""
    data = _flat("CRWV", 95.0, 90.32, 106.46)
    kept = _filter_sma_trend(
        ["CRWV"], data, TODAY, single=False,
        live_prices={"CRWV": 106.46},
    )
    assert kept == ["CRWV"]


def test_no_live_prices_falls_back_to_prev_close():
    """Omitting live_prices reproduces today's behavior byte-for-byte."""
    data = _flat("CRWV", 95.0, 90.32, 106.46)
    assert _filter_sma_trend(["CRWV"], data, TODAY, single=False) == []


def test_live_price_missing_for_ticker_falls_back():
    """A ticker absent from live_prices takes the prev-close path, not a crash."""
    data = _flat("CRWV", 95.0, 90.32, 106.46)
    kept = _filter_sma_trend(
        ["CRWV"], data, TODAY, single=False, live_prices={"OTHER": 500.0},
    )
    assert kept == []


def test_nonpositive_live_price_falls_back():
    """pre_price arrives as 0 when Futu reports N/A — must not be trusted."""
    data = _flat("NBIS", 200.0, 193.23, 238.25)
    kept = _filter_sma_trend(
        ["NBIS"], data, TODAY, single=False, live_prices={"NBIS": 0.0},
    )
    assert kept == []


def test_bypass_exempts_sma50_when_gap_large():
    """gap 12%, live above SMA200 but below SMA50 → kept."""
    # 200 bars at 100 then 50 bars at 300 → SMA200 ~150, SMA50 ~300.
    closes = [100.0] * 199 + [300.0] * 49 + [180.0, 190.0]
    data = _frame("XYZ", closes)
    kept = _filter_sma_trend(
        ["XYZ"], data, TODAY, single=False,
        live_prices={"XYZ": 190.0}, gaps={"XYZ": 12.0}, bypass_gap_pct=10.0,
    )
    assert kept == ["XYZ"]


def test_bypass_never_exempts_sma200():
    """gap 12% but live below SMA200 → still dropped."""
    closes = [100.0] * 199 + [300.0] * 49 + [90.0, 95.0]
    data = _frame("XYZ", closes)
    kept = _filter_sma_trend(
        ["XYZ"], data, TODAY, single=False,
        live_prices={"XYZ": 95.0}, gaps={"XYZ": 12.0}, bypass_gap_pct=10.0,
    )
    assert kept == []


def test_gap_below_bypass_threshold_still_gated():
    """gap 8% under a 10% threshold → SMA50 still enforced."""
    closes = [100.0] * 199 + [300.0] * 49 + [180.0, 190.0]
    data = _frame("XYZ", closes)
    kept = _filter_sma_trend(
        ["XYZ"], data, TODAY, single=False,
        live_prices={"XYZ": 190.0}, gaps={"XYZ": 8.0}, bypass_gap_pct=10.0,
    )
    assert kept == []


def test_bypass_disabled_when_threshold_zero_or_none():
    """bypass_gap_pct 0 / None both mean 'no exemption'."""
    closes = [100.0] * 199 + [300.0] * 49 + [180.0, 190.0]
    data = _frame("XYZ", closes)
    for pct in (0, None):
        kept = _filter_sma_trend(
            ["XYZ"], data, TODAY, single=False,
            live_prices={"XYZ": 190.0}, gaps={"XYZ": 12.0}, bypass_gap_pct=pct,
        )
        assert kept == [], f"bypass_gap_pct={pct!r} must not exempt SMA50"


def test_ticker_missing_from_gaps_takes_strict_path():
    closes = [100.0] * 199 + [300.0] * 49 + [180.0, 190.0]
    data = _frame("XYZ", closes)
    kept = _filter_sma_trend(
        ["XYZ"], data, TODAY, single=False,
        live_prices={"XYZ": 190.0}, gaps={}, bypass_gap_pct=10.0,
    )
    assert kept == []


def test_insufficient_history_for_sma50_still_dropped():
    """Under 50 completed bars → dropped, regardless of live price or gap."""
    data = _frame("NEW", [100.0] * 40 + [500.0])
    kept = _filter_sma_trend(
        ["NEW"], data, TODAY, single=False,
        live_prices={"NEW": 500.0}, gaps={"NEW": 50.0}, bypass_gap_pct=10.0,
    )
    assert kept == []


def test_short_history_gates_on_sma50_only():
    """SPCX 2026-10-21: 90 bars, no SMA200 — SMA50 alone decides."""
    data = _frame("SPCX", [100.0] * 90 + [110.0])
    kept = _filter_sma_trend(
        ["SPCX"], data, TODAY, single=False, live_prices={"SPCX": 110.0},
    )
    assert kept == ["SPCX"]


def test_short_history_below_sma50_dropped():
    data = _frame("SPCX", [100.0] * 90 + [95.0])
    kept = _filter_sma_trend(
        ["SPCX"], data, TODAY, single=False, live_prices={"SPCX": 95.0},
    )
    assert kept == []


def test_short_history_gap_bypass_does_not_waive_sma50():
    """Without SMA200, SMA50 is the only floor — a big gap must not waive it."""
    data = _frame("SPCX", [100.0] * 90 + [95.0])
    kept = _filter_sma_trend(
        ["SPCX"], data, TODAY, single=False,
        live_prices={"SPCX": 95.0}, gaps={"SPCX": 15.0}, bypass_gap_pct=10.0,
    )
    assert kept == []


def test_gapquote_namedtuple_fields():
    from futu_sync import GapQuote

    q = GapQuote(price=106.46, gap=17.9)
    assert q.price == 106.46
    assert q.gap == 17.9


class _FakeSnapshotCtx:
    """Stands in for futu.OpenQuoteContext. Returns one basicinfo frame and
    one snapshot frame, both built from plain dicts."""

    def __init__(self, basic_rows, snap_rows):
        self._basic = pd.DataFrame(basic_rows)
        self._snap = pd.DataFrame(snap_rows)
        self.closed = False

    def get_stock_basicinfo(self, **kwargs):
        return 0, self._basic

    def get_market_snapshot(self, codes):
        return 0, self._snap[self._snap["code"].isin(codes)]

    def close(self):
        self.closed = True


def _install_fake_futu(monkeypatch, basic_rows, snap_rows):
    """Patch out the futu import, the TCP probe, and OpenQuoteContext."""
    import sys
    import types

    import futu_sync

    ctx = _FakeSnapshotCtx(basic_rows, snap_rows)
    fake = types.SimpleNamespace(
        OpenQuoteContext=lambda host, port: ctx,
        RET_OK=0,
        Market=types.SimpleNamespace(US="US", HK="HK"),
        SecurityType=types.SimpleNamespace(STOCK="STOCK"),
    )
    monkeypatch.setitem(sys.modules, "futu", fake)
    monkeypatch.setattr(futu_sync, "_opend_reachable", lambda h, p, **kw: True)
    return ctx


US_BASIC = [
    {"code": "US.NBIS", "exchange_type": "US_NASDAQ", "delisting": False},
    {"code": "US.SMALL", "exchange_type": "US_NASDAQ", "delisting": False},
]


def test_us_discovery_premarket_returns_pre_price_and_gap(monkeypatch):
    from futu_sync import discover_morning_gap_candidates

    _install_fake_futu(monkeypatch, US_BASIC, [
        {"code": "US.NBIS", "total_market_val": 5e10, "last_price": 193.23,
         "prev_close_price": 193.23, "pre_price": 238.25,
         "pre_change_rate": 23.3, "pre_volume": 100000},
        {"code": "US.SMALL", "total_market_val": 1e6, "last_price": 5.0,
         "prev_close_price": 4.0, "pre_price": 5.0,
         "pre_change_rate": 25.0, "pre_volume": 100},
    ])
    out = discover_morning_gap_candidates(
        min_gap_pct=5.0, min_market_cap=3e8, min_price=20.0,
        pre_market=True, exchanges=["US_NASDAQ"],
    )
    assert list(out) == ["NBIS"]                 # SMALL fails cap + price
    assert out["NBIS"].price == pytest.approx(238.25)
    assert out["NBIS"].gap == pytest.approx(23.3)


def test_us_discovery_premarket_falls_back_to_last_price(monkeypatch):
    """pre_price arrives as the string 'N/A' outside the pre-auction window."""
    from futu_sync import discover_morning_gap_candidates

    _install_fake_futu(monkeypatch, US_BASIC[:1], [
        {"code": "US.NBIS", "total_market_val": 5e10, "last_price": 193.23,
         "prev_close_price": 193.23, "pre_price": "N/A",
         "pre_change_rate": 23.3, "pre_volume": 100000},
    ])
    out = discover_morning_gap_candidates(
        min_gap_pct=5.0, min_market_cap=3e8, min_price=20.0,
        pre_market=True, exchanges=["US_NASDAQ"],
    )
    assert out["NBIS"].price == pytest.approx(193.23)


def test_us_discovery_postopen_uses_last_price_and_derived_gap(monkeypatch):
    from futu_sync import discover_morning_gap_candidates

    _install_fake_futu(monkeypatch, US_BASIC[:1], [
        {"code": "US.NBIS", "total_market_val": 5e10, "last_price": 238.25,
         "prev_close_price": 193.23, "pre_price": "N/A",
         "pre_change_rate": "N/A", "pre_volume": 0},
    ])
    out = discover_morning_gap_candidates(
        min_gap_pct=5.0, min_market_cap=3e8, min_price=20.0,
        pre_market=False, exchanges=["US_NASDAQ"],
    )
    assert out["NBIS"].price == pytest.approx(238.25)
    assert out["NBIS"].gap == pytest.approx(23.30, abs=0.01)


def test_hk_discovery_returns_quotes(monkeypatch):
    from futu_sync import discover_hk_morning_gap_candidates

    _install_fake_futu(
        monkeypatch,
        [{"code": "HK.00700", "exchange_type": "HK_MAINBOARD", "delisting": False}],
        [{"code": "HK.00700", "total_market_val": 5e11, "last_price": 660.0,
          "prev_close_price": 600.0}],
    )
    out = discover_hk_morning_gap_candidates(
        min_gap_pct=5.0, min_market_cap=3e8, min_price=20.0,
        exchanges=["HK_MAINBOARD"],
    )
    assert list(out) == ["0700.HK"]
    assert out["0700.HK"].price == pytest.approx(660.0)
    assert out["0700.HK"].gap == pytest.approx(10.0)


def test_discovery_returns_none_when_opend_unreachable(monkeypatch):
    import futu_sync
    from futu_sync import discover_morning_gap_candidates

    monkeypatch.setattr(futu_sync, "_opend_reachable", lambda h, p, **kw: False)
    out = discover_morning_gap_candidates(
        min_gap_pct=5.0, min_market_cap=3e8, min_price=20.0,
        pre_market=True, exchanges=["US_NASDAQ"],
    )
    assert out is None


def test_config_exposes_live_price_and_bypass_knobs():
    """Both morning-gap sections must ship the new knobs with the agreed
    defaults, so a revert is a config edit rather than a code change."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cfg = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    for section in ("morning_gap", "hk_morning_gap"):
        s = cfg[section]
        assert s["sma_use_live_price"] is True, section
        assert s["sma_bypass_gap_percent"] == 10.0, section
