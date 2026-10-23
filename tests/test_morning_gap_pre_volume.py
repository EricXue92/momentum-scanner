"""Pre-market volume gate: pre_volume must be >= ratio x 20d average volume.

Fixtures mirror 2026-09-11: NVT (20d avg ~2.0M) and WIX (20d avg ~0.8M)
both cleared the 5% pre_change_rate gate on a handful of thin pre-market
prints (pre_price above the whole day's range) and opened +1.7% / -0.1%.
"""

import pandas as pd

from futu_sync import GapQuote
from main import _filter_pre_market_volume

TODAY = pd.Timestamp("2026-09-11").date()


def _frame(ticker: str, volumes: list[float]) -> pd.DataFrame:
    """yfinance batch shape: MultiIndex columns (ticker, field). Last row is
    dated TODAY so `_trim_today` drops it."""
    idx = pd.date_range(end="2026-09-11", periods=len(volumes), freq="B")
    cols = pd.MultiIndex.from_product([[ticker], ["Volume"]])
    return pd.DataFrame({(ticker, "Volume"): volumes}, index=idx, columns=cols)


def _flat(ticker: str, avg: float, n: int = 30) -> pd.DataFrame:
    return _frame(ticker, [avg] * (n - 1) + [0.0])


def test_thin_pre_market_print_dropped():
    """NVT: 2.0M avg, 12k pre-market shares = 0.6% < 5% -> dropped."""
    data = _flat("NVT", 2_000_000)
    quotes = {"NVT": GapQuote(price=164.75, gap=6.2, pre_volume=12_000)}
    kept = _filter_pre_market_volume(
        ["NVT"], data, quotes, 0.05, 20, TODAY, single=False
    )
    assert kept == []


def test_real_pre_market_volume_kept():
    """A genuine gapper: 2.0M avg, 300k pre-market shares = 15% -> kept."""
    data = _flat("NVT", 2_000_000)
    quotes = {"NVT": GapQuote(price=164.75, gap=6.2, pre_volume=300_000)}
    kept = _filter_pre_market_volume(
        ["NVT"], data, quotes, 0.05, 20, TODAY, single=False
    )
    assert kept == ["NVT"]


def test_exact_ratio_boundary_kept():
    data = _flat("WIX", 800_000)
    quotes = {"WIX": GapQuote(price=79.68, gap=5.5, pre_volume=40_000)}
    kept = _filter_pre_market_volume(
        ["WIX"], data, quotes, 0.05, 20, TODAY, single=False
    )
    assert kept == ["WIX"]


def test_ratio_zero_disables_gate():
    data = _flat("WIX", 800_000)
    quotes = {"WIX": GapQuote(price=79.68, gap=5.5, pre_volume=1)}
    kept = _filter_pre_market_volume(
        ["WIX"], data, quotes, 0.0, 20, TODAY, single=False
    )
    assert kept == ["WIX"]


def test_missing_pre_volume_is_kept():
    """Legacy GapQuote (no pre_volume) must not be dropped by the new gate."""
    data = _flat("WIX", 800_000)
    quotes = {"WIX": GapQuote(price=79.68, gap=5.5)}
    kept = _filter_pre_market_volume(
        ["WIX"], data, quotes, 0.05, 20, TODAY, single=False
    )
    assert kept == ["WIX"]


def test_single_ticker_frame_shape():
    """yfinance returns flat columns for a single ticker download."""
    idx = pd.date_range(end="2026-09-11", periods=30, freq="B")
    data = pd.DataFrame({"Volume": [1_000_000.0] * 29 + [0.0]}, index=idx)
    quotes = {"NVT": GapQuote(price=1.0, gap=6.0, pre_volume=10_000)}
    assert _filter_pre_market_volume(
        ["NVT"], data, quotes, 0.05, 20, TODAY, single=True
    ) == []


def test_gapquote_positional_construction_still_works():
    q = GapQuote(price=1.0, gap=2.0)
    assert q.pre_volume is None


# ── Big-gap bypass (2026-10-23) ──────────────────────────────────────────
# RNG gapped +12% pre-market on earnings with 33k pre-market shares against a
# 1.49M 20d average (2.2%) and was dropped by the 5% ratio at all three
# pre-market scans. A gap >= `bypass_gap_pct` relaxes the ratio to
# `bypass_ratio`; a thin tape (ELAN: 204 shares) still fails even at 2%.

RNG_AVG = 1_486_895


def test_big_gap_relaxes_ratio_to_bypass_ratio():
    """RNG: +12.3%, 33,122 shares = 2.2% -> kept at the 2% bypass floor."""
    data = _flat("RNG", RNG_AVG)
    quotes = {"RNG": GapQuote(price=32.0, gap=12.3, pre_volume=33_122)}
    kept = _filter_pre_market_volume(
        ["RNG"], data, quotes, 0.05, 20, TODAY, single=False,
        bypass_gap_pct=10.0, bypass_ratio=0.02,
    )
    assert kept == ["RNG"]


def test_big_gap_thin_tape_still_dropped():
    """ELAN: 204 shares on a 5.15M avg is noise whatever the gap says."""
    data = _flat("ELAN", 5_151_315)
    quotes = {"ELAN": GapQuote(price=25.0, gap=14.0, pre_volume=204)}
    kept = _filter_pre_market_volume(
        ["ELAN"], data, quotes, 0.05, 20, TODAY, single=False,
        bypass_gap_pct=10.0, bypass_ratio=0.02,
    )
    assert kept == []


def test_gap_below_bypass_keeps_base_ratio():
    """NVT-style +6.2% on 2.2% of avg: below the bypass gap -> base 5% applies."""
    data = _flat("NVT", RNG_AVG)
    quotes = {"NVT": GapQuote(price=164.75, gap=6.2, pre_volume=33_122)}
    kept = _filter_pre_market_volume(
        ["NVT"], data, quotes, 0.05, 20, TODAY, single=False,
        bypass_gap_pct=10.0, bypass_ratio=0.02,
    )
    assert kept == []


def test_bypass_off_when_gap_pct_zero():
    """bypass_gap_pct=0 (config default) = no bypass: RNG dropped as before."""
    data = _flat("RNG", RNG_AVG)
    quotes = {"RNG": GapQuote(price=32.0, gap=12.3, pre_volume=33_122)}
    kept = _filter_pre_market_volume(
        ["RNG"], data, quotes, 0.05, 20, TODAY, single=False,
        bypass_gap_pct=0, bypass_ratio=0.02,
    )
    assert kept == []


def test_bypass_ratio_zero_waives_gate_for_big_gap():
    """bypass_ratio=0 with a big gap: like the ADR knob, 0 = fully waived."""
    data = _flat("RNG", RNG_AVG)
    quotes = {"RNG": GapQuote(price=32.0, gap=12.3, pre_volume=1)}
    kept = _filter_pre_market_volume(
        ["RNG"], data, quotes, 0.05, 20, TODAY, single=False,
        bypass_gap_pct=10.0, bypass_ratio=0,
    )
    assert kept == ["RNG"]
