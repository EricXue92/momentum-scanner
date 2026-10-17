"""Throttle recovery pass in the k-line fetchers.

2026-10-17 10:43-10:53 (Mac on battery, Yahoo throttled): the IPO batch and
the SPY benchmark both came back empty and were silently skipped, and the
rs-line audit got 13/91 names from a 78/91-sparse batch — the 78 were then
reported as "insufficient history" and the strongest-RS snapshot was cut from
13 names. A one-batch call never cooled down or retried. Both fetchers now
make ONE recovery pass, after a cooldown, over the tickers that a failed or
throttled batch left without data.
"""

import pandas as pd

FIELDS = ["Open", "High", "Low", "Close", "Volume"]


def _grid(tickers, n=80):
    idx = pd.date_range(end="2026-05-21", periods=n, freq="B")
    if len(tickers) == 1:
        return pd.DataFrame(100.0, index=idx, columns=FIELDS)
    return pd.DataFrame(
        100.0, index=idx, columns=pd.MultiIndex.from_product([tickers, FIELDS])
    )


def _with_nan(frame, ticker):
    out = frame.copy()
    for f in FIELDS:
        out[(ticker, f)] = float("nan")
    return out


class _Downloads:
    """Scripted _yf_download_with_retry: pops one response per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, tickers, **kwargs):
        self.calls.append(list(tickers))
        resp = self.responses.pop(0)
        return resp(tickers) if callable(resp) else resp


# --- US: us_rs_3m.fetch_us_klines_yf ---


def _patch_us(monkeypatch, downloads, throttled=False):
    import us_rs_3m
    sleeps: list[float] = []
    monkeypatch.setattr("us_rs_3m._yf_download_with_retry", downloads, raising=False)
    monkeypatch.setattr(
        "us_rs_3m._retry_sparse_in_batch", lambda *a, **k: throttled, raising=False
    )
    monkeypatch.setattr("us_rs_3m.time.sleep", lambda s: sleeps.append(s))
    return us_rs_3m, sleeps


def test_us_failed_single_batch_gets_one_recovery_pass(monkeypatch):
    downloads = _Downloads([None, _grid])
    us_rs_3m, sleeps = _patch_us(monkeypatch, downloads)
    klines = us_rs_3m.fetch_us_klines_yf(["AAA", "BBB"], period="6mo")
    assert sorted(klines) == ["AAA", "BBB"]
    assert downloads.calls == [["AAA", "BBB"], ["AAA", "BBB"]]
    assert 30 in sleeps  # throttle cooldown before the recovery pass


def test_us_spy_benchmark_failure_is_recovered(monkeypatch):
    downloads = _Downloads([pd.DataFrame(), _grid])
    us_rs_3m, _ = _patch_us(monkeypatch, downloads)
    assert us_rs_3m._fetch_spy_kline() is not None


def test_us_throttled_batch_retries_only_the_names_it_lost(monkeypatch):
    downloads = _Downloads([lambda t: _with_nan(_grid(t), "BBB"), _grid])
    us_rs_3m, _ = _patch_us(monkeypatch, downloads, throttled=True)
    klines = us_rs_3m.fetch_us_klines_yf(["AAA", "BBB"], period="6mo")
    assert sorted(klines) == ["AAA", "BBB"]
    assert downloads.calls[1] == ["BBB"]


def test_us_short_history_in_a_throttled_batch_is_not_retried(monkeypatch):
    # A fresh IPO with 5 rows is real data, not a throttle hole: a batch of
    # only such names trips the >50%-sparse heuristic, but nothing is missing.
    downloads = _Downloads([lambda t: _grid(t, n=5)])
    us_rs_3m, sleeps = _patch_us(monkeypatch, downloads, throttled=True)
    klines = us_rs_3m.fetch_us_klines_yf(["NEW1", "NEW2"], period="1y")
    assert sorted(klines) == ["NEW1", "NEW2"]
    assert len(downloads.calls) == 1
    assert sleeps == []


def test_us_healthy_batch_skips_recovery(monkeypatch):
    downloads = _Downloads([_grid])
    us_rs_3m, sleeps = _patch_us(monkeypatch, downloads)
    us_rs_3m.fetch_us_klines_yf(["AAA", "BBB"], period="6mo")
    assert len(downloads.calls) == 1
    assert sleeps == []


def test_us_recovery_is_a_single_pass(monkeypatch):
    downloads = _Downloads([None, None])
    us_rs_3m, _ = _patch_us(monkeypatch, downloads)
    assert us_rs_3m.fetch_us_klines_yf(["AAA", "BBB"], period="6mo") == {}
    assert len(downloads.calls) == 2


def test_us_recovery_can_be_disabled(monkeypatch):
    downloads = _Downloads([None])
    us_rs_3m, _ = _patch_us(monkeypatch, downloads)
    assert us_rs_3m.fetch_us_klines_yf(["AAA"], period="6mo", recover=False) == {}
    assert len(downloads.calls) == 1


# --- HK: hk_eod.fetch_hk_klines_yf ---


def _patch_hk(monkeypatch, downloads, throttled=False):
    import hk_eod
    import main
    sleeps: list[float] = []
    monkeypatch.setattr(main, "_yf_download_with_retry", downloads)
    monkeypatch.setattr(main, "_retry_sparse_in_batch", lambda *a, **k: throttled)
    monkeypatch.setattr(hk_eod.time, "sleep", lambda s: sleeps.append(s))
    return hk_eod, sleeps


def test_hk_failed_batch_gets_one_recovery_pass(monkeypatch):
    downloads = _Downloads([None, _grid])
    hk_eod, sleeps = _patch_hk(monkeypatch, downloads)
    klines = hk_eod.fetch_hk_klines_yf(["0700", "0005"], period="2y")
    assert sorted(klines) == ["HK.00005", "HK.00700"]
    assert downloads.calls[1] == ["0700.HK", "0005.HK"]
    assert 30 in sleeps


def test_hk_throttled_batch_retries_only_the_names_it_lost(monkeypatch):
    downloads = _Downloads([lambda t: _with_nan(_grid(t), "0005.HK"), _grid])
    hk_eod, _ = _patch_hk(monkeypatch, downloads, throttled=True)
    klines = hk_eod.fetch_hk_klines_yf(["0700", "0005"], period="2y")
    assert sorted(klines) == ["HK.00005", "HK.00700"]
    assert downloads.calls[1] == ["0005.HK"]


def test_hk_healthy_batch_skips_recovery(monkeypatch):
    downloads = _Downloads([_grid])
    hk_eod, sleeps = _patch_hk(monkeypatch, downloads)
    hk_eod.fetch_hk_klines_yf(["0700", "0005"], period="2y")
    assert len(downloads.calls) == 1
    assert sleeps == []


# --- wall-clock budget: a total outage must not double the cloud runtime ---


def test_us_recovery_skipped_when_first_pass_ran_too_long(monkeypatch):
    # 59 batches x (retries + 30s cooldown) ≈ 54 min of a dead Yahoo; a second
    # pass would hit the workflow's 60-min timeout before `return 1`.
    downloads = _Downloads([None])
    us_rs_3m, _ = _patch_us(monkeypatch, downloads)
    clock = iter([0.0, us_rs_3m._RECOVERY_MAX_ELAPSED_SEC + 1])
    monkeypatch.setattr("us_rs_3m.time.monotonic", lambda: next(clock))
    assert us_rs_3m.fetch_us_klines_yf(["AAA"], period="6mo") == {}
    assert len(downloads.calls) == 1


def test_hk_recovery_skipped_when_first_pass_ran_too_long(monkeypatch):
    import hk_eod
    downloads = _Downloads([None])
    _patch_hk(monkeypatch, downloads)
    clock = iter([0.0, hk_eod._RECOVERY_MAX_ELAPSED_SEC + 1])
    monkeypatch.setattr(hk_eod.time, "monotonic", lambda: next(clock))
    assert hk_eod.fetch_hk_klines_yf(["0700"], period="2y") == {}
    assert len(downloads.calls) == 1
