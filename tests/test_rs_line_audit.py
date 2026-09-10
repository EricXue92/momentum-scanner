import pandas as pd

from rs_line_audit import classify, render_report, write_top_n, _hk_master_to_futu


def _frame(d):
    return pd.DataFrame.from_dict(
        d, orient="index", columns=["rs_ema", "rs_ema_chg_5d"]
    )


def _rev(d):
    """d: {id: (adr_pct, ret_lb, ret_per_adr)}"""
    return pd.DataFrame.from_dict(d, orient="index", columns=["adr_pct", "ret_lb", "ret_per_adr"])


def test_render_sorts_weakest_first_and_flags_cuts():
    feats = _frame({
        "AAA": (0.01, 0.06),    # +6%  strong
        "BBB": (0.02, -0.032),  # -3.2% cut
        "CCC": (0.03, -0.004),  # -0.4% within band
        "DDD": (0.04, -0.009),  # -0.9% cut
    })
    rev = _rev({})  # no reversal data -> cuts get "—" + DROP
    text = render_report(["AAA", "BBB", "CCC", "DDD"], feats, rev, tolerance=0.005,
                         adr_mult=1.5, market="US", as_of="2026-05-28")
    # weakest first: BBB(-3.2) DDD(-0.9) CCC(-0.4) AAA(+6)
    assert text.index("BBB") < text.index("DDD") < text.index("CCC") < text.index("AAA")
    # cuts (BBB, DDD) get DROP; CCC within band gets no flag
    bbb_line = next(l for l in text.splitlines() if "BBB" in l and "rank" not in l)
    ccc_line = next(l for l in text.splitlines() if "CCC" in l and "rank" not in l)
    assert "DROP" in bbb_line
    assert "DROP" not in ccc_line and "EXEMPT" not in ccc_line


def test_render_lists_unknowns_and_counts():
    feats = _frame({"AAA": (0.01, 0.06), "BBB": (0.02, -0.032)})
    rev = _rev({})
    text = render_report(["AAA", "BBB", "ZZZ", "YYY"], feats, rev, tolerance=0.005,
                         adr_mult=1.5, market="US", as_of="2026-05-28")
    assert "ZZZ" in text and "YYY" in text          # unknown names shown
    assert "scanned: 4" in text
    assert "scored: 2" in text
    assert "unknown: 2" in text
    # No anomaly_ids passed → all unknowns categorized as insufficient history
    assert "insufficient history" in text
    assert "data anomaly" not in text
    assert "short-hist: 2, anomaly: 0" in text
    assert "direction-cut: 1" in text


def test_render_splits_unknown_into_anomaly_and_short_history():
    feats = _frame({"AAA": (0.01, 0.06)})
    rev = _rev({})
    text = render_report(
        ["AAA", "ZZZ", "YYY"], feats, rev, tolerance=0.005,
        adr_mult=1.5, market="US", as_of="2026-06-13",
        anomaly_ids={"YYY"},
    )
    # both unknowns appear, on separate labeled lines
    lines = text.splitlines()
    short_line = next(l for l in lines if "insufficient history" in l)
    anom_line  = next(l for l in lines if "data anomaly" in l)
    assert "ZZZ" in short_line and "YYY" not in short_line
    assert "YYY" in anom_line and "ZZZ" not in anom_line
    assert "short-hist: 1, anomaly: 1" in text


def test_render_handles_all_unknown_without_crash():
    text = render_report(["AAA", "BBB"], _frame({}), _rev({}), tolerance=0.005,
                         adr_mult=1.5, market="HK", as_of="2026-05-28")
    assert "scored: 0" in text and "unknown: 2" in text


def test_render_exempts_strong_reversal_among_cuts():
    feats = _frame({"AAA": (0.01, 0.06), "BBB": (0.02, -0.032), "CCC": (0.03, -0.02)})
    rev = _rev({"BBB": (9.0, 0.18, 2.0), "CCC": (8.0, 0.04, 0.5)})  # BBB strong, CCC weak
    text = render_report(["AAA", "BBB", "CCC"], feats, rev, tolerance=0.005,
                         adr_mult=1.5, market="US", as_of="2026-05-28")
    bbb = next(l for l in text.splitlines() if "BBB" in l and "rank" not in l)
    ccc = next(l for l in text.splitlines() if "CCC" in l and "rank" not in l)
    assert "EXEMPT" in bbb
    assert "DROP" in ccc and "EXEMPT" not in ccc
    assert "direction-cut: 2 -> exempt(reversing): 1, drop: 1" in text


def test_render_non_cut_rows_have_no_flag():
    feats = _frame({"AAA": (0.01, 0.06)})  # above band, not cut
    rev = _rev({"AAA": (5.0, 0.06, 1.2)})
    text = render_report(["AAA"], feats, rev, tolerance=0.005, adr_mult=1.5,
                         market="US", as_of="2026-05-28")
    aaa = next(l for l in text.splitlines() if "AAA" in l and "rank" not in l)
    assert "EXEMPT" not in aaa and "DROP" not in aaa
    assert "direction-cut: 0" in text


def test_classify_splits_drops_exempts_and_ranks_keeps_desc():
    feats = _frame({
        "AAA": (0.01, 0.06),    # +6%  strong  -> keep
        "BBB": (0.02, -0.032),  # cut, weak    -> DROP
        "CCC": (0.03, -0.02),   # cut, exempt  -> keep (exempt)
        "DDD": (0.04, -0.004),  # within band  -> keep
    })
    rev = _rev({"CCC": (8.0, 0.18, 2.0)})  # only CCC exempts
    b = classify(["AAA", "BBB", "CCC", "DDD", "ZZZ"], feats, rev,
                 tolerance=0.005, adr_mult=1.5)
    assert b["drops"] == ["BBB"]
    assert b["exempts"] == ["CCC"]
    assert b["unknowns"] == ["ZZZ"]
    # strongest first across kept (AAA +6, DDD -0.4, CCC -2.0), then unknowns
    assert b["keeps_ranked"] == ["AAA", "DDD", "CCC", "ZZZ"]


def test_write_top_n_takes_strongest_scored_only(tmp_path):
    # keeps_ranked is strongest-first with unknowns appended; unknowns have no
    # score and must never appear in the top-N file. US snapshots land in
    # TV/US so they import straight into TradingView.
    buckets = {"keeps_ranked": ["AAA", "BBB", "CCC", "ZZZ"], "unknowns": ["ZZZ"]}
    p = write_top_n(buckets, "us", "2026-08-30", tmp_path, top_n=3)
    assert p == tmp_path / "TV" / "US" / "rs_us_2026-08-30.txt"
    assert p.read_text() == "AAA,BBB,CCC\n"


def test_write_top_n_unknown_never_pads_short_list(tmp_path):
    buckets = {"keeps_ranked": ["AAA", "ZZZ"], "unknowns": ["ZZZ"]}
    p = write_top_n(buckets, "us", "2026-08-30", tmp_path, top_n=10)
    assert p.read_text() == "AAA\n"


def test_write_top_n_hk_filename(tmp_path):
    buckets = {"keeps_ranked": ["HKEX:2099", "HKEX:1548"], "unknowns": []}
    p = write_top_n(buckets, "hk", "2026-08-30", tmp_path, top_n=10)
    assert p == tmp_path / "hk_rs_2026-08-30.txt"
    assert p.read_text() == "HKEX:2099,HKEX:1548\n"


def test_write_top_n_skips_empty(tmp_path):
    # Empty-list convention: no 0-byte artifacts.
    buckets = {"keeps_ranked": ["ZZZ"], "unknowns": ["ZZZ"]}
    assert write_top_n(buckets, "us", "2026-08-30", tmp_path, top_n=10) is None
    assert list(tmp_path.iterdir()) == []


def test_top_n_from_config_default_and_override():
    from rs_line import top_n_from_config
    assert top_n_from_config({}) == 10
    assert top_n_from_config({"rs_line": {"top_n": 5}}) == 5


def test_hk_master_to_futu_pads_and_prefixes():
    assert _hk_master_to_futu("HKEX:522") == "HK.00522"
    assert _hk_master_to_futu("HKEX:1304") == "HK.01304"
    assert _hk_master_to_futu("148") == "HK.00148"   # tolerate bare code


def test_write_audit_files_lands_in_rs_audit_subdir(tmp_path):
    from rs_line_audit import write_audit_files
    buckets = {"drops": ["BBB", "DDD"], "exempts": [], "keeps_ranked": ["AAA", "CCC"]}
    report, drop, keep = write_audit_files(
        "report text", buckets, "us", "2026-09-10", tmp_path
    )
    audit = tmp_path / "rs-audit"
    assert report == audit / "rs_line_audit_US_2026-09-10.txt"
    assert drop == audit / "rs_line_audit_US_2026-09-10_drop.txt"
    assert keep == audit / "rs_line_audit_US_2026-09-10_keep_ranked.txt"
    assert report.read_text() == "report text\n"
    assert drop.read_text() == "BBB,DDD\n"
    assert keep.read_text() == "AAA,CCC\n"
    # nothing leaked to the output root
    assert not list(tmp_path.glob("rs_line_audit_*"))


def test_write_audit_files_hk_and_empty_buckets(tmp_path):
    from rs_line_audit import write_audit_files
    buckets = {"drops": [], "exempts": [], "keeps_ranked": []}
    report, drop, keep = write_audit_files("r", buckets, "hk", "2026-09-10", tmp_path)
    assert report.name == "rs_line_audit_HK_2026-09-10.txt"
    assert drop.read_text() == ""
    assert keep.read_text() == ""
