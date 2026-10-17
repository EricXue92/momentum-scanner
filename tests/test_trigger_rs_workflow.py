"""Tests for scripts/trigger_rs_workflow.sh's post-dispatch watch.

Regression: 2026-10-17 09:01 the trigger dispatched fine, but the Mac (on
battery, lid closed) went back to sleep 2s later. `gh run watch` was frozen
until the lid opened at 09:08:31, exited non-zero on its dead connection,
and the single `gh run view` re-check ran before Wi-Fi was back → empty
conclusion → a high-priority "US RS workflow failed (conclusion=unknown)"
push. The run itself succeeded at 09:12. The re-check now polls until the
run reports `completed` (or the poll window ends) before deciding.

The script is exercised for real via subprocess with a fake `gh` / `curl`
(env overrides TRIGGER_*), so the shell logic under test is the shipped one.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trigger_rs_workflow.sh"

_FAKE_GH = r"""#!/bin/bash
# Records every call; answers by subcommand.
echo "$*" >> "$FAKE_GH_CALLS"
case "$*" in
  *" workflow run "*)
    echo "https://github.com/EricXue92/momentum-scanner/actions/runs/123"
    exit 0 ;;
  *" run watch "*)
    exit "${FAKE_GH_WATCH_EXIT:-1}" ;;
  *" run view "*)
    n=$(cat "$FAKE_GH_COUNTER" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$FAKE_GH_COUNTER"
    line=$(sed -n "${n}p" "$FAKE_GH_VIEW_SEQ")
    [ -z "$line" ] && line=$(tail -n 1 "$FAKE_GH_VIEW_SEQ")
    [ "$line" = "ERR" ] && exit 1
    echo "$line"
    exit 0 ;;
esac
echo "unexpected gh call: $*" >&2
exit 99
"""

_FAKE_CURL = r"""#!/bin/bash
echo "$*" >> "$FAKE_CURL_CALLS"
exit 0
"""


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def harness(tmp_path: Path):
    gh = _write_exec(tmp_path / "gh", _FAKE_GH)
    curl = _write_exec(tmp_path / "curl", _FAKE_CURL)
    calls = tmp_path / "gh_calls.txt"
    curl_calls = tmp_path / "curl_calls.txt"
    counter = tmp_path / "counter.txt"
    seq = tmp_path / "view_seq.txt"
    log = tmp_path / "trigger.log"

    def run(view_seq: list[str], *, watch_exit: int = 1, poll_tries: int = 5):
        seq.write_text("\n".join(view_seq) + "\n")
        env = {
            **os.environ,
            "TRIGGER_GH": str(gh),
            "TRIGGER_CURL": str(curl),
            "TRIGGER_LOG": str(log),
            "TRIGGER_CAFFEINATE": "/usr/bin/true",
            "TRIGGER_POLL_GAP": "0",
            "TRIGGER_POLL_TRIES": str(poll_tries),
            "TRIGGER_DISPATCH_GAP": "0",
            "FAKE_GH_CALLS": str(calls),
            "FAKE_GH_COUNTER": str(counter),
            "FAKE_GH_VIEW_SEQ": str(seq),
            "FAKE_GH_WATCH_EXIT": str(watch_exit),
            "FAKE_CURL_CALLS": str(curl_calls),
        }
        proc = subprocess.run(
            ["/bin/bash", str(SCRIPT), "update_us_rs_3m.yml", "US"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        return proc, log.read_text(), (
            curl_calls.read_text() if curl_calls.exists() else ""
        ), calls.read_text()

    return run


def test_watch_success_needs_no_recheck(harness) -> None:
    proc, log, curl_calls, gh_calls = harness(["completed success"], watch_exit=0)
    assert proc.returncode == 0
    assert "completed successfully" in log
    assert curl_calls == ""
    assert " run view " not in gh_calls


def test_network_blip_then_success_is_not_an_alert(harness) -> None:
    """The 2026-10-17 shape: watch dies, first re-checks can't reach GH,
    the run is actually green."""
    proc, log, curl_calls, _ = harness(["ERR", "ERR", "completed success"])
    assert proc.returncode == 0
    assert "conclusion=success" in log
    assert curl_calls == ""


def test_still_running_is_polled_until_completed(harness) -> None:
    proc, log, curl_calls, gh_calls = harness(
        ["in_progress ", "in_progress ", "completed success"]
    )
    assert proc.returncode == 0
    assert gh_calls.count(" run view ") == 3
    assert curl_calls == ""


def test_real_failure_still_alerts(harness) -> None:
    proc, log, curl_calls, _ = harness(["in_progress ", "completed failure"])
    assert proc.returncode == 1
    assert "conclusion=failure" in log
    assert "US RS workflow failed" in curl_calls
    assert curl_calls.count("ntfy.sh") == 1


def test_unconfirmed_after_window_alerts_as_unconfirmed(harness) -> None:
    """GH unreachable for the whole window: say so — do NOT claim failure."""
    proc, log, curl_calls, gh_calls = harness(["ERR"], poll_tries=3)
    assert proc.returncode == 2
    assert gh_calls.count(" run view ") == 3
    assert "unconfirmed" in curl_calls
    assert "workflow failed" not in curl_calls
    assert "could not confirm" in log
