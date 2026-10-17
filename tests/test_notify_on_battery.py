"""Tests for the on-battery ntfy warning.

Regression: 2026-10-17 the Mac ran the 10:00 us-eod on battery (12%) with the
lid closed. `sleep 1` on battery put it to sleep 3 minutes in; the process
then only advanced during 5-15s DarkWake maintenance windows (no Wi-Fi/DNS
yet) — yfinance came back empty, Futu reported 网络中断 and the conditional
RS screener died on NameResolutionError. The caffeinate wrapper now holds an
idle-sleep assertion, but a scheduled run on battery is still a degraded
run, so the first run of the day on battery pushes a warning (once per day,
marker `state/on_battery_alerted_<date>.txt`).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import main
from cleanup import cleanup_old_outputs
from notify import notify_on_battery
from power import PowerStatus, power_status, parse_pmset_batt

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


# --- main._alert_if_on_battery ---


def test_on_battery_sends_alert(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main, "power_status", lambda: PowerStatus(True, 12))
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert mock_post.call_count == 1
    assert "12%" in mock_post.call_args.args[3]


def test_on_ac_is_silent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main, "power_status", lambda: PowerStatus(False, 63))
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert not mock_post.called


def test_unknown_power_is_silent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main, "power_status", lambda: None)
    with patch("notify._ntfy_post", return_value=True) as mock_post:
        main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)
    assert not mock_post.called


def test_probe_error_never_raises(tmp_path: Path, monkeypatch) -> None:
    def boom():
        raise RuntimeError("pmset exploded")

    monkeypatch.setattr(main, "power_status", boom)
    main._alert_if_on_battery("us-eod", _CFG, tmp_path, _TODAY)


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
