"""`_sort_seen_by_rs_3m` — eod_seen_US.txt is kept ordered by 3M RS strength
(strongest first). Order only: the ticker set must never change."""
import pandas as pd

from main import _load_seen, _sort_seen_by_rs_3m


def _table(rows: dict[str, tuple[float, int]]) -> pd.DataFrame:
    df = pd.DataFrame(
        [{"ticker": t, "raw_score": r, "rs_percentile": p} for t, (r, p) in rows.items()]
    )
    return df.set_index("ticker")


def _lines(path) -> list[str]:
    return path.read_text().splitlines()


def test_sorts_strongest_first(tmp_path):
    p = tmp_path / "eod_seen_US.txt"
    p.write_text("AAA\nBBB\nCCC\n")
    table = _table({"AAA": (0.1, 80), "BBB": (0.9, 99), "CCC": (0.5, 95)})
    _sort_seen_by_rs_3m(p, table)
    assert _lines(p) == ["BBB", "CCC", "AAA"]


def test_raw_score_breaks_percentile_ties(tmp_path):
    p = tmp_path / "eod_seen_US.txt"
    p.write_text("AAA\nBBB\n")
    table = _table({"AAA": (0.30, 95), "BBB": (0.32, 95)})
    _sort_seen_by_rs_3m(p, table)
    assert _lines(p) == ["BBB", "AAA"]


def test_missing_and_nan_tickers_go_last_alphabetically(tmp_path):
    p = tmp_path / "eod_seen_US.txt"
    p.write_text("ZZZ\nAAA\nNAN\nBBB\n")
    table = _table({"AAA": (0.1, 80), "NAN": (float("nan"), 0)})
    _sort_seen_by_rs_3m(p, table)
    assert _lines(p) == ["AAA", "BBB", "NAN", "ZZZ"]


def test_falls_back_to_percentile_without_raw_score(tmp_path):
    p = tmp_path / "eod_seen_US.txt"
    p.write_text("AAA\nBBB\n")
    table = _table({"AAA": (0.1, 80), "BBB": (0.9, 99)}).drop(columns=["raw_score"])
    _sort_seen_by_rs_3m(p, table)
    assert _lines(p) == ["BBB", "AAA"]


def test_no_table_leaves_file_untouched(tmp_path):
    p = tmp_path / "eod_seen_US.txt"
    p.write_text("BBB\nAAA\n")
    _sort_seen_by_rs_3m(p, None)
    _sort_seen_by_rs_3m(p, pd.DataFrame())
    assert _lines(p) == ["BBB", "AAA"]


def test_missing_master_is_a_noop(tmp_path):
    p = tmp_path / "eod_seen_US.txt"
    _sort_seen_by_rs_3m(p, _table({"AAA": (0.1, 80)}))
    assert not p.exists()


def test_ticker_set_is_preserved(tmp_path):
    p = tmp_path / "eod_seen_US.txt"
    p.write_text("CCC\nAAA\nBBB\nAAA\n")
    before = _load_seen(p)
    _sort_seen_by_rs_3m(p, _table({"AAA": (0.1, 80), "CCC": (0.5, 95)}))
    assert _load_seen(p) == before
    assert _lines(p) == ["CCC", "AAA", "BBB"]


def _hk_table(rows: dict[str, int]) -> pd.DataFrame:
    # Shape of hk_rs._split_combined's 3M frame: Futu-code index, percentile only.
    return pd.DataFrame({"rs_percentile": rows})


def test_hk_master_maps_tv_ticker_to_futu_code(tmp_path):
    from rs_line_audit import _hk_master_to_futu

    p = tmp_path / "eod_seen_HK.txt"
    p.write_text("HKEX:1138\nHKEX:522\nHKEX:9988\n")
    table = _hk_table({"HK.01138": 70, "HK.00522": 99, "HK.09988": 85})
    _sort_seen_by_rs_3m(p, table, key=_hk_master_to_futu)
    assert _lines(p) == ["HKEX:522", "HKEX:9988", "HKEX:1138"]


def test_unparseable_entry_goes_last_instead_of_aborting(tmp_path):
    from rs_line_audit import _hk_master_to_futu

    p = tmp_path / "eod_seen_HK.txt"
    p.write_text("HKEX:abc\nHKEX:522\nHKEX:700\n")
    table = _hk_table({"HK.00522": 60, "HK.00700": 90})
    _sort_seen_by_rs_3m(p, table, key=_hk_master_to_futu)
    assert _lines(p) == ["HKEX:700", "HKEX:522", "HKEX:abc"]
