"""`_yf_download_with_retry` must back off long enough for Yahoo's throttle
window (>= 30s, see `_retry_sparse_in_batch`); the old fixed 3s between
attempts retried inside the same throttled window and never recovered."""

import pandas as pd

import main


def test_retry_backoff_grows_and_covers_throttle_window(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: pd.DataFrame())
    out = main._yf_download_with_retry(["AAA", "BBB"], period="2mo")
    assert out is not None and out.empty
    assert len(sleeps) == 2  # 3 attempts, 2 waits
    assert sleeps[1] > sleeps[0]
    assert sum(sleeps) >= 20
