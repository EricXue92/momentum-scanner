"""Tests for `main._retry_on_network_error` — the network-error retry around
the conditional RS group.

Regression: 2026-10-17 (SPY -1.11%, QQQ -2.50%) the RS condition was met but
the single Finviz screener call died with a NameResolutionError (the Mac was
on battery, lid closed, and only progressed in 5-15s DarkWake slivers where
Wi-Fi/DNS was not yet up). One exception = the day's RS list lost. The RS
group is the most valuable list on exactly such a day, so a network error now
waits for the network to come back and retries a few times before giving up.
"""
from __future__ import annotations

import socket

import pytest
import requests
from finviz.helper_functions.error_handling import ConnectionTimeout

import main


def test_returns_result_on_first_success() -> None:
    waits: list[int] = []
    out = main._retry_on_network_error(
        lambda: 42, label="[RS]", wait=lambda: waits.append(1) or True
    )
    assert out == 42
    assert waits == []


def test_retries_on_connection_error_then_succeeds() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='finviz.com'): Max retries exceeded "
                "(Caused by NameResolutionError(...))"
            )
        return "ok"

    waits: list[int] = []
    out = main._retry_on_network_error(
        fn, label="[RS]", attempts=3, wait=lambda: waits.append(1) or True
    )
    assert out == "ok"
    assert calls["n"] == 3
    assert len(waits) == 2  # one wait between each pair of attempts


def test_gives_up_after_attempts_and_reraises() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise socket.gaierror(8, "nodename nor servname provided, or not known")

    with pytest.raises(OSError):
        main._retry_on_network_error(fn, label="[RS]", attempts=3, wait=lambda: True)
    assert calls["n"] == 3


def test_non_network_error_is_not_retried() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise KeyError("filters")

    with pytest.raises(KeyError):
        main._retry_on_network_error(fn, label="[RS]", attempts=3, wait=lambda: True)
    assert calls["n"] == 1


def test_finviz_connection_timeout_is_retried() -> None:
    """finviz's own ConnectionTimeout is a bare Exception subclass, not OSError."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionTimeout("https://finviz.com/screener.ashx")
        return "ok"

    assert main._retry_on_network_error(fn, label="[RS]", wait=lambda: True) == "ok"
    assert calls["n"] == 2


def test_finviz_too_many_requests_is_retried() -> None:
    """finviz_timeout re-raises the library's rate-limit sentinel as a plain
    Exception("Too many requests.") after its bounded retries."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Too many requests.")
        return "ok"

    assert main._retry_on_network_error(fn, label="[RS]", wait=lambda: True) == "ok"
    assert calls["n"] == 2


def test_wait_returning_false_still_retries() -> None:
    """A failed network probe must not short-circuit the remaining attempts —
    the probe is a courtesy delay, the retry itself is the real test."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise requests.exceptions.ConnectionError("down")
        return "ok"

    assert main._retry_on_network_error(fn, label="[RS]", wait=lambda: False) == "ok"


def test_default_wait_probes_finviz(monkeypatch) -> None:
    """Default wait = net_ready.wait_for_network against finviz.com, bounded."""
    seen: dict = {}

    def fake_wait(hosts, **kw):
        seen["hosts"] = hosts
        seen["kw"] = kw
        return True

    monkeypatch.setattr(main, "wait_for_network", fake_wait)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("down")
        return "ok"

    assert main._retry_on_network_error(fn, label="[RS]") == "ok"
    assert seen["hosts"] == ["finviz.com"]
    assert 0 < seen["kw"]["max_wait_seconds"] <= 300
