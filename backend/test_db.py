"""Connectivity + smoke test for backend/db.py against Neon.

Run from the backend/ directory:  python test_db.py

Verifies the asyncpg pool connects (SSL) and that all four functions work:
  create_disaster_event, update_event_status,
  get_event_status, get_event_results.

Uses a throwaway event_id and deletes it at the end.
"""
import asyncio
import json
import uuid

import db


async def main() -> None:
    event_id = str(uuid.uuid4())
    print(f"[test] using event_id={event_id}")

    pool = await db.get_pool()
    print("[ok] pool created (SSL required)")

    # 1. create_disaster_event -> status should be 'received'
    await db.create_disaster_event(
        event_id=event_id,
        location="Karachi, Pakistan",
        disaster_type="flood",
        magnitude=3.2,
    )
    print("[ok] create_disaster_event")

    status = await db.get_event_status(event_id)
    assert status is not None, "event not found after insert"
    assert status["status"] == "received", status
    assert status["step"] == "received", status
    print(f"[ok] get_event_status -> status={status['status']} step={status['step']}")

    # 2. update_event_status -> moves to satellite/processing
    await db.update_event_status(event_id, status="processing", step="satellite")
    status = await db.get_event_status(event_id)
    assert status["status"] == "processing", status
    assert status["step"] == "satellite", status
    assert status["updated_at"] >= status["created_at"], status
    print(f"[ok] update_event_status -> status={status['status']} step={status['step']}")

    # 3. get_event_results -> joins all 5 tables (children null until agents write)
    results = await db.get_event_results(event_id)
    assert results is not None, "results not found"
    assert results["event_id"] == uuid.UUID(event_id), results
    for key in ("satellite", "hazard", "impact", "report"):
        assert key in results, f"missing {key} in results"
    print("[ok] get_event_results -> keys:", sorted(results.keys()))
    print(json.dumps(results, indent=2, default=str))

    # cleanup
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM disaster_events WHERE event_id = $1", event_id
        )
    print("[ok] cleanup")

    await db.close_pool()
    print("[done] all 4 functions verified")


if __name__ == "__main__":
    asyncio.run(main())
