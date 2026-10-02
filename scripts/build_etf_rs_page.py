#!/usr/bin/env python3
"""Cloud-side build of the ETF RS trend page for GitHub Pages.

Runs the same ``etf_rs.run_etf_rs`` as us-eod (one ~50-ticker yfinance batch,
``[etf_rs]`` from config.toml) into a scratch output dir, then copies
``Reports/ETF/etf_rs_trend.html`` to ``<site_dir>/index.html`` for
``actions/upload-pages-artifact``. The dated ``.txt`` it also writes stays in
the scratch dir — nothing is committed back to the repo.

Exits 1 when no page was produced (fetch failure, list disabled/empty), so
the workflow fails before the deploy step and Pages keeps serving the
previous day's page instead of an empty one.

Usage: build_etf_rs_page.py [site_dir]   (default: _site)
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Make repo root importable so `etf_rs` resolves as a top-level module
# (mirrors how main.py imports it).
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

import etf_rs  # noqa: E402

logger = logging.getLogger("momentum_scanner")

_PAGE = Path("Reports") / "ETF" / "etf_rs_trend.html"


def build(site_dir: Path, config_path: Path = _REPO_ROOT / "config.toml") -> int:
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f).get("etf_rs", {})
    # Same calendar day the local us-eod stamps on the page.
    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    with tempfile.TemporaryDirectory() as scratch:
        etf_rs.run_etf_rs(cfg, Path(scratch), today)
        page = Path(scratch) / _PAGE
        if not page.is_file():
            logger.error("[ETF RS] no trend page produced; keeping the deployed one")
            return 1
        site_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(page, site_dir / "index.html")
    logger.info(f"[ETF RS] site built -> {site_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_site")))
