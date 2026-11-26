"""`filters_any` on a `[[longs]]` group: one Finviz screen per alternative
code, results unioned.

Why it exists (→ decisions 2026-11-26): `earningsdate_today` only matches a
company that reports *today*. One that reported yesterday after the close —
whose gap is today — sits under `earningsdate_yesterdayafter`, and Finviz
silently ignores an `a|b` OR inside one filter, so the two need two screens.
"""
import tomllib
from pathlib import Path

import pytest

import main

CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


@pytest.fixture
def screens(monkeypatch):
    """Fake `main.run_screener`: records calls, answers from `results`
    (keyed by the last filter code; an Exception value is raised)."""
    calls: list[dict] = []
    results: dict[str, object] = {}

    def fake(filters, signal=None, capture_caps=None):
        calls.append({"filters": list(filters), "signal": signal, "caps": capture_caps})
        out = results.get(filters[-1], [])
        if isinstance(out, Exception):
            raise out
        return list(out)

    monkeypatch.setattr(main, "run_screener", fake)
    return calls, results


def test_group_without_filters_any_runs_its_one_screen(screens):
    calls, results = screens
    results["f2"] = ["AAA", "BBB"]
    caps: dict[str, float] = {}

    out = main._longs_group_screen({"filters": ["f1", "f2"], "signal": "sig"}, caps)

    assert out == ["AAA", "BBB"]
    assert calls == [{"filters": ["f1", "f2"], "signal": "sig", "caps": caps}]


def test_filters_any_runs_one_screen_per_alternative(screens):
    calls, results = screens
    caps: dict[str, float] = {}

    main._longs_group_screen(
        {"filters": ["f1"], "signal": "sig", "filters_any": ["a", "b"]}, caps
    )

    assert [c["filters"] for c in calls] == [["f1", "a"], ["f1", "b"]]
    assert all(c["signal"] == "sig" and c["caps"] is caps for c in calls)


def test_filters_any_unions_in_order_without_duplicates(screens):
    _, results = screens
    results["a"] = ["DE", "LI"]
    results["b"] = ["DELL", "DE", "HPQ"]

    out = main._longs_group_screen({"filters": ["f1"], "filters_any": ["a", "b"]}, None)

    assert out == ["DE", "LI", "DELL", "HPQ"]


def test_filters_any_does_not_mutate_the_group_config(screens):
    cfg = {"filters": ["f1"], "filters_any": ["a", "b"]}

    main._longs_group_screen(cfg, None)

    assert cfg == {"filters": ["f1"], "filters_any": ["a", "b"]}


def test_one_failed_alternative_keeps_the_other_screens_tickers(screens):
    """Adding the second screen must not cost the first one's result."""
    _, results = screens
    results["a"] = ["DE"]
    results["b"] = ConnectionError("finviz reset")

    out = main._longs_group_screen({"filters": ["f1"], "filters_any": ["a", "b"]}, None)

    assert out == ["DE"]


def test_all_alternatives_failing_raises(screens):
    """The Longs loop's own handler then logs the group as failed."""
    _, results = screens
    results["a"] = ConnectionError("first")
    results["b"] = ConnectionError("second")

    with pytest.raises(ConnectionError, match="second"):
        main._longs_group_screen({"filters": ["f1"], "filters_any": ["a", "b"]}, None)


def test_config_earnings_gap_covers_yesterdays_after_close_reporters():
    cfg = tomllib.loads(CONFIG.read_text("utf-8"))
    eg = {g["key"]: g for g in cfg["longs"]}["earnings_gap"]

    assert eg["filters_any"] == ["earningsdate_today", "earningsdate_yesterdayafter"]
    # The date code lives in filters_any only — a copy left in `filters`
    # would AND with the alternative and empty the after-close screen.
    assert not [f for f in eg["filters"] if f.startswith("earningsdate_")]
    # The rest of the group is unchanged.
    assert {"cap_smallover", "sh_avgvol_o500", "sh_price_o20", "sh_relvol_o1.5",
            "ta_gap_u5", "ta_sma50_pa", "ta_sma200_pa"} <= set(eg["filters"])
