from pathlib import Path

import pytest

from report import ranker


@pytest.fixture
def fake_us_dir(tmp_path: Path) -> Path:
    """Build a fake output/TV/US directory with dated .txt files."""
    d = tmp_path / "TV" / "US"
    d.mkdir(parents=True)
    # date stem matches "2026_05_07" used by the rest of the pipeline.
    files = {
        "2026_05_07_TheSetup.txt": "NASDAQ:AAPL,NYSE:CRWV",
        "2026_05_07_EarningsGap.txt": "NASDAQ:AAPL,NASDAQ:NVDA",  # AAPL dup
        "2026_05_07_HighVolume.txt": "NASDAQ:NVDA,NASDAQ:TSLA",  # NVDA dup
        "2026_05_07_Leaders.txt": "NASDAQ:META",
        "2026_05_07_GapUp.txt": "",
        "2026_05_07_NewHigh52W.txt": "NYSE:UBER",
        "2026_05_07_IPO.txt": "NASDAQ:RDDT",
        "2026_05_07_TopGainers.txt": "NASDAQ:PLTR",
        "2026_05_07_RS.txt": "NYSE:WMT",
    }
    for name, content in files.items():
        (d / name).write_text(content)
    return d


def test_read_dated_txt_returns_ticker_list(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("NASDAQ:AAPL,NASDAQ:NVDA,NYSE:UBER")
    assert ranker.read_dated_txt(p) == ["NASDAQ:AAPL", "NASDAQ:NVDA", "NYSE:UBER"]


def test_read_dated_txt_handles_empty(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("")
    assert ranker.read_dated_txt(p) == []


def test_read_dated_txt_missing_file_returns_empty(tmp_path: Path):
    assert ranker.read_dated_txt(tmp_path / "nonexistent.txt") == []


def test_read_dated_txt_strips_whitespace(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("  NASDAQ:AAPL , NASDAQ:NVDA  \n")
    assert ranker.read_dated_txt(p) == ["NASDAQ:AAPL", "NASDAQ:NVDA"]


def test_collect_market_groups(fake_us_dir):
    result = ranker.collect_market_groups(fake_us_dir, "2026_05_07", "us")
    # Returns dict[group_name -> list[ticker]] in priority order
    assert list(result.keys()) == [
        "TheSetup", "EarningsGap", "HighVolume", "Leaders", "GapUp",
        "NewHigh52W", "IPO", "TopGainers", "RS",
    ]
    assert result["EarningsGap"] == ["NASDAQ:AAPL", "NASDAQ:NVDA"]
    assert result["GapUp"] == []


def test_rank_and_cap_priority_dedup(fake_us_dir):
    groups = ranker.collect_market_groups(fake_us_dir, "2026_05_07", "us")
    analyzed, truncated = ranker.rank_and_cap(groups, cap=50)
    tickers_only = [t for t, _ in analyzed]
    # NVDA appears in both EarningsGap and HighVolume; should land in the higher-priority one only.
    assert tickers_only.count("NASDAQ:NVDA") == 1
    # AAPL appears in TheSetup and EarningsGap; TheSetup is highest priority.
    assert tickers_only.count("NASDAQ:AAPL") == 1
    assert analyzed[0] == ("NASDAQ:AAPL", "TheSetup")
    assert analyzed[1] == ("NYSE:CRWV", "TheSetup")
    assert analyzed[2] == ("NASDAQ:NVDA", "EarningsGap")
    assert analyzed[3] == ("NASDAQ:TSLA", "HighVolume")
    assert truncated == []


def test_rank_and_cap_truncates_at_limit(fake_us_dir):
    groups = ranker.collect_market_groups(fake_us_dir, "2026_05_07", "us")
    analyzed, truncated = ranker.rank_and_cap(groups, cap=3)
    assert len(analyzed) == 3
    assert len(truncated) == 6  # 9 unique total - 3 = 6
    # Cap honored.
    assert [t for t, _ in analyzed] == [
        "NASDAQ:AAPL", "NYSE:CRWV", "NASDAQ:NVDA",
    ]
    # Lowest-priority survivors fall into truncated.
    assert ("NYSE:WMT", "RS") in truncated


def test_rank_and_cap_empty():
    analyzed, truncated = ranker.rank_and_cap({}, cap=50)
    assert analyzed == []
    assert truncated == []
