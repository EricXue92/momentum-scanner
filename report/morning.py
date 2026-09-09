"""Pre-market catalyst report — CLI entrypoint.

Triggered by main.py morning-gap path as a detached subprocess. Reads a
snapshot JSON sidecar (per-ticker gap%, price, market cap, first-seen
offset), fans out DeepSeek + Tavily catalyst analysis per ticker, appends
to output/Reports/PreMarket/<date>_us_premarket.html (the markdown
accumulator that feeds it lives in output/state/), then pushes ntfy."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import anthropic

from notify import notify_morning_catalyst_ready
from report.llm import DeepSeekBackend, LLMBackend
from report.state import CONFIG_PATH, OUTPUT_STATE_DIR, PREMARKET_DIR, load_dotenv

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotEntry:
    """One pre-market gapper as captured at scan time. All numeric fields
    are optional because main.py builds these from Futu snapshots that
    occasionally have null cells."""

    ticker: str
    company_name: str | None = None
    gap_pct: float | None = None
    last_price: float | None = None
    market_cap: float | None = None
    first_seen_offset_minutes: int | None = None


def write_snapshot(path: Path, entries: list[SnapshotEntry]) -> None:
    payload = [asdict(e) for e in entries]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_snapshot(path: Path) -> list[SnapshotEntry]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        SnapshotEntry(
            ticker=row["ticker"],
            company_name=row.get("company_name"),
            gap_pct=row.get("gap_pct"),
            last_price=row.get("last_price"),
            market_cap=row.get("market_cap"),
            first_seen_offset_minutes=row.get("first_seen_offset_minutes"),
        )
        for row in raw
    ]


RETRY_BACKOFF_SECONDS = 5.0
PER_CALL_TIMEOUT_SECONDS = 180.0

CATALYST_FAILURE_PLACEHOLDER = (
    "### 主催化剂\n\n[分析失败: {exc_type}]\n\n"
    "### 证据\n\n—\n\n"
    "### 分类\n\n**其他**\n\n"
    "### 提示\n\n—\n"
)


def _fmt(value: Any, *, default: str = "?") -> str:
    return default if value is None else str(value)


def build_user_message(entry: SnapshotEntry) -> str:
    """Serialize one snapshot entry into the user message for DeepSeek.
    The structured snapshot is shown to the model for grounding but the
    system prompt forbids reprinting it."""
    payload = {
        "ticker": entry.ticker,
        "company_name": entry.company_name,
        "gap_pct": entry.gap_pct,
        "last_price": entry.last_price,
        "market_cap": entry.market_cap,
        "first_seen_offset_minutes": entry.first_seen_offset_minutes,
    }
    return (
        f"Ticker: {entry.ticker}\n\n"
        f"Pre-market snapshot (for grounding only — do NOT reprint):\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
        f"Identify the most likely catalyst behind today's pre-market gap. "
        f"Issue ≤3 web_search calls in English. Emit the 4 sections per the "
        f"system prompt."
    )


async def analyze_catalyst(
    backend: LLMBackend,
    system_prompt: str,
    entry: SnapshotEntry,
    semaphore: asyncio.Semaphore,
) -> str:
    """Single-ticker DeepSeek call with one retry. Returns the model's
    4-section markdown, or the catalyst-shaped placeholder on failure."""
    user_msg = build_user_message(entry)
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            async with semaphore:
                text = await asyncio.wait_for(
                    backend.analyze(system_prompt, user_msg),
                    timeout=PER_CALL_TIMEOUT_SECONDS,
                )
            if not text:
                raise RuntimeError("empty response")
            return text
        except anthropic.APIStatusError as e:
            status = getattr(e, "status_code", None)
            retriable = status is None or status >= 500 or status in (408, 429)
            if not retriable:
                logger.error(
                    f"[morning] {entry.ticker}: non-retriable HTTP {status}: {e}"
                )
                return CATALYST_FAILURE_PLACEHOLDER.format(
                    exc_type=f"HTTP{status}"
                )
            last_error = e
            logger.warning(
                f"[morning] {entry.ticker}: attempt {attempt}: HTTP {status}: {e}"
            )
        except (
            anthropic.APIConnectionError,
            asyncio.TimeoutError,
            RuntimeError,
        ) as e:
            last_error = e
            logger.warning(
                f"[morning] {entry.ticker}: attempt {attempt}: "
                f"{type(e).__name__}: {e}"
            )
        if attempt == 1:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
    return CATALYST_FAILURE_PLACEHOLDER.format(
        exc_type=type(last_error).__name__ if last_error else "Unknown"
    )


def write_report(
    *,
    out_path: Path,
    date_iso: str,
    offset_min: int,
    entries: list[SnapshotEntry],
    sections: list[str],
    model_label: str,
    generated_at: datetime,
    skipped_count: int,
    et_time_hhmm: str,
) -> None:
    """Create or append the day's catalyst report. Byte-level append, no
    parsing of prior content. Missing-parent dirs are created."""
    # Lazy import to break the circular dependency:
    # morning_renderer imports SnapshotEntry from morning, so a top-level
    # import here would deadlock when running `python -m report.morning`.
    from report.morning_renderer import (  # noqa: PLC0415
        render_append_section,
        render_initial_document,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        block = render_append_section(
            offset_min=offset_min,
            et_time_hhmm=et_time_hhmm,
            entries=entries,
            sections=sections,
        )
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(block)
    else:
        doc = render_initial_document(
            date_iso=date_iso,
            offset_min=offset_min,
            entries=entries,
            sections=sections,
            model_label=model_label,
            generated_at=generated_at,
            skipped_count=skipped_count,
        )
        out_path.write_text(doc, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "morning_gap_catalyst_system.md"

DEFAULT_MAX_TICKERS = 10
DEFAULT_CONCURRENCY = 3
DEFAULT_MAX_SEARCH_CALLS = 3
DEFAULT_MODEL = "deepseek-v4-pro"


def _load_catalyst_cfg() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        with CONFIG_PATH.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(f"[morning] failed to read {CONFIG_PATH}: {e}")
        return {}
    return data.get("morning_gap_catalyst") or {}


def _build_deepseek_backend(cfg: dict[str, Any]) -> DeepSeekBackend:
    """Construct the DeepSeek backend from env vars + the catalyst config
    block. Raises RuntimeError on missing keys — caller logs + soft-fails."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    tavily_key = os.environ.get("TAVILY_API_KEY")
    missing = [
        n for n, v in (("DEEPSEEK_API_KEY", api_key), ("TAVILY_API_KEY", tavily_key)) if not v
    ]
    if missing:
        raise RuntimeError(f"missing env var(s): {', '.join(missing)}")
    return DeepSeekBackend(
        api_key=api_key,  # type: ignore[arg-type]
        tavily_api_key=tavily_key,  # type: ignore[arg-type]
        model=cfg.get("deepseek_model", DEFAULT_MODEL),
        max_search_calls=int(cfg.get("max_search_calls", DEFAULT_MAX_SEARCH_CALLS)),
    )


def _cap_and_sort(entries: list[SnapshotEntry], cap: int) -> tuple[list[SnapshotEntry], int]:
    """Sort by gap% desc, drop entries beyond `cap`. Returns (kept, skipped_count)."""
    sorted_entries = sorted(
        entries, key=lambda e: (e.gap_pct or 0.0), reverse=True
    )
    kept = sorted_entries[:cap]
    skipped = max(0, len(sorted_entries) - cap)
    return kept, skipped


async def _run_async(
    *, snapshot_path: Path, date_iso: str, offset_min: int
) -> int:
    load_dotenv()
    cfg = _load_catalyst_cfg()
    if not cfg.get("enabled", True):
        logger.info("[morning] catalyst report disabled in config")
        snapshot_path.unlink(missing_ok=True)
        return 0

    try:
        entries = read_snapshot(snapshot_path)
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"[morning] failed to read snapshot {snapshot_path}: {e}")
        try:
            snapshot_path.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    if not entries:
        logger.info("[morning] empty snapshot, nothing to do")
        snapshot_path.unlink(missing_ok=True)
        return 0

    cap = int(cfg.get("max_tickers_per_run", DEFAULT_MAX_TICKERS))
    entries, skipped = _cap_and_sort(entries, cap)

    if not PROMPT_PATH.is_file():
        logger.error(f"[morning] system prompt missing at {PROMPT_PATH}")
        snapshot_path.unlink(missing_ok=True)
        return 0
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    try:
        backend = _build_deepseek_backend(cfg)
    except RuntimeError as e:
        logger.warning(f"[morning] backend init failed; skipping: {e}")
        snapshot_path.unlink(missing_ok=True)
        return 0

    concurrency = int(cfg.get("concurrency", DEFAULT_CONCURRENCY))
    semaphore = asyncio.Semaphore(concurrency)
    try:
        sections = await asyncio.gather(
            *(analyze_catalyst(backend, system_prompt, e, semaphore) for e in entries)
        )
    finally:
        await backend.aclose()

    date_stem = date_iso.replace("-", "_")
    # The markdown is the append-across-scans accumulator (byte-appended by
    # write_report); it is internal state, not a deliverable. The HTML under
    # Reports/PreMarket is re-rendered from the full accumulator every scan.
    md_state_path = OUTPUT_STATE_DIR / f"premarket_catalyst_{date_stem}.md"
    html_path = PREMARKET_DIR / f"{date_stem}_us_premarket.html"
    now_hkt = datetime.now(HKT)
    now_et = datetime.now(ET)
    write_report(
        out_path=md_state_path,
        date_iso=date_iso,
        offset_min=offset_min,
        entries=entries,
        sections=list(sections),
        model_label=backend.model_label(),
        generated_at=now_hkt,
        skipped_count=skipped,
        et_time_hhmm=now_et.strftime("%H:%M"),
    )
    logger.info(f"[morning] updated accumulator {md_state_path}")

    from report.renderer import markdown_to_html  # noqa: PLC0415
    md_text = md_state_path.read_text(encoding="utf-8")
    html_text = markdown_to_html(md_text, f"Pre-market Catalyst Report — {date_iso} (US)")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_text, encoding="utf-8")
    logger.info(f"[morning] wrote {html_path}")

    # Reload full config for [notify] section.
    try:
        with CONFIG_PATH.open("rb") as fh:
            full_cfg = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        full_cfg = {}
    notify_morning_catalyst_ready(
        report_path=html_path,
        offset_min=offset_min,
        n_tickers=len(entries),
        config=full_cfg,
    )
    snapshot_path.unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m report.morning")
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--offset", required=True, type=int,
                        help="Minutes from market open (e.g. -20)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        return asyncio.run(
            _run_async(
                snapshot_path=args.snapshot,
                date_iso=args.date,
                offset_min=args.offset,
            )
        )
    except Exception as e:
        logger.exception(f"[morning] aborted: {e}")
        try:
            args.snapshot.unlink(missing_ok=True)
        except OSError:
            pass
        return 0  # soft-fail


if __name__ == "__main__":
    sys.exit(main())
