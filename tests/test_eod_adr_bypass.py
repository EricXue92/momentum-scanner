"""Big-gap ADR% bypass for US EOD Longs (the_setup / earnings_gap): a daily
gap >= the group's adr_bypass_gap_percent relaxes the ADR% floor to
adr_bypass_min_percent.

Fixtures mirror CRM 2026-08-27: earnings gap +11.9% (close +22.6%, RVol 4.7x)
but 20d ADR% 3.74 < 4.0, so every Longs group dropped it at EOD — the
morning-gap bypass (2026-09-03) never covered the EOD path.
"""

import pandas as pd

import main
from main import _daily_gaps, filter_dollar_volume_and_adr_yf

LAST_BAR = "2026-08-27"
TODAY = pd.Timestamp("2026-08-28").date()
FIELDS = ["Open", "High", "Low", "Close", "Volume"]


def _bars(adr_pct: float, gap_pct: float, bars: int = 40) -> pd.DataFrame:
    """Flat 100.0 closes with a constant `adr_pct`% daily range; the last bar
    opens `gap_pct`% above the prior close."""
    idx = pd.date_range(end=LAST_BAR, periods=bars, freq="B")
    df = pd.DataFrame(
        {
            "Open": 100.0,
            "High": 100.0 * (1 + adr_pct / 100),
            "Low": 100.0,
            "Close": 100.0,
            "Volume": 10_000_000.0,
        },
        index=idx,
    )
    df.iloc[-1, df.columns.get_loc("Open")] = 100.0 * (1 + gap_pct / 100)
    return df[FIELDS]


def _batch(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """yfinance `group_by="ticker"` batch shape."""
    return pd.concat(frames, axis=1)


def _patch_download(monkeypatch, data: pd.DataFrame) -> None:
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: data.copy())


def test_daily_gaps_uses_last_open_vs_prior_close():
    data = _batch({"CRM": _bars(3.74, 11.88), "FLAT": _bars(3.74, 0.0)})
    gaps = _daily_gaps(["CRM", "FLAT"], data, False, TODAY, single=False)
    assert round(gaps["CRM"], 2) == 11.88
    assert round(gaps["FLAT"], 2) == 0.0


def test_daily_gaps_trims_todays_partial_bar():
    """Intraday rerun: today's bar is dropped, so the gap is the previous
    completed bar's — the same basis ADR% uses."""
    data = _batch({"CRM": _bars(3.74, 11.88)})
    today = pd.Timestamp(LAST_BAR).date()
    gaps = _daily_gaps(["CRM"], data, True, today, single=False)
    assert round(gaps["CRM"], 2) == 0.0


def test_daily_gaps_single_ticker_flat_frame():
    gaps = _daily_gaps(["CRM"], _bars(3.74, 11.88), False, TODAY, single=True)
    assert round(gaps["CRM"], 2) == 11.88


def test_daily_gaps_omits_ticker_without_data():
    data = _batch({"CRM": _bars(3.74, 11.88)})
    assert _daily_gaps(["NOPE"], data, False, TODAY, single=False) == {}


def test_eod_big_gap_relaxes_adr_floor_crm_shape(monkeypatch):
    """CRM shape: ADR% 3.74 < 4.0, gap 11.88% >= 10% → kept at the 3.0 floor."""
    _patch_download(
        monkeypatch, _batch({"CRM": _bars(3.74, 11.88), "HOOD": _bars(5.2, 0.0)})
    )
    kept = filter_dollar_volume_and_adr_yf(
        ["CRM", "HOOD"], 100_000_000, 4.0, 20,
        adr_bypass_gap_pct=10.0, adr_bypass_min_pct=3.0,
    )
    assert kept == ["CRM", "HOOD"]


def test_eod_small_gap_keeps_base_floor(monkeypatch):
    _patch_download(
        monkeypatch, _batch({"CRM": _bars(3.74, 6.0), "HOOD": _bars(5.2, 0.0)})
    )
    kept = filter_dollar_volume_and_adr_yf(
        ["CRM", "HOOD"], 100_000_000, 4.0, 20,
        adr_bypass_gap_pct=10.0, adr_bypass_min_pct=3.0,
    )
    assert kept == ["HOOD"]


def test_eod_bypass_floor_still_enforced(monkeypatch):
    """gap 12% but ADR% 2.5 < relaxed 3.0 floor → still dropped."""
    _patch_download(
        monkeypatch, _batch({"SLOW": _bars(2.5, 12.0), "HOOD": _bars(5.2, 0.0)})
    )
    kept = filter_dollar_volume_and_adr_yf(
        ["SLOW", "HOOD"], 100_000_000, 4.0, 20,
        adr_bypass_gap_pct=10.0, adr_bypass_min_pct=3.0,
    )
    assert kept == ["HOOD"]


def test_eod_without_bypass_args_is_unchanged(monkeypatch):
    """Leaders / Shorts / RS and the other Longs groups pass no bypass args."""
    _patch_download(
        monkeypatch, _batch({"CRM": _bars(3.74, 11.88), "HOOD": _bars(5.2, 0.0)})
    )
    kept = filter_dollar_volume_and_adr_yf(["CRM", "HOOD"], 100_000_000, 4.0, 20)
    assert kept == ["HOOD"]


def test_config_enables_bypass_only_for_setup_and_earnings_gap():
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cfg = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    by_key = {g["key"]: g for g in cfg["longs"]}
    for key in ("the_setup", "earnings_gap"):
        assert by_key[key]["adr_bypass_gap_percent"] == 10.0
        assert by_key[key]["adr_bypass_min_percent"] == 3.0
    for key in ("high_volume", "gap_up", "new_high_52w", "top_gainers"):
        assert "adr_bypass_gap_percent" not in by_key[key]
