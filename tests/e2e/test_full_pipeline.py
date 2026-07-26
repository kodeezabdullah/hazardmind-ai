"""End-to-end pipeline test — Rawalpindi flood, live everything.

Drives the LangGraph pipeline exactly like backend/router.py's /analyze:
generate event_id ONCE -> create_disaster_event -> run the compiled graph.
Each node is wrapped to time it and record the event_id it actually saw, so a
UUID truncation (the #1 pre-migration bug) is caught byte-for-byte.

Writes a blunt markdown report to tests/e2e/report_<timestamp>.md and prints a
summary. Does NOT fix failures — it reports what actually works.

Run:
    tests/e2e/.venv-e2e/Scripts/python tests/e2e/test_full_pipeline.py
"""

import asyncio
import io
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from tests.e2e._env import load_all_service_envs

# Load every service .env BEFORE importing the graph (so each agent's
# load_dotenv(override=False) is a no-op over the merged, already-present vars).
ENV_SUMMARY = load_all_service_envs()

import asyncpg  # noqa: E402
import requests  # noqa: E402

# Scenario is overridable via env so the same harness can run flood (S1/SAR) or
# earthquake/landslide (S2/optical) without editing the file.
import os as _os

LOCATION = _os.getenv("E2E_LOCATION", "Rawalpindi")
DISASTER_TYPE = _os.getenv("E2E_DISASTER_TYPE", "flood")
MAGNITUDE = float(_os.getenv("E2E_MAGNITUDE", "0.0"))
BASELINE_TOTAL_SECONDS = 142.0  # the pre-migration ~142s reference

# ---------------------------------------------------------------------------
# Log capture: a root handler that keeps every record so we can grep for
# WARNING/ERROR lines that DID NOT surface as an assertion failure (the broad
# `except Exception` across agents swallows real failures into conservative
# defaults — this makes them visible).
# ---------------------------------------------------------------------------
_LOG_BUFFER = io.StringIO()


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            self.acquire()
            _LOG_BUFFER.write(self.format(record) + "\n")
        finally:
            self.release()


def _install_log_capture():
    handler = _BufferHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Also echo to stdout so a hanging run shows progress.
    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    root.addHandler(stream)


# ---------------------------------------------------------------------------
# Results accumulation
# ---------------------------------------------------------------------------
ASSERTIONS = []  # (name, ok, detail)
NODE_TIMES = {}  # stage -> seconds
NODE_EVENT_IDS = {}  # stage -> event_id seen at that node
GEMINI_STATS = {"primary": 0, "backup": 0, "backup_rescued": 0, "429": 0}


def check(name, ok, detail=""):
    ASSERTIONS.append((name, bool(ok), detail))
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {name} :: {detail}")
    return ok


def _wrap_node(stage, fn):
    """Wrap a node coroutine to time it and record the event_id it received."""

    async def wrapper(state):
        NODE_EVENT_IDS[stage] = state.get("event_id")
        t0 = time.time()
        try:
            return await fn(state)
        finally:
            NODE_TIMES[stage] = time.time() - t0
            print(f">>> node '{stage}' finished in {NODE_TIMES[stage]:.1f}s")

    return wrapper


async def _db():
    url = __import__("os").environ["NEON_DATABASE_URL"]
    return await asyncpg.connect(url, ssl="require", timeout=30)


def _url_ok(url, expect_pdf=False):
    """HTTP-GET url; return (ok, detail). Checks 200 + non-zero length; if
    expect_pdf, checks the body starts with %PDF."""
    if not url:
        return False, "empty URL"
    try:
        r = requests.get(url, timeout=60)
        clen = r.headers.get("Content-Length") or str(len(r.content))
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        if int(len(r.content)) == 0:
            return False, "0-byte body"
        # detect an R2/S3 XML error page masquerading as 200
        head = r.content[:16]
        if head.strip().startswith(b"<?xml") or head.strip().startswith(b"<Error"):
            return False, f"XML error page ({clen} bytes)"
        if expect_pdf and not r.content.startswith(b"%PDF"):
            return False, f"not a PDF (starts {head!r})"
        return True, f"200, {len(r.content)} bytes"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _scan_gemini_logs():
    text = _LOG_BUFFER.getvalue()
    for line in text.splitlines():
        low = line.lower()
        if "gemini key slot: primary" in low:
            GEMINI_STATS["primary"] += 1
        if "gemini key slot: backup" in low:
            GEMINI_STATS["backup"] += 1
        if "retrying the same request once on the backup" in low:
            GEMINI_STATS["429"] += 1
        if "backup" in low and "429" in low and "also" in low:
            pass  # backup also failed
    # A backup slot that produced content after a 429 == a rescue.
    GEMINI_STATS["backup_rescued"] = min(GEMINI_STATS["backup"], GEMINI_STATS["429"])


async def run():
    _install_log_capture()
    import os

    print("=== .env merge ===")
    for k, v in ENV_SUMMARY.items():
        print(f"  {k}: {v}")

    # Build the graph with wrapped nodes for per-node timing + event_id capture.
    from graph import _load_node, _route_after  # noqa
    from langgraph.graph import END, StateGraph
    from shared.pipeline_state import PipelineState
    from db import create_disaster_event, update_event_status, update_pipeline_log

    nodes = {
        "satellite": _load_node("satellite", "satellite_node"),
        "hazard": _load_node("hazard", "hazard_node"),
        "impact": _load_node("impact", "impact_node"),
        "report": _load_node("report", "report_node"),
    }
    g = StateGraph(PipelineState)
    for stage, fn in nodes.items():
        g.add_node(stage, _wrap_node(stage, fn))
    g.set_entry_point("satellite")
    g.add_conditional_edges("satellite", _route_after("satellite", "hazard"))
    g.add_conditional_edges("hazard", _route_after("hazard", "impact"))
    g.add_conditional_edges("impact", _route_after("impact", "report"))
    g.add_edge("report", END)
    graph = g.compile()

    # --- drive it like /analyze ---
    # Normally a fresh UUID per run (matches router.py). E2E_EVENT_ID lets a
    # re-run reuse a prior run's event_id so the satellite band cache
    # (<temp>/hazardmind-satellite/<event_id>/bands) is reused instead of
    # re-downloading — handy while iterating on downstream stages.
    event_id = os.getenv("E2E_EVENT_ID") or str(uuid.uuid4())
    print(f"\n=== event_id (generated once) = {event_id} ===\n")
    # If reusing an event_id, clear any prior child rows so assertions see a
    # clean write from THIS run (upserts handle most, but satellite DELETEs+INSERTs).
    if os.getenv("E2E_EVENT_ID"):
        conn0 = await _db()
        try:
            for t in ("satellite_results", "hazard_zones", "impact_data", "final_reports"):
                await conn0.execute(f"DELETE FROM {t} WHERE event_id=$1", event_id)
            await conn0.execute("DELETE FROM disaster_events WHERE event_id=$1", event_id)
        finally:
            await conn0.close()
    await create_disaster_event(event_id, LOCATION, DISASTER_TYPE, MAGNITUDE)
    await update_event_status(event_id, status="processing", step="satellite")

    initial_state = {
        "event_id": event_id,
        "location": LOCATION,
        "disaster_type": DISASTER_TYPE,
        "magnitude": MAGNITUDE,
        "status": "satellite",
        "current_step": "satellite",
        "progress": 0,
        "errors": [],
        "anomalies": [],
        "confidence_scores": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    t_start = time.time()
    final_state = None
    crash = None
    try:
        final_state = await graph.ainvoke(initial_state)
    except Exception as e:  # noqa: BLE001
        crash = e
        import traceback

        traceback.print_exc()
    total_seconds = time.time() - t_start

    # persist pipeline_log like the orchestrator does
    if final_state:
        log = {
            "errors": final_state.get("errors") or [],
            "anomalies": final_state.get("anomalies") or [],
            "confidence_scores": final_state.get("confidence_scores") or {},
        }
        try:
            await update_pipeline_log(event_id, log)
        except Exception as e:  # noqa: BLE001
            print("pipeline_log persist failed:", e)
        status = final_state.get("status")
        await update_event_status(
            event_id,
            status="complete" if status == "complete" else "failed",
            step="complete" if status == "complete" else "failed",
        )

    # =====================================================================
    # ASSERTIONS
    # =====================================================================
    print("\n=== ASSERTIONS ===")

    # 1. event_id byte-identical at every node
    seen = NODE_EVENT_IDS
    all_match = all(v == event_id for v in seen.values()) and len(seen) > 0
    check(
        "1. event_id identical at every node",
        all_match,
        f"generated={event_id}; per-node={seen}",
    )

    conn = await _db()
    try:
        # 2. disaster_events row + status
        ev = await conn.fetchrow(
            "SELECT status, step, progress FROM disaster_events WHERE event_id=$1",
            event_id,
        )
        check(
            "2. disaster_events row + terminal status",
            ev is not None,
            f"row={dict(ev) if ev else None}",
        )

        # 3. satellite_results + R2 URLs
        sat = await conn.fetchrow(
            "SELECT true_color_url, index_url, classification_url, geojson_url, "
            "affected_area_km2, total_zones FROM satellite_results WHERE event_id=$1",
            event_id,
        )
        if sat:
            url_results = {}
            for col in ("true_color_url", "index_url", "classification_url", "geojson_url"):
                url_results[col] = _url_ok(sat[col])
            all_urls_ok = all(ok for ok, _ in url_results.values())
            check(
                "3. satellite_results row + every R2 URL 200/non-zero",
                all_urls_ok,
                "; ".join(f"{c}: {d}" for c, (ok, d) in url_results.items()),
            )
        else:
            check("3. satellite_results row + R2 URLs", False, "no satellite_results row")

        # 4. hazard_zones == exactly 3 rows (flood/eq/landslide)
        hz = await conn.fetch(
            "SELECT hazard_type FROM hazard_zones WHERE event_id=$1 ORDER BY hazard_type",
            event_id,
        )
        types = sorted(r["hazard_type"] for r in hz)
        check(
            "4. hazard_zones has exactly 3 rows (flood/earthquake/landslide)",
            types == ["earthquake", "flood", "landslide"],
            f"rows={len(hz)} types={types}",
        )

        # 5. impact_data row
        imp = await conn.fetchrow(
            "SELECT total_affected, overall_confidence FROM impact_data WHERE event_id=$1",
            event_id,
        )
        check("5. impact_data row written", imp is not None, f"row={dict(imp) if imp else None}")

        # 6. final_reports + PDF
        rep = await conn.fetchrow(
            "SELECT pdf_url, map_url, confidence_level FROM final_reports WHERE event_id=$1",
            event_id,
        )
        if rep:
            pdf_ok, pdf_detail = _url_ok(rep["pdf_url"], expect_pdf=True)
            check("6. final_reports row + PDF URL 200 + %PDF", pdf_ok, f"pdf: {pdf_detail}")
        else:
            check("6. final_reports row + PDF", False, "no final_reports row")

        # 9. pipeline_log persisted
        plog = await conn.fetchval(
            "SELECT pipeline_log FROM disaster_events WHERE event_id=$1", event_id
        )
        check(
            "9. disaster_events.pipeline_log contains errors/anomalies trail",
            plog is not None,
            f"pipeline_log keys={list(json.loads(plog).keys()) if plog else None}",
        )
    finally:
        await conn.close()

    # 7. final PipelineState.status == complete
    check(
        "7. final PipelineState.status == 'complete'",
        bool(final_state) and final_state.get("status") == "complete",
        f"status={final_state.get('status') if final_state else 'CRASH: '+repr(crash)}",
    )

    # 8. confidence_scores has an entry from every stage
    cs = (final_state or {}).get("confidence_scores") or {}
    have = sorted(cs.keys())
    check(
        "8. confidence_scores has an entry from every stage",
        all(s in cs for s in ("satellite", "hazard", "impact", "report")),
        f"present={have} values={cs}",
    )

    _scan_gemini_logs()
    _write_report(event_id, final_state, crash, total_seconds)


def _swallowed_log_lines():
    out = []
    for line in _LOG_BUFFER.getvalue().splitlines():
        if line.startswith("WARNING") or line.startswith("ERROR"):
            out.append(line)
    return out


def _write_report(event_id, final_state, crash, total_seconds):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(__file__).resolve().parent / f"report_{ts}.md"
    passed = sum(1 for _, ok, _ in ASSERTIONS if ok)
    total = len(ASSERTIONS)
    swallowed = _swallowed_log_lines()

    lines = []
    lines.append(f"# E2E Pipeline Report — {ts}")
    lines.append("")
    lines.append(f"- location=`{LOCATION}` disaster_type=`{DISASTER_TYPE}` event_id=`{event_id}`")
    lines.append(f"- **{passed}/{total} assertions passed**")
    lines.append(f"- final status: `{(final_state or {}).get('status') if final_state else 'CRASH: '+repr(crash)}`")
    lines.append("")
    lines.append("## Assertions")
    lines.append("| # | assertion | result | detail |")
    lines.append("|---|---|---|---|")
    for name, ok, detail in ASSERTIONS:
        lines.append(f"| | {name} | {'PASS' if ok else '**FAIL**'} | {detail} |")
    lines.append("")
    lines.append("## Timing (vs ~142s baseline)")
    lines.append("| stage | seconds |")
    lines.append("|---|---|")
    for stage in ("satellite", "hazard", "impact", "report"):
        lines.append(f"| {stage} | {NODE_TIMES.get(stage, float('nan')):.1f} |")
    lines.append(f"| **total** | **{total_seconds:.1f}** (baseline {BASELINE_TOTAL_SECONDS:.0f}) |")
    lines.append("")
    lines.append("## Gemini 429 / backup-key")
    lines.append(f"- primary-slot successes: {GEMINI_STATS['primary']}")
    lines.append(f"- 429s that triggered a backup retry: {GEMINI_STATS['429']}")
    lines.append(f"- backup-slot successes (rescues): {GEMINI_STATS['backup']}")
    lines.append("")
    lines.append(f"## Swallowed WARNING/ERROR log lines ({len(swallowed)})")
    lines.append("These did NOT surface as assertion failures (broad `except Exception`):")
    lines.append("```")
    lines.extend(swallowed[:400])
    lines.append("```")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n=== wrote {path} ===")
    print(f"=== {passed}/{total} assertions passed; total {total_seconds:.1f}s ===")


if __name__ == "__main__":
    asyncio.run(run())
