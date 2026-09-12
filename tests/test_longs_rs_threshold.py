"""Per-group 12M RS override for US Longs: a `[[longs]]` entry may set
`min_rs_percentile` (0 = no gate) instead of inheriting the global
`min_rs_percentile_longs`. TheSetup is the first user."""
import tomllib
from pathlib import Path

import main

CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def test_inherits_global_when_unset():
    assert main._longs_rs_threshold({"key": "gap_up"}, 90) == 90


def test_explicit_zero_disables_gate():
    assert main._longs_rs_threshold({"key": "the_setup", "min_rs_percentile": 0}, 90) == 0


def test_explicit_value_overrides_global():
    assert main._longs_rs_threshold({"key": "x", "min_rs_percentile": 70}, 90) == 70


def test_config_the_setup_has_no_rs_gate():
    cfg = tomllib.loads(CONFIG.read_text("utf-8"))
    the_setup = cfg["longs"][0]
    assert the_setup["key"] == "the_setup"
    assert the_setup["min_rs_percentile"] == 0
