"""Morning-gap window pre-check runs BEFORE the network gate.

The morning-gap plists fire many slots and most land outside the window
(both DST variants, weekends — 2026-10-17 Sat 13:40 fired too). The window
used to be checked only after `wait_for_network`, so every out-of-window
fire spent the 20s settle, and with the network down waited the full 300s
and pushed a false "scan skipped" alert.
"""

import main


def test_us_out_of_window_short_circuits(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_get_et_scan_offset", lambda o, t: seen.append((o, t)))
    cfg = {"morning_gap": {"scan_offsets": [-20, -10], "offset_tolerance_minutes": 3}}
    assert main._morning_gap_in_window("morning-gap", cfg) is False
    assert seen == [([-20, -10], 3)]


def test_us_in_window(monkeypatch):
    monkeypatch.setattr(main, "_get_et_scan_offset", lambda o, t: -20)
    assert main._morning_gap_in_window("morning-gap", {"morning_gap": {}}) is True


def test_hk_uses_hkt_clock_and_hk_section(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_get_hkt_scan_offset", lambda o, t: seen.append((o, t)) or 10)
    monkeypatch.setattr(main, "_get_et_scan_offset", lambda o, t: None)
    cfg = {"hk_morning_gap": {"scan_offsets": [10, 20]}}
    assert main._morning_gap_in_window("hk-morning-gap", cfg) is True
    assert seen == [([10, 20], 2)]


def test_offset_zero_counts_as_in_window(monkeypatch):
    monkeypatch.setattr(main, "_get_et_scan_offset", lambda o, t: 0)
    assert main._morning_gap_in_window("morning-gap", {"morning_gap": {}}) is True


def test_missing_section_defers_to_the_run_path(monkeypatch):
    # The run path logs "No [morning_gap] config section found" and exits 1;
    # the pre-check must not swallow that.
    monkeypatch.setattr(main, "_get_et_scan_offset", lambda o, t: None)
    assert main._morning_gap_in_window("morning-gap", {}) is True


def test_defaults_match_the_run_path():
    assert main._US_GAP_DEFAULT_OFFSETS == [-20, -10, 10, 15, 20, 25, 30]
    assert main._HK_GAP_DEFAULT_OFFSETS == [10, 20, 30, 40, 50, 60]
