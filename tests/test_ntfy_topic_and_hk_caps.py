"""ntfy topic kept out of git (public repo) and HK Longs market-cap
fallbacks (2026-11-12)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

import futu_sync
import hk_eod
import notify


# --- ntfy topic ---------------------------------------------------------------


def test_config_topic_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "from-env")
    assert notify._topic({"ntfy_topic": "from-config"}) == "from-config"


def test_empty_config_topic_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "from-env")
    assert notify._topic({"ntfy_topic": ""}) == "from-env"
    assert notify._topic({}) == "from-env"


def test_dotenv_is_the_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify, "_topic_from_dotenv", lambda: "from-dotenv")
    assert notify._topic({}) == "from-dotenv"


def test_no_topic_anywhere() -> None:
    assert notify._topic({}) is None  # conftest strips env + .env


def test_dotenv_parser_reads_only_the_topic_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        '# comment\nDEEPSEEK_API_KEY=sk-x\nexport NTFY_TOPIC="abc-123"\n', encoding="utf-8"
    )
    monkeypatch.undo()  # drop the conftest stub of _topic_from_dotenv
    monkeypatch.setattr(notify, "_DOTENV_PATH", env)
    monkeypatch.setattr(notify, "_dotenv_topic", False)
    assert notify._topic_from_dotenv() == "abc-123"

    monkeypatch.setattr(notify, "_DOTENV_PATH", tmp_path / "missing")
    monkeypatch.setattr(notify, "_dotenv_topic", False)
    assert notify._topic_from_dotenv() is None


def test_repo_config_carries_no_topic() -> None:
    """The repository is public: a real topic in config.toml is a leak."""
    import tomllib

    cfg = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "config.toml").read_text(encoding="utf-8")
    )
    assert cfg["notify"]["ntfy_topic"] == ""


# --- Futu snapshot retry ------------------------------------------------------


class _Ctx:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def get_market_snapshot(self, batch):
        self.calls += 1
        return self.results.pop(0)

    def close(self):
        pass


def _fake_futu(monkeypatch: pytest.MonkeyPatch, ctx: _Ctx) -> list[float]:
    mod = types.ModuleType("futu")
    mod.RET_OK = 0
    mod.OpenQuoteContext = lambda host, port: ctx
    monkeypatch.setitem(sys.modules, "futu", mod)
    monkeypatch.setattr(futu_sync, "_opend_reachable", lambda h, p: True)
    sleeps: list[float] = []
    monkeypatch.setattr(futu_sync.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


_SNAP = pd.DataFrame({"code": ["HK.00700"], "total_market_val": [4.0e12]})


def test_snapshot_failure_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _Ctx([(-1, "rate limited"), (0, _SNAP)])
    sleeps = _fake_futu(monkeypatch, ctx)
    caps = futu_sync.get_market_caps_futu(["HKEX:700"], market="HK")
    assert caps == {"HKEX:700": 4.0e12}
    assert ctx.calls == 2 and sleeps == [futu_sync._SNAPSHOT_RETRY_DELAY_SEC]


def test_snapshot_failing_twice_still_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _Ctx([(-1, "down"), (-1, "down")])
    _fake_futu(monkeypatch, ctx)
    assert futu_sync.get_market_caps_futu(["HKEX:700"], market="HK") is None
    assert ctx.calls == 2


# --- yfinance cap fallback ----------------------------------------------------


def _metrics() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "last_price": [400.0, 10.0, 80.0, 90.0],
            "avg_dollar_vol_20d": [5e9, 5e9, 1e6, float("nan")],
        },
        index=["HK.00700", "HK.00005", "HK.01234", "HK.09999"],
    )


def test_yf_fallback_only_asks_for_names_that_can_matter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main

    asked: list[str] = []
    monkeypatch.setattr(
        main, "_get_market_cap", lambda t: asked.append(t) or (None if t == "9999.HK" else 1e11)
    )
    monkeypatch.setattr(hk_eod.time, "sleep", lambda s: None)

    caps = hk_eod._yf_caps_fallback(_metrics(), {"min_price": 50.0})

    # below the price floor / below the dollar-volume floor are not looked up;
    # a short history (NaN dollar volume = IPO ladder) is.
    assert asked == ["0700.HK", "9999.HK"]
    assert caps == {"HK.00700": 1e11}


def test_yf_fallback_gives_up_loudly_on_a_huge_list(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    import main

    monkeypatch.setattr(main, "_get_market_cap", lambda t: pytest.fail("must not fetch"))
    monkeypatch.setattr(hk_eod, "_YF_CAPS_MAX_NAMES", 1)
    with caplog.at_level("ERROR", logger=hk_eod.logger.name):
        assert hk_eod._yf_caps_fallback(_metrics(), {}) == {}
    assert any("EMPTY today" in r.message for r in caplog.records)
