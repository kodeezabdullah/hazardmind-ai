"""Migration verification tests (feat/durable-evidence-trail).

Applies every .sql file in shared/db/migrations/ against the LIVE Neon DB
(the only DB this project has -- see root CLAUDE.md's schema section) and
verifies:
  - every migration file applies without error, in order
  - running the whole set a second time is a no-op (idempotency) -- every
    statement uses IF NOT EXISTS
  - the columns the durable-evidence-trail migration (0002) added actually
    exist afterwards, on all three target tables

Requires NEON_DATABASE_URL (loaded from backend/.env) and asyncpg -- run
with D:\\hazardmind-ai\\.venv-e2e\\Scripts\\python.exe, which has asyncpg
installed and was used for the original live-schema introspection this
migration is based on.

Run: D:\\hazardmind-ai\\.venv-e2e\\Scripts\\python.exe shared/db/migrations/test_migrations.py
"""
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).resolve().parent
REPO_ROOT = MIGRATIONS_DIR.parent.parent.parent

load_dotenv(REPO_ROOT / "backend" / ".env")
DB_URL = os.getenv("NEON_DATABASE_URL")

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  PASS: {m}")


def bad(m):
    FAIL.append(m)
    print(f"  FAIL: {m}")


EXPECTED_NEW_COLUMNS = {
    "satellite_results": {
        "confidence", "confidence_basis", "evidence_count",
        "coverage_percent", "coverage_status", "scene_age_days",
        "index_calibrated", "index_units", "selection_reason",
        "scene_cloud_percent", "aoi_cloud_percent", "scl_reused",
        "diagnostics",
    },
    "hazard_zones": {"diagnostics"},
    "impact_data": {"diagnostics"},
}


async def _run():
    if not DB_URL:
        bad("NEON_DATABASE_URL not set (checked backend/.env) -- cannot run live migration tests")
        return

    conn = await asyncpg.connect(DB_URL)
    try:
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            bad("no .sql migration files found in shared/db/migrations/")
            return
        ok(f"found {len(files)} migration file(s): {[f.name for f in files]}")

        # First pass -- every file must apply without error, in order.
        for f in files:
            sql = f.read_text(encoding="utf-8")
            try:
                await conn.execute(sql)
                ok(f"{f.name} applied without error (pass 1)")
            except Exception as exc:  # noqa: BLE001
                bad(f"{f.name} FAILED to apply (pass 1): {exc}")
                return

        # Idempotency -- running the whole set again must be a no-op, no error.
        for f in files:
            sql = f.read_text(encoding="utf-8")
            try:
                await conn.execute(sql)
                ok(f"{f.name} re-applied without error (pass 2 -- idempotency)")
            except Exception as exc:  # noqa: BLE001
                bad(f"{f.name} is NOT idempotent -- failed on second run: {exc}")

        # Confirm the durable-evidence-trail columns actually exist now.
        for table, expected_cols in EXPECTED_NEW_COLUMNS.items():
            rows = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = $1
                """,
                table,
            )
            actual_cols = {r["column_name"] for r in rows}
            missing = expected_cols - actual_cols
            if not missing:
                ok(f"{table}: all {len(expected_cols)} durable-evidence-trail columns present")
            else:
                bad(f"{table}: missing columns after migration: {missing}")
    finally:
        await conn.close()


if __name__ == "__main__":
    print("=" * 70)
    print("TEST: shared/db/migrations idempotency + column presence (live Neon)")
    print("=" * 70)
    asyncio.run(_run())
    print("=" * 70)
    print(f"SUMMARY: PASS={len(PASS)} FAIL={len(FAIL)}")
    print("=" * 70)
    if FAIL:
        sys.exit(1)
