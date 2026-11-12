"""Fixes from the 2026-11-12 whole-code review: writes that survive a kill,
a network gate that only probes what the mode uses, and a morning-gap seen
set that is recorded only after the push went out."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import etf_rs_leaders
import main
from notify import notify_morning_gap

_CFG = {"notify": {"enabled": True, "ntfy_topic": "t", "max_tickers_in_body": 100}}


# --- atomic master writes ---------------------------------------------------


def test_persist_seen_keeps_the_old_master_when_the_write_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = tmp_path / "state" / "eod_seen_US.txt"
    master.parent.mkdir()
    master.write_text("AAA\nBBB\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("killed mid-write")

    monkeypatch.setattr(main.os, "replace", boom)
    main._persist_seen(master, {"AAA", "BBB", "CCC"})  # soft: logs, no raise

    assert master.read_text(encoding="utf-8") == "AAA\nBBB\n"
    assert [p.name for p in master.parent.iterdir()] == ["eod_seen_US.txt"]


def test_persist_seen_writes_sorted_lines(tmp_path: Path) -> None:
    master = tmp_path / "state" / "eod_seen_US.txt"
    main._persist_seen(master, {"BBB", "AAA"})
    assert master.read_text(encoding="utf-8") == "AAA\nBBB\n"
    assert [p.name for p in master.parent.iterdir()] == ["eod_seen_US.txt"]


def test_leaders_master_write_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = tmp_path / "ETF_rs_leaders.txt"
    master.write_text("NVDA,AVGO\n", encoding="utf-8")
    etf_rs_leaders._write_csv(master, ["NVDA", "AVGO", "TSM"])
    assert master.read_text(encoding="utf-8") == "NVDA,AVGO,TSM\n"

    monkeypatch.setattr(
        etf_rs_leaders.os, "replace",
        lambda s, d: (_ for _ in ()).throw(OSError("killed")),
    )
    with pytest.raises(OSError):
        etf_rs_leaders._write_csv(master, [])
    assert master.read_text(encoding="utf-8") == "NVDA,AVGO,TSM\n"


# --- network gate -----------------------------------------------------------


@pytest.mark.parametrize("mode", ["morning-gap", "hk-morning-gap"])
def test_morning_gap_gate_probes_yahoo_only(mode: str) -> None:
    assert main._network_gate_hosts(mode) == ["query1.finance.yahoo.com"]


def test_hk_eod_gate_does_not_probe_finviz() -> None:
    hosts = main._network_gate_hosts("hk-eod")
    assert "finviz.com" not in hosts
    assert "raw.githubusercontent.com" in hosts


@pytest.mark.parametrize("mode", ["us-eod", "eod"])
def test_us_eod_gate_still_probes_finviz(mode: str) -> None:
    assert "finviz.com" in main._network_gate_hosts(mode)


# --- morning-gap: seen only after the push ----------------------------------


def test_classify_without_persist_leaves_the_seen_file_alone(tmp_path: Path) -> None:
    fresh, promoted = main._morning_gap_classify(
        "2026_11_12", ["AAA", "BBB"], tmp_path, is_pre=True, persist=False
    )
    assert (fresh, promoted) == (["AAA", "BBB"], [])
    # A scan whose push failed: the next scan must see the same names as fresh.
    again, _ = main._morning_gap_classify(
        "2026_11_12", ["AAA", "BBB"], tmp_path, is_pre=True, persist=False
    )
    assert again == ["AAA", "BBB"]


def test_mark_seen_makes_the_next_scan_not_fresh(tmp_path: Path) -> None:
    main._morning_gap_mark_seen("2026_11_12", ["AAA"], tmp_path, is_pre=True)
    fresh, _ = main._morning_gap_classify(
        "2026_11_12", ["AAA", "BBB"], tmp_path, is_pre=True, persist=False
    )
    assert fresh == ["BBB"]
    # Post-open: a pre-market name confirmed after the open is "promoted".
    main._morning_gap_mark_seen("2026_11_12", ["CCC"], tmp_path, is_pre=False)
    fresh, promoted = main._morning_gap_classify(
        "2026_11_12", ["AAA", "CCC", "DDD"], tmp_path, is_pre=False, persist=False
    )
    assert (fresh, promoted) == (["DDD"], ["AAA"])


def test_notify_reports_a_failed_post() -> None:
    with patch("notify._ntfy_post", return_value=False):
        assert notify_morning_gap(["AAA"], -5, 1, _CFG) is False
    with patch("notify._ntfy_post", return_value=True):
        assert notify_morning_gap(["AAA"], -5, 1, _CFG, promoted=["BBB"]) is True


def test_notify_counts_nothing_to_send_as_not_lost() -> None:
    with patch("notify._ntfy_post") as post:
        assert notify_morning_gap([], -5, 0, _CFG) is True
        assert notify_morning_gap(["AAA"], -5, 1, {"notify": {"enabled": False}}) is True
    post.assert_not_called()
