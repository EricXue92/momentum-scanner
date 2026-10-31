"""Cumulative TradingView watchlist of the strongest constituents of the
strongest ETFs (``[etf_rs.leaders]``).

Every run, after ``etf_rs`` has written ``<date>_ETF_rs_1m.txt``, the top
``top_etfs`` (10) rows of THAT file are read back and the symbols of their
``   1M强: A > B > …`` continuation lines (``etf_holdings_rs``) are merged,
in rank order, into one candidate list (≤ 10 × 5 names; an ETF without a
line — IBIT, a 404, a fetch failure — contributes nothing and is not
back-filled from rank 11). The file, not the in-memory tables, is the
input on purpose: the list is "the names under the first ten rows of the
file you read every day", and a rerun / a hand-fixed file gives the same
answer.

Two outputs under ``output/TV/US/``, both comma-separated so they import
into TradingView as they are:

* ``ETF_rs_leaders.txt`` — the **master**: undated, append-only, in order of
  first appearance; never pruned, never aged out (no ``_RETENTION_RULES``
  entry matches an undated name). Delete it to reset. Hand edits are
  tolerated (newline- or comma-separated, duplicates dropped on read).
* ``<date>_ETF_rs_new.txt`` — the day's additions (names the master did not
  have), absent when nothing is new. Like every EOD list it is written
  once per day: a same-day rerun that finds further new names grows the
  master but keeps the first run's file (log says so). Aged out by the
  generic ``TV/US`` 5-day rule.

Soft step: ``run_etf_rs`` wraps it; a failure costs neither ranking file nor
the trend page. No ntfy, no Webull mirror, no Futu/TV sync, no eod_seen.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger("momentum_scanner")

_LABEL = "ETF RS 龙头"
MASTER_NAME = "ETF_rs_leaders.txt"
NEW_STEM = "ETF_rs_new"
DEFAULT_TOP_ETFS = 10

_EXTRA_PREFIX = "1M强:"
_EXTRA_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")       # " (前十大)"
_ROW_RE = re.compile(r"^(\d+)\.\s+(\S+)")                  # "12. XLV 🟢↑2 - …"
_SPLIT_RE = re.compile(r"[,\s]+")


def parse_extra(line: str) -> list[str]:
    """Inverse of ``etf_holdings_rs.format_extra``: ``"   1M强: A > B (前十大)"``
    → ``["A", "B"]``. Anything that is not such a line → ``[]``."""
    text = line.strip()
    if not text.startswith(_EXTRA_PREFIX):
        return []
    text = _EXTRA_SUFFIX_RE.sub("", text[len(_EXTRA_PREFIX):])
    return [s.strip() for s in text.split(">") if s.strip()]


def parse_ranking(text: str) -> list[tuple[str, list[str]]]:
    """``[(etf, strongest_constituents)]`` in file order, one entry per
    ``N. TICKER …`` row; the constituents come from the indented ``1M强:``
    line right under the row (``[]`` when the row has none)."""
    entries: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if m:
            entries.append((m.group(2), []))
        elif entries and line[:1].isspace():
            syms = parse_extra(line)
            if syms:
                entries[-1] = (entries[-1][0], syms)
    return entries


def candidates(entries: list[tuple[str, list[str]]], top_etfs: int = DEFAULT_TOP_ETFS) -> list[str]:
    """Ordered, de-duplicated union of the constituents of the first
    ``top_etfs`` entries (ETF rank first, then strength within the ETF)."""
    out: list[str] = []
    seen: set[str] = set()
    for _, syms in entries[:top_etfs]:
        for s in syms:
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def load_master(path: Path) -> list[str]:
    """The master as an ordered list; missing file → ``[]``. Accepts commas
    and/or whitespace as separators (hand edits), drops duplicates."""
    if not path.is_file():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in _SPLIT_RE.split(path.read_text(encoding="utf-8")):
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _write_csv(path: Path, symbols: list[str]) -> None:
    path.write_text(",".join(symbols) + "\n", encoding="utf-8")


def update_leaders(cfg: dict, output_dir: Path, today: date, ranking_path: Path | None) -> list[str]:
    """Grow the master from today's 1M ranking file; returns the names added
    today (``[]`` when disabled, no file, or nothing new). Writes
    ``<date>_ETF_rs_new.txt`` only when there is something new AND the day
    has no such file yet."""
    if not cfg.get("enabled", True):
        logger.info(f"[{_LABEL}] disabled in config; skipping")
        return []
    if ranking_path is None or not Path(ranking_path).is_file():
        logger.warning(f"[{_LABEL}] no 1M ranking file to read; skipping")
        return []
    top_etfs = int(cfg.get("top_etfs", DEFAULT_TOP_ETFS))
    entries = parse_ranking(Path(ranking_path).read_text(encoding="utf-8"))
    cands = candidates(entries, top_etfs)
    tv_dir = output_dir / "TV" / "US"
    master_path = tv_dir / MASTER_NAME
    master = load_master(master_path)
    known = set(master)
    new = [s for s in cands if s not in known]
    logger.info(f"[{_LABEL}] top {min(top_etfs, len(entries))} ETFs -> {len(cands)} candidates, "
                f"master {len(master)}, new {len(new)}: {new}")
    if not new:
        return []
    tv_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(master_path, master + new)
    new_path = tv_dir / f"{today:%Y_%m_%d}_{NEW_STEM}.txt"
    if new_path.exists():
        logger.info(f"[{_LABEL}] {new_path.name} already written today; keeping the earlier "
                    f"run's file (master grown by {len(new)})")
    else:
        _write_csv(new_path, new)
        logger.info(f"[{_LABEL}] {len(new)} new -> {new_path}")
    return new
