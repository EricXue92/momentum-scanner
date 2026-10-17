"""The detached catalyst-report subprocess outlives the scan's caffeinate
assertion (start_new_session), so it must carry its own."""

import subprocess

import main


def _capture(monkeypatch):
    argv: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda args, **kw: argv.append(list(args)) or None
    )
    return argv


def test_spawn_wraps_subprocess_in_caffeinate(monkeypatch, tmp_path):
    fake_bin = tmp_path / "caffeinate"
    fake_bin.write_text("")
    monkeypatch.setattr(main, "_CAFFEINATE_BIN", str(fake_bin))
    argv = _capture(monkeypatch)
    main._spawn_catalyst_report(
        fresh=["AAA"], snapshot_rows={}, today_iso="2026-10-17", offset=-20,
        config={"morning_gap_catalyst": {"enabled": True}},
    )
    assert argv and argv[0][:3] == [str(fake_bin), "-i", "-s"]
    assert "report.morning" in argv[0]


def test_spawn_without_caffeinate_binary_runs_bare(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_CAFFEINATE_BIN", str(tmp_path / "missing"))
    argv = _capture(monkeypatch)
    main._spawn_catalyst_report(
        fresh=["AAA"], snapshot_rows={}, today_iso="2026-10-17", offset=-20,
        config={"morning_gap_catalyst": {"enabled": True}},
    )
    assert argv and "caffeinate" not in argv[0][0]
    assert "report.morning" in argv[0]
