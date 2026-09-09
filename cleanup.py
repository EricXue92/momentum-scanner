"""Retention cleanup for dated output artifacts.

Deletes dated files older than the per-rule retention window. Driven by an
explicit table of (directory, filename regex, date format, keep_days)
tuples so unrecognised filenames are never touched.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Rule:
    subdir: str           # relative to output_dir; "" = output_dir itself
    pattern: re.Pattern   # must contain one group capturing the date
    date_fmt: str         # strptime format for the captured group
    keep_days: int        # 2 = today + yesterday; 4 = today + 3 prior


# YYYY_MM_DD is the project-wide convention; hk_rs_rating uses YYYY-MM-DD.
_DATE_U = r"(\d{4}_\d{2}_\d{2})"   # underscores
_DATE_D = r"(\d{4}-\d{2}-\d{2})"   # dashes

_RETENTION_RULES: tuple[_Rule, ...] = (
    # Dated scan outputs — 5-day retention (today + 4 prior days).
    _Rule("TV/US",     re.compile(rf"^{_DATE_U}_.+\.txt$"),  "%Y_%m_%d", 5),
    _Rule("TV/HK",     re.compile(rf"^{_DATE_U}_.+\.txt$"),  "%Y_%m_%d", 5),
    _Rule("Webull/US", re.compile(rf"^{_DATE_U}_.+\.txt$"),  "%Y_%m_%d", 5),
    _Rule("Webull/HK", re.compile(rf"^{_DATE_U}_.+\.txt$"),  "%Y_%m_%d", 5),
    # CANSLIM reports (report/) and the pre-market catalyst report
    # (<date>_us_premarket.*) — 7-day retention (today + 6 prior days).
    _Rule("Reports",   re.compile(rf"^{_DATE_U}_(us|hk|us_premarket)\.(md|html)$"),
          "%Y_%m_%d", 7),
    # Per-day state caches — 2-day retention.
    _Rule("state", re.compile(rf"^morning_gap_seen_(?:pre|post)_{_DATE_U}\.txt$"),
          "%Y_%m_%d", 2),
    _Rule("state", re.compile(rf"^hk_morning_gap_seen_post_{_DATE_U}\.txt$"),
          "%Y_%m_%d", 2),
    # 3M variant first — its filename starts with hk_rs_rating_3m_, which
    # would otherwise be a no-match against the 12M regex below (the 12M
    # regex anchors on hk_rs_rating_<date>.csv with no '3m_' segment).
    _Rule("state", re.compile(rf"^hk_rs_rating_3m_{_DATE_D}\.csv$"),
          "%Y-%m-%d", 2),
    _Rule("state", re.compile(rf"^hk_rs_rating_{_DATE_D}\.csv$"),
          "%Y-%m-%d", 2),
    # hk_metrics_*.csv: HK long-side metrics state cache (hk_metrics.py, ISO
    # dashes). 2-day window — only today's cache is ever read (no walk-back).
    _Rule("state", re.compile(rf"^hk_metrics_{_DATE_D}\.csv$"),
          "%Y-%m-%d", 2),
    # rs_rating_*.csv: 4-day window preserves the documented 3-day GitHub
    # fetch fallback in rs_rating.py (_FALLBACK_MAX_AGE_DAYS = 3).
    _Rule("state", re.compile(rf"^rs_rating_{_DATE_U}\.csv$"),
          "%Y_%m_%d", 4),
    # rs_rating_3m_*.csv: US 3M RS cache (us_rs_3m.py, date uses ISO dashes).
    # Same 4-day window as the 12M counterpart.
    _Rule("state", re.compile(rf"^rs_rating_3m_{_DATE_D}\.csv$"),
          "%Y-%m-%d", 4),
    # Daily strongest-RS snapshots written by the (now scheduled) rs-line
    # audit — 4-day window (today + 3 prior). US lives in TV/US
    # (TradingView-importable); HK stays at the output root. The root rs_us
    # pattern is kept so pre-move leftovers still age out.
    _Rule("TV/US", re.compile(rf"^rs_us_{_DATE_D}\.txt$"),
          "%Y-%m-%d", 4),
    _Rule("", re.compile(rf"^(?:rs_us|hk_rs)_{_DATE_D}\.txt$"),
          "%Y-%m-%d", 4),
    # Audit report + sidecars accumulate daily now that the audit is on the
    # EOD schedule. Same 4-day window as the snapshots above.
    _Rule("", re.compile(rf"^rs_line_audit_(?:US|HK)_{_DATE_D}(?:_drop|_keep_ranked)?\.txt$"),
          "%Y-%m-%d", 4),
)


def cleanup_old_outputs(output_dir: Path, today: date) -> None:
    """Delete dated artifacts under output_dir older than each rule's window.

    Soft-fails: per-file IO errors are caught and logged; the function
    never raises. Files whose names don't match any rule are not touched.
    """
    total_deleted = 0
    for rule in _RETENTION_RULES:
        total_deleted += _clean_one_rule(output_dir, rule, today)
    if total_deleted:
        logger.info(f"[cleanup] Removed {total_deleted} stale output file(s)")


def _clean_one_rule(output_dir: Path, rule: _Rule, today: date) -> int:
    target_dir = output_dir / rule.subdir if rule.subdir else output_dir
    if not target_dir.is_dir():
        return 0
    cutoff = today - timedelta(days=rule.keep_days - 1)
    deleted = 0
    for entry in target_dir.iterdir():
        if not entry.is_file():
            continue
        m = rule.pattern.match(entry.name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), rule.date_fmt).date()
        except ValueError:
            logger.warning(f"[cleanup] Skipping malformed date in {entry.name}")
            continue
        if file_date >= cutoff:
            continue
        try:
            entry.unlink()
            deleted += 1
        except FileNotFoundError:
            pass  # raced with another process; fine
        except OSError as e:
            logger.warning(f"[cleanup] Failed to delete {entry}: {e}")
    return deleted
