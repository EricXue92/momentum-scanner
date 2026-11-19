"""Tests for the power alerts: low battery and a run frozen by system sleep.

Regression 1: 2026-10-17 the Mac ran the 10:00 us-eod on battery (12%) with
the lid closed. `sleep 1` on battery put it to sleep 3 minutes in; the
process then only advanced during 5-15s DarkWake maintenance windows (no
Wi-Fi/DNS yet) — yfinance came back empty, Futu reported 网络中断 and the
conditional RS screener died on NameResolutionError.

Regression 2: 2026-11-19 — a week of `pmset -g log` showed that battery
alone is harmless: with the lid open a scheduled wake is a full wake and
caffeinate holds the scan. Only battery + closed lid breaks it (22/22
scheduled wakes were 5-19s DarkWakes; hk-eod started inside one at 20:05,
its caffeinate assertion was ignored, and it resumed at 20:23 when the lid
opened). The old "running on battery: plug in" push fired on every battery
run, i.e. also on the healthy lid-open ones. Now: a push when the battery is
low (once per day, marker `state/on_battery_alerted_<date>.txt`) or when the
run was actually frozen by sleep (per run, no marker).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import main
from cleanup import cleanup_old_outputs
from notify import notify_on_battery, notify_scan_stalled
from power import PowerStatus, asleep_seconds, power_status, parse_pmset_batt

_CFG = {"notify": {"enabled": True, "ntfy_topic": "t"}}
_TODAY = "2026_10_17"

_BATT = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=35782755)\t12%; discharging; 1:02 remaining present: true\n"
)
_AC = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=35782755)\t63%; charging; 1:13 remaining present: true\n"
)


def _marker(output_dir: Path, today: str = _TODAY) -> Path:
    return output_dir / "state" / f"on_battery_alerted_{today}.txt"


# --- power.parse_pmset_batt / power_status ---


def test_parse_detects_battery_and_percent() -> None:
    assert parse_pmset_batt(_BATT) == PowerStatus(on_battery=True, percent=12)


def test_parse_detects_ac() -> None:
    assert parse_pmset_batt(_AC) == PowerStatus(on_battery=False, percent=63)


def test_parse_unknown_output_is_none() -> None:
    assert parse_pmset_batt("") is None
    assert parse_pmset_batt("garbage\n") is None


def test_parse_desktop_without_battery_line() -> None:
    assert parse_pmset_batt("Now drawing from 'AC Power'\n") == PowerStatus(
        on_battery=False, percent=None
    )


def test_power_status_uses_runner() -> None:
    assert power_status(runner=lambda: _BATT) == PowerStatus(True, 12)


def test_power_status_soft_fails() -> None:
    def boom():
        raise FileNotFoundError("pmset")

    assert power_status(runner=boom) is None


# --- power.asleep_seconds ---


def test_asleep_seconds_is_continuous_minus_awake_clock() -> None:
    clocks = {"continuous": 770_223.4, "awake": 361_170.8}
    got = asleep_seconds(
        continuous=lambda: clocks["continuous"], awake=lambda: clocks["awake"]
    )
    assert got == 770_223.4 - 361_170.8


def test_asleep_seconds_soft_fails_without_the_clocks() -> None:
    def boom() -> float:
        raise AttributeError("CLOCK_UPTIME_RAW")  # non-macOS

    assert asleep_seconds(continuous=boom, awake=lambda: 1.0) is None


# --- notify.notify_on_battery ---


def test_posts_high_priority_alert_naming_mode_and_percent(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_on_battery("us-eod", 12, _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1
    _server, _topic, title, body = mock_post.call_args.args
    assert "battery" in title.lower()
    assert "us-eod" in body
    assert "12%" in body
    assert mock_post.call_args.kwargs["priority"] == "high"
    assert _marker(tmp_path).exists()


def test_title_is_ascii_safe(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_on_battery("hk-eod", None, _CFG, tmp_path, _TODAY)
    mock_post.call_args.args[2].encode("ascii")


def test_unknown_percent_is_fine(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_on_battery("hk-eod", None, _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1


def test_second_call_same_day_is_silent(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_on_battery("us-eod", 12, _CFG, tmp_path, _TODAY)
        notify_on_battery("morning-gap", 11, _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1


def test_failed_post_does_not_consume_the_daily_alert(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=False):
        notify_on_battery("us-eod", 12, _CFG, tmp_path, _TODAY)
    assert not _marker(tmp_path).exists()
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_on_battery("us-eod", 12, _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1


def test_disabled_config_is_noop(tmp_path: Path) -> None:
    cfg = {"notify": {"enabled": False, "ntfy_topic": "t"}}
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_on_battery("us-eod", 12, cfg, tmp_path, _TODAY)
    assert not mock_post.called


def test_unwritable_state_dir_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "state"
    blocker.write_text("i am a file, not a directory")
    with patch("notify._ntfy_post", return_value=True):
        notify_on_battery("us-eod", 12, _CFG, tmp_path, _TODAY)


# --- notify.notify_scan_stalled ---


def test_stalled_push_names_mode_minutes_and_power(tmp_path: Path) -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_scan_stalled("hk-eod", 17, PowerStatus(True, 100), _CFG)
    assert mock_post.call_count == 1
    _server, _topic, title, body = mock_post.call_args.args
    title.encode("ascii")
    assert "17 min" in title
    assert "hk-eod" in body
    assert "battery (100%)" in body
    assert "lid" in body


def test_stalled_push_reports_ac_when_plugged_in_since() -> None:
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_scan_stalled("us-eod", 40, PowerStatus(False, 98), _CFG)
    assert "AC" in mock_post.call_args.args[3]


def test_stalled_push_disabled_config_is_noop() -> None:
    cfg = {"notify": {"enabled": False, "ntfy_topic": "t"}}
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        notify_scan_stalled("us-eod", 40, PowerStatus(True, 50), cfg)
    assert not mock_post.called


# --- main._alert_if_on_battery ---


def _power(monkeypatch, status: PowerStatus | None, slept: float = 0.0) -> None:
    monkeypatch.setattr(main, "power_status", lambda: status)
    monkeypatch.setattr(main, "_slept_since_start", lambda: slept)


def test_healthy_battery_run_is_silent(tmp_path: Path, monkeypatch) -> None:
    """The 2026-11-12 push: hk-morning-gap at 80%, lid open, scan on time."""
    _power(monkeypatch, PowerStatus(True, 80))
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("hk-morning-gap", _CFG, tmp_path, _TODAY)
    assert not mock_post.called
    assert not _marker(tmp_path).exists()


def test_low_battery_sends_alert(tmp_path: Path, monkeypatch) -> None:
    _power(monkeypatch, PowerStatus(True, 12))
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1
    assert "12%" in mock_post.call_args.args[3]
    assert _marker(tmp_path).exists()


def test_battery_at_the_threshold_is_silent(tmp_path: Path, monkeypatch) -> None:
    _power(monkeypatch, PowerStatus(True, 20))
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert not mock_post.called


def test_unknown_percent_on_battery_is_silent(tmp_path: Path, monkeypatch) -> None:
    _power(monkeypatch, PowerStatus(True, None))
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert not mock_post.called


def test_low_percent_on_ac_is_silent(tmp_path: Path, monkeypatch) -> None:
    _power(monkeypatch, PowerStatus(False, 5))
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert not mock_post.called


def test_run_frozen_by_sleep_sends_stalled_alert(tmp_path: Path, monkeypatch) -> None:
    """2026-11-19 hk-eod: launched in a 20:05 DarkWake, resumed 20:23."""
    _power(monkeypatch, PowerStatus(True, 100), slept=1043.0)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("hk-eod", _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1
    _server, _topic, title, body = mock_post.call_args.args
    assert "17 min" in title
    assert "hk-eod" in body
    assert not _marker(tmp_path).exists()  # per run, not once per day


def test_stalled_alert_fires_even_when_plugged_in_since(
    tmp_path: Path, monkeypatch
) -> None:
    _power(monkeypatch, PowerStatus(False, 98), slept=3600.0)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1
    assert "60 min" in mock_post.call_args.args[2]


def test_stalled_alert_ignores_the_low_battery_marker(
    tmp_path: Path, monkeypatch
) -> None:
    _marker(tmp_path).parent.mkdir(parents=True)
    _marker(tmp_path).write_text("morning-gap\n")
    _power(monkeypatch, PowerStatus(True, 60), slept=600.0)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("hk-eod", _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1


def test_short_sleep_is_not_a_stall(tmp_path: Path, monkeypatch) -> None:
    _power(monkeypatch, PowerStatus(True, 80), slept=179.0)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert not mock_post.called


def test_stalled_and_low_battery_push_both(tmp_path: Path, monkeypatch) -> None:
    _power(monkeypatch, PowerStatus(True, 12), slept=600.0)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 2


def test_stalled_alert_survives_an_unknown_power_source(
    tmp_path: Path, monkeypatch
) -> None:
    _power(monkeypatch, None, slept=600.0)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1


def test_unknown_power_without_stall_is_silent(tmp_path: Path, monkeypatch) -> None:
    _power(monkeypatch, None)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert not mock_post.called


def test_probe_error_never_raises(tmp_path: Path, monkeypatch) -> None:
    def boom():
        raise RuntimeError("pmset exploded")

    monkeypatch.setattr(main, "power_status", boom)
    main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)


def test_slept_since_start_is_the_asleep_clock_delta(monkeypatch) -> None:
    monkeypatch.setattr(main, "_ASLEEP_AT_START", 100.0)
    monkeypatch.setattr(main, "asleep_seconds", lambda: 1143.0)
    assert main._slept_since_start() == 1043.0


def test_slept_since_start_is_zero_without_the_clock(monkeypatch) -> None:
    monkeypatch.setattr(main, "_ASLEEP_AT_START", None)
    monkeypatch.setattr(main, "asleep_seconds", lambda: 1143.0)
    assert main._slept_since_start() == 0.0
    monkeypatch.setattr(main, "_ASLEEP_AT_START", 100.0)
    monkeypatch.setattr(main, "asleep_seconds", lambda: None)
    assert main._slept_since_start() == 0.0


# --- cleanup: marker is aged out like opend_down_alerted ---


def test_cleanup_ages_out_marker(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    old = state / "on_battery_alerted_2026_10_10.txt"
    fresh = state / "on_battery_alerted_2026_10_17.txt"
    old.write_text("us-eod\n")
    fresh.write_text("us-eod\n")
    cleanup_old_outputs(tmp_path, date(2026, 10, 17))
    assert not old.exists()
    assert fresh.exists()
