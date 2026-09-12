"""Config-shape guard for the `the_setup` US Longs group (heavy volume + gap up 5%).

Highest-priority event group: first `[[longs]]` entry so it wins within-Longs
dedup; deduped against the cross-day master like every other event group;
file stem `TheSetup` via the `[futu.groups]` mapping."""
import tomllib
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


@pytest.fixture(scope="module")
def cfg() -> dict:
    return tomllib.loads(CONFIG.read_text("utf-8"))


@pytest.fixture(scope="module")
def the_setup(cfg) -> dict:
    by_key = {g["key"]: g for g in cfg["longs"]}
    assert "the_setup" in by_key, "the_setup group missing from [[longs]]"
    return by_key["the_setup"]


def test_the_setup_is_first_longs_group(cfg):
    assert cfg["longs"][0]["key"] == "the_setup"


def test_the_setup_filters_gap_up_5_and_price_over_10(the_setup):
    filters = set(the_setup["filters"])
    assert "ta_gap_u5" in filters
    assert "sh_price_o10" in filters
    assert "sh_price_o20" not in filters
    # Baseline shared with the other Longs groups.
    assert {"ind_stocksonly", "cap_smallover", "sh_avgvol_o500",
            "ta_sma50_pa", "ta_sma200_pa"} <= filters


def test_the_setup_heavy_volume_is_local_rvol_3x_20d(the_setup):
    assert the_setup["min_relative_volume"] == 3
    assert the_setup.get("relative_volume_days", 20) == 20


def test_the_setup_has_no_master_dedup_exemption(the_setup):
    # Deliberately deduped against eod_seen_US like every event group.
    assert "dedup_seen" not in the_setup


def test_every_longs_key_has_futu_and_tv_mapping(cfg):
    futu_groups = cfg["futu"]["groups"]
    tv_lists = cfg["tv_sync"]["lists"]
    for g in cfg["longs"]:
        k = f"longs_{g['key']}"
        assert k in futu_groups, f"[futu.groups] missing {k}"
        assert k in tv_lists, f"[tv_sync.lists] missing {k}"


def test_the_setup_file_stem_and_append_only(cfg):
    assert cfg["futu"]["groups"]["longs_the_setup"] == "TheSetup"
    assert cfg["tv_sync"]["lists"]["longs_the_setup"] == "TheSetup"
    assert "TheSetup" in cfg["futu"]["append_only_groups"]
    assert "TheSetup" in cfg["tv_sync"]["append_only_lists"]


def test_the_setup_is_an_eod_repeat_event_group(cfg):
    assert "the_setup" in cfg["eod_repeat"]["keys"]
