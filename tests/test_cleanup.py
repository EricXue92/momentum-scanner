from datetime import date
from pathlib import Path

import pytest

from cleanup import cleanup_old_outputs


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")


@pytest.fixture
def output_tree(tmp_path: Path) -> Path:
    """Build a synthetic output/ tree spanning 2026-05-09..2026-05-15."""
    out = tmp_path / "output"
    for d in ("TV/US", "TV/HK", "Webull/US", "Webull/HK", "Reports",
              "Reports/PostMarket", "Reports/PreMarket", "state"):
        (out / d).mkdir(parents=True)
    return out


def test_recent_five_days_survive(output_tree: Path) -> None:
    # 5-day window: today (05_15) + 4 prior days (05_14..05_11) survive.
    for d in ("2026_05_15", "2026_05_14", "2026_05_13",
              "2026_05_12", "2026_05_11"):
        _touch(output_tree / f"TV/US/{d}_Leaders.txt")
    cleanup_old_outputs(output_tree, date(2026, 5, 15))
    for d in ("2026_05_15", "2026_05_14", "2026_05_13",
              "2026_05_12", "2026_05_11"):
        assert (output_tree / f"TV/US/{d}_Leaders.txt").exists()


def test_files_older_than_five_days_deleted(output_tree: Path) -> None:
    # Cutoff for today=05_15 is 05_11; 05_10 and earlier are pruned.
    _touch(output_tree / "TV/US/2026_05_15_Leaders.txt")
    _touch(output_tree / "TV/US/2026_05_11_Leaders.txt")
    _touch(output_tree / "TV/US/2026_05_10_Leaders.txt")
    _touch(output_tree / "TV/US/2026_05_09_Leaders.txt")

    _touch(output_tree / "TV/HK/2026_05_10_Shorts.txt")
    _touch(output_tree / "Webull/US/2026_05_09_GapUp.txt")
    _touch(output_tree / "Webull/HK/2026_05_09_RS.txt")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    assert (output_tree / "TV/US/2026_05_15_Leaders.txt").exists()
    assert (output_tree / "TV/US/2026_05_11_Leaders.txt").exists()
    assert not (output_tree / "TV/US/2026_05_10_Leaders.txt").exists()
    assert not (output_tree / "TV/US/2026_05_09_Leaders.txt").exists()
    assert not (output_tree / "TV/HK/2026_05_10_Shorts.txt").exists()
    assert not (output_tree / "Webull/US/2026_05_09_GapUp.txt").exists()
    assert not (output_tree / "Webull/HK/2026_05_09_RS.txt").exists()


def test_canslim_reports_use_seven_day_window(output_tree: Path) -> None:
    # CANSLIM reports keep 7 days (today + 6 prior). Today = 05_15, cutoff =
    # 05_09: 05_09..05_15 survive, 05_08 and earlier are pruned.
    for d in ("2026_05_15", "2026_05_10", "2026_05_09", "2026_05_08", "2026_05_01"):
        _touch(output_tree / f"Reports/{d}_us.md")
        _touch(output_tree / f"Reports/{d}_hk.html")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    for d in ("2026_05_15", "2026_05_10", "2026_05_09"):
        assert (output_tree / f"Reports/{d}_us.md").exists()
        assert (output_tree / f"Reports/{d}_hk.html").exists()
    for d in ("2026_05_08", "2026_05_01"):
        assert not (output_tree / f"Reports/{d}_us.md").exists()
        assert not (output_tree / f"Reports/{d}_hk.html").exists()


def test_premarket_catalyst_reports_use_seven_day_window(output_tree: Path) -> None:
    # <date>_us_premarket.{md,html} (morning-gap catalyst report) shares the
    # 7-day CANSLIM window. Today = 05_15, cutoff = 05_09.
    for d in ("2026_05_15", "2026_05_09", "2026_05_08", "2026_04_01"):
        _touch(output_tree / f"Reports/{d}_us_premarket.md")
        _touch(output_tree / f"Reports/{d}_us_premarket.html")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    for d in ("2026_05_15", "2026_05_09"):
        assert (output_tree / f"Reports/{d}_us_premarket.md").exists()
        assert (output_tree / f"Reports/{d}_us_premarket.html").exists()
    for d in ("2026_05_08", "2026_04_01"):
        assert not (output_tree / f"Reports/{d}_us_premarket.md").exists()
        assert not (output_tree / f"Reports/{d}_us_premarket.html").exists()


def test_rs_rating_uses_four_day_window(output_tree: Path) -> None:
    # rs_rating_*.csv survives for 4 days; today is 2026-05-15, cutoff =
    # 2026-05-12, so 05_12..05_15 survive, 05_11 and earlier go.
    for d in ("2026_05_15", "2026_05_14", "2026_05_13",
              "2026_05_12", "2026_05_11", "2026_05_09"):
        _touch(output_tree / f"state/rs_rating_{d}.csv")

    # hk_rs_rating_*.csv (12M) and hk_rs_rating_3m_*.csv (3M) are both on
    # the standard 2-day rule. Today = 15, cutoff = 14.
    for d in ("2026-05-15", "2026-05-14", "2026-05-13", "2026-05-12"):
        _touch(output_tree / f"state/hk_rs_rating_{d}.csv")
        _touch(output_tree / f"state/hk_rs_rating_3m_{d}.csv")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    assert (output_tree / "state/rs_rating_2026_05_15.csv").exists()
    assert (output_tree / "state/rs_rating_2026_05_14.csv").exists()
    assert (output_tree / "state/rs_rating_2026_05_13.csv").exists()
    assert (output_tree / "state/rs_rating_2026_05_12.csv").exists()
    assert not (output_tree / "state/rs_rating_2026_05_11.csv").exists()
    assert not (output_tree / "state/rs_rating_2026_05_09.csv").exists()

    assert (output_tree / "state/hk_rs_rating_2026-05-15.csv").exists()
    assert (output_tree / "state/hk_rs_rating_2026-05-14.csv").exists()
    assert not (output_tree / "state/hk_rs_rating_2026-05-13.csv").exists()
    assert not (output_tree / "state/hk_rs_rating_2026-05-12.csv").exists()

    assert (output_tree / "state/hk_rs_rating_3m_2026-05-15.csv").exists()
    assert (output_tree / "state/hk_rs_rating_3m_2026-05-14.csv").exists()
    assert not (output_tree / "state/hk_rs_rating_3m_2026-05-13.csv").exists()
    assert not (output_tree / "state/hk_rs_rating_3m_2026-05-12.csv").exists()


def test_survivors_are_preserved(output_tree: Path) -> None:
    # The state files we explicitly never want to delete.
    _touch(output_tree / "state/eod_seen_US.txt")
    _touch(output_tree / "state/eod_seen_HK.txt")
    _touch(output_tree / "state/eod_seen_IPO.txt")
    _touch(output_tree / "state/eod_seen_HKIPO.txt")
    _touch(output_tree / "state/ntfy_last_seen.txt")
    _touch(output_tree / "state/edgar_cache/AAPL.json")
    _touch(output_tree / "launchd_US.log")
    _touch(output_tree / "launchd_HK.log")

    # Non-dated rogue file in a watched directory.
    _touch(output_tree / "TV/US/notes.txt")
    # Reports cover preview (not dated).
    _touch(output_tree / "Reports/_cover_preview.html")

    # Mix in something old to ensure cleanup actually ran.
    _touch(output_tree / "TV/US/2026_05_01_Leaders.txt")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    assert (output_tree / "state/eod_seen_US.txt").exists()
    assert (output_tree / "state/eod_seen_HK.txt").exists()
    assert (output_tree / "state/eod_seen_IPO.txt").exists()
    assert (output_tree / "state/eod_seen_HKIPO.txt").exists()
    assert (output_tree / "state/ntfy_last_seen.txt").exists()
    assert (output_tree / "state/edgar_cache/AAPL.json").exists()
    assert (output_tree / "launchd_US.log").exists()
    assert (output_tree / "launchd_HK.log").exists()
    assert (output_tree / "TV/US/notes.txt").exists()
    assert (output_tree / "Reports/_cover_preview.html").exists()
    # ...and the old dated file was actually deleted.
    assert not (output_tree / "TV/US/2026_05_01_Leaders.txt").exists()


def test_morning_gap_state_files_cleaned(output_tree: Path) -> None:
    _touch(output_tree / "state/morning_gap_seen_pre_2026_05_15.txt")
    _touch(output_tree / "state/morning_gap_seen_post_2026_05_15.txt")
    _touch(output_tree / "state/morning_gap_seen_pre_2026_05_14.txt")
    _touch(output_tree / "state/morning_gap_seen_pre_2026_05_13.txt")
    _touch(output_tree / "state/morning_gap_seen_post_2026_05_12.txt")
    _touch(output_tree / "state/hk_morning_gap_seen_post_2026_05_15.txt")
    _touch(output_tree / "state/hk_morning_gap_seen_post_2026_05_13.txt")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    assert (output_tree / "state/morning_gap_seen_pre_2026_05_15.txt").exists()
    assert (output_tree / "state/morning_gap_seen_post_2026_05_15.txt").exists()
    assert (output_tree / "state/morning_gap_seen_pre_2026_05_14.txt").exists()
    assert not (output_tree / "state/morning_gap_seen_pre_2026_05_13.txt").exists()
    assert not (output_tree / "state/morning_gap_seen_post_2026_05_12.txt").exists()
    assert (output_tree / "state/hk_morning_gap_seen_post_2026_05_15.txt").exists()
    assert not (output_tree / "state/hk_morning_gap_seen_post_2026_05_13.txt").exists()


def test_empty_state_dir_does_not_crash(output_tree: Path) -> None:
    # state/ exists but contains nothing matching the rules.
    cleanup_old_outputs(output_tree, date(2026, 5, 15))


def test_missing_subdir_is_skipped(tmp_path: Path) -> None:
    # output/ exists but none of the expected subdirs do.
    out = tmp_path / "output"
    out.mkdir()
    cleanup_old_outputs(out, date(2026, 5, 15))


def test_malformed_date_in_filename_is_skipped(
    output_tree: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Feb 30 doesn't exist — file must NOT be deleted, and a warning logged.
    _touch(output_tree / "TV/US/2026_02_30_Leaders.txt")
    with caplog.at_level("WARNING"):
        cleanup_old_outputs(output_tree, date(2026, 5, 15))
    assert (output_tree / "TV/US/2026_02_30_Leaders.txt").exists()
    assert any("malformed date" in r.message for r in caplog.records)


def test_cleanup_keeps_recent_rs_rating_3m(tmp_path):
    """rs_rating_3m_*.csv follows the same 4-day window as rs_rating_*.csv.
    Note: 3M cache uses dash-separated date format (vs underscore for 12M)."""
    state = tmp_path / "output" / "state"
    state.mkdir(parents=True)
    keep = state / "rs_rating_3m_2026-05-20.csv"  # 1 day old
    drop = state / "rs_rating_3m_2026-05-10.csv"  # 11 days old
    keep.write_text("ticker,raw_score,rs_percentile\nAAA,0.2,95\n")
    drop.write_text("ticker,raw_score,rs_percentile\nAAA,0.2,95\n")

    cleanup_old_outputs(tmp_path / "output", date(2026, 5, 21))
    assert keep.exists()
    assert not drop.exists()


def test_cleanup_top_n_snapshots_four_day_window(tmp_path: Path) -> None:
    out = tmp_path / "output"
    tv_us = out / "TV" / "US"
    tv_us.mkdir(parents=True)
    # US snapshots live in TV/US; root-level ones are pre-move leftovers that
    # must still age out. HK stays at the output root.
    _touch(tv_us / "rs_us_2026-05-15.txt")
    _touch(tv_us / "rs_us_2026-05-12.txt")
    _touch(tv_us / "rs_us_2026-05-11.txt")
    _touch(out / "rs_us_2026-05-11.txt")
    _touch(out / "hk_rs_2026-05-15.txt")
    _touch(out / "hk_rs_2026-05-11.txt")
    cleanup_old_outputs(out, date(2026, 5, 15))
    assert (tv_us / "rs_us_2026-05-15.txt").exists()
    assert (tv_us / "rs_us_2026-05-12.txt").exists()
    assert not (tv_us / "rs_us_2026-05-11.txt").exists()
    assert not (out / "rs_us_2026-05-11.txt").exists()
    assert (out / "hk_rs_2026-05-15.txt").exists()
    assert not (out / "hk_rs_2026-05-11.txt").exists()


def test_cleanup_audit_reports_four_day_window(tmp_path: Path) -> None:
    out = tmp_path / "output"
    out.mkdir()
    _touch(out / "rs_line_audit_US_2026-05-15.txt")
    _touch(out / "rs_line_audit_US_2026-05-12.txt")          # 4th day — kept
    _touch(out / "rs_line_audit_US_2026-05-11.txt")          # 5th day — pruned
    _touch(out / "rs_line_audit_HK_2026-05-11_drop.txt")
    _touch(out / "rs_line_audit_HK_2026-05-11_keep_ranked.txt")
    cleanup_old_outputs(out, date(2026, 5, 15))
    assert (out / "rs_line_audit_US_2026-05-15.txt").exists()
    assert (out / "rs_line_audit_US_2026-05-12.txt").exists()
    assert not (out / "rs_line_audit_US_2026-05-11.txt").exists()
    assert not (out / "rs_line_audit_HK_2026-05-11_drop.txt").exists()
    assert not (out / "rs_line_audit_HK_2026-05-11_keep_ranked.txt").exists()


def test_cleanup_removes_old_hk_metrics_state_cache(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "hk_metrics_2026-05-26.csv").write_text("code,last_price\n")   # today
    (state / "hk_metrics_2026-05-25.csv").write_text("code,last_price\n")   # yesterday
    (state / "hk_metrics_2026-05-20.csv").write_text("code,last_price\n")   # 6 days old

    cleanup_old_outputs(tmp_path, date(2026, 5, 26))

    assert (state / "hk_metrics_2026-05-26.csv").exists()      # today kept
    assert (state / "hk_metrics_2026-05-25.csv").exists()      # yesterday kept (2-day window)
    assert not (state / "hk_metrics_2026-05-20.csv").exists()  # 6-day pruned


def test_postmarket_html_uses_seven_day_window(output_tree: Path) -> None:
    # Reports/PostMarket/<date>_{us,hk}.html — 7 days. Today = 05_15, cutoff = 05_09.
    for d in ("2026_05_15", "2026_05_09", "2026_05_08"):
        _touch(output_tree / f"Reports/PostMarket/{d}_us.html")
        _touch(output_tree / f"Reports/PostMarket/{d}_hk.html")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    for d in ("2026_05_15", "2026_05_09"):
        assert (output_tree / f"Reports/PostMarket/{d}_us.html").exists()
        assert (output_tree / f"Reports/PostMarket/{d}_hk.html").exists()
    assert not (output_tree / "Reports/PostMarket/2026_05_08_us.html").exists()
    assert not (output_tree / "Reports/PostMarket/2026_05_08_hk.html").exists()


def test_premarket_html_uses_seven_day_window(output_tree: Path) -> None:
    # Reports/PreMarket/<date>_us_premarket.html — 7 days.
    for d in ("2026_05_15", "2026_05_09", "2026_05_08"):
        _touch(output_tree / f"Reports/PreMarket/{d}_us_premarket.html")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    assert (output_tree / "Reports/PreMarket/2026_05_15_us_premarket.html").exists()
    assert (output_tree / "Reports/PreMarket/2026_05_09_us_premarket.html").exists()
    assert not (output_tree / "Reports/PreMarket/2026_05_08_us_premarket.html").exists()


def test_premarket_markdown_state_uses_two_day_window(output_tree: Path) -> None:
    # state/premarket_catalyst_<date>.md is the append-across-scans
    # accumulator; only today's is ever appended to. Today = 15, cutoff = 14.
    for d in ("2026_05_15", "2026_05_14", "2026_05_13"):
        _touch(output_tree / f"state/premarket_catalyst_{d}.md")

    cleanup_old_outputs(output_tree, date(2026, 5, 15))

    assert (output_tree / "state/premarket_catalyst_2026_05_15.md").exists()
    assert (output_tree / "state/premarket_catalyst_2026_05_14.md").exists()
    assert not (output_tree / "state/premarket_catalyst_2026_05_13.md").exists()
