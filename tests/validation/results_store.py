"""Versioned JSON results storage, keyed by git commit.

Every harness run writes to `tests/validation/results/<short_sha>.json` and
never overwrites a prior commit's file, so any two runs are directly
comparable by diffing two files named after the commits that produced them.
A run against a dirty working tree is stamped `<short_sha>-dirty` so it can
never be silently confused with a clean, reproducible baseline.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _run_git(args: list[str]) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=Path(__file__).resolve().parents[2], text=True
    ).strip()


def current_commit_tag() -> str:
    """Short SHA of HEAD, suffixed `-dirty` if the working tree has
    uncommitted changes (staged or unstaged) at the time the harness ran."""
    short_sha = _run_git(["rev-parse", "--short", "HEAD"])
    status = _run_git(["status", "--porcelain"])
    return f"{short_sha}-dirty" if status else short_sha


def current_commit_full() -> str:
    return _run_git(["rev-parse", "HEAD"])


@dataclass
class EventResult:
    event_id: str
    location: str
    satellite_type: Optional[str]
    satellite_type_reason: Optional[str]
    coverage_tier: Optional[int]
    temporal_spread_days: Optional[int]
    reported_confidence: Optional[float]
    confidence_basis: Optional[str]
    metrics_including_permanent_water: Optional[dict]
    metrics_excluding_permanent_water: Optional[dict]
    permanent_water_note: str
    pipeline_status: str
    pipeline_error: Optional[str] = None
    harness_notes: list[str] = field(default_factory=list)
    elapsed_seconds: Optional[float] = None
    bytes_downloaded: Optional[int] = None


@dataclass
class RunReport:
    commit: str
    commit_full: str
    run_timestamp_utc: str
    events: list[EventResult]

    def as_dict(self) -> dict:
        return {
            "commit": self.commit,
            "commit_full": self.commit_full,
            "run_timestamp_utc": self.run_timestamp_utc,
            "events": [asdict(e) for e in self.events],
        }


def write_run_report(events: list[EventResult]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = current_commit_tag()
    report = RunReport(
        commit=tag,
        commit_full=current_commit_full(),
        run_timestamp_utc=datetime.now(timezone.utc).isoformat(),
        events=events,
    )
    out_path = RESULTS_DIR / f"{tag}.json"
    if out_path.exists():
        # Never silently overwrite a prior run against the same commit —
        # append a numeric suffix instead so both are kept for comparison.
        n = 2
        while (RESULTS_DIR / f"{tag}.run{n}.json").exists():
            n += 1
        out_path = RESULTS_DIR / f"{tag}.run{n}.json"
    out_path.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    return out_path


def print_summary_table(events: list[EventResult]) -> None:
    header = (
        f"{'Event':<28}{'Sat':<12}{'Tier':<6}{'IoU(incl)':<11}"
        f"{'F1(incl)':<10}{'IoU(excl)':<11}{'Conf':<7}{'Basis':<20}"
        f"{'Sec':<8}{'MB':<8}{'Status'}"
    )
    print(header)
    print("-" * len(header))
    for e in events:
        incl = e.metrics_including_permanent_water or {}
        excl = e.metrics_excluding_permanent_water or {}
        mb = f"{e.bytes_downloaded / 1e6:.0f}" if e.bytes_downloaded else "-"
        sec = f"{e.elapsed_seconds:.0f}" if e.elapsed_seconds is not None else "-"
        print(
            f"{e.location[:27]:<28}"
            f"{(e.satellite_type or '-'):<12}"
            f"{(str(e.coverage_tier) if e.coverage_tier is not None else '-'):<6}"
            f"{incl.get('iou', '-'):<11}"
            f"{incl.get('f1', '-'):<10}"
            f"{excl.get('iou', 'n/a'):<11}"
            f"{(e.reported_confidence if e.reported_confidence is not None else '-'):<7}"
            f"{(e.confidence_basis or '-'):<20}"
            f"{sec:<8}"
            f"{mb:<8}"
            f"{e.pipeline_status}"
        )
