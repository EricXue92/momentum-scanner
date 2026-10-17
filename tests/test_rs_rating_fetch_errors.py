"""`fetch_rs_table` must degrade (retry → cache fallback → None), never raise.

It is called from the us-eod main flow outside any try/except, before a
single .txt is written. `urlopen` can fail with exceptions that are NOT
OSError: `http.client.IncompleteRead` (truncated body) and
`UnicodeDecodeError` (garbage body). Either one used to abort the whole run.
"""

import http.client
from pathlib import Path

import pytest

import rs_rating


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rs_rating.time, "sleep", lambda *_: None)


class _Resp:
    def __init__(self, body: bytes | None = None, exc: Exception | None = None):
        self._body, self._exc = body, exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        if self._exc is not None:
            raise self._exc
        return self._body or b""


def test_incomplete_read_falls_back_to_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "rs_rating_2026_10_16.csv").write_text("Ticker,Percentile\nAAPL,95\n")

    def _truncated(*_a, **_k):
        return _Resp(exc=http.client.IncompleteRead(b"Ticker,Perc"))

    monkeypatch.setattr(rs_rating, "urlopen", _truncated)
    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_17")
    assert table == {"AAPL": 95}


def test_undecodable_body_returns_none_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rs_rating, "urlopen", lambda *a, **k: _Resp(body=b"\xff\xfe\xfd"))
    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_17") is None


def test_incomplete_read_is_retried_before_giving_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def _flaky(*_a, **_k):
        calls.append(1)
        if len(calls) < 2:
            return _Resp(exc=http.client.IncompleteRead(b""))
        return _Resp(body=b"Ticker,Percentile\nMSFT,91\n")

    monkeypatch.setattr(rs_rating, "urlopen", _flaky)
    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_17") == {"MSFT": 91}
    assert len(calls) == 2
