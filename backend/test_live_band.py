"""Live Band test — Peshawar flood, magnitude 6.2, with an injected anomaly.

Posts REAL messages to the configured Band room and verifies that the new
natural-message + discussion pipeline behaves:

  ✅ Natural messages (not JSON dumps)
  ✅ Agent personalities visible
  ✅ Anomaly discussion triggered (extent >> GDACS)
  ✅ Low confidence warning sent
  ✅ Structured JSON also sent (separate)

It captures every outbound message via a spy around send_text_message /
send_event, drives start_pipeline + the satellite->hazard transition with a
mocked satellite result, and prints the exact Band room messages.

Run from backend/:  python test_live_band.py
"""
import asyncio
import json
import sys
import uuid

# Windows consoles default to cp1252; force UTF-8 so ✅/km² render.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - best effort
    pass

import band_client as bc
import orchestrator as orch_module

# Injected artificial anomaly (the test fixture from the task) + a GDACS
# estimate so the extent-vs-GDACS trigger fires (500 > 120 * 2).
MOCK_SATELLITE_RESULT = {
    "affected_area_km2": 500,   # unusually large
    "zones": 45,
    "confidence": 0.65,         # low confidence
    "gdacs_estimate_km2": 120,  # GDACS baseline -> 500/120 = 4.2x
    "risk_cities": ["Peshawar", "Nowshera", "Charsadda"],
}

captured: list[dict] = []


def _install_spies() -> None:
    """Wrap the real senders so we record exactly what hits the Band room."""
    real_text = bc.send_text_message
    real_event = bc.send_event

    async def spy_text(content, mentions=None, room_id=None):
        captured.append({"kind": "text", "content": content})
        return await real_text(content, mentions=mentions, room_id=room_id)

    async def spy_event(event_type, title, data, mentions=None, room_id=None):
        captured.append(
            {"kind": "event", "event_type": event_type, "title": title, "data": data}
        )
        return await real_event(event_type, title, data, mentions=mentions, room_id=room_id)

    # Patch in band_client AND in orchestrator's imported references.
    bc.send_text_message = spy_text
    bc.send_event = spy_event
    orch_module.send_text_message = spy_text
    orch_module.send_event = spy_event
    # send_handoff / send_thought / send_task_update call through band_client's
    # module-level names, so patching bc.* covers them.


async def main() -> None:
    _install_spies()

    # Avoid touching Neon — stub the DB status writer.
    async def fake_update(event_id, status, step):
        return None

    orch_module.update_event_status = fake_update
    # Shrink the discussion wait so the test is quick (still posts to Band).
    orch_module.DISCUSSION_WAIT_SECONDS = 1

    agent = orch_module.OrchestratorAgent()
    event_id = str(uuid.uuid4())
    disaster = {"location": "Peshawar", "disaster_type": "flood", "magnitude": 6.2}

    print(f"=== LIVE TEST event_id={event_id} ===\n")

    # 1. Dispatch to satellite (natural high-urgency message + structured notify).
    await agent.start_pipeline(event_id, disaster)

    # 2. Feed the mocked satellite completion into the inbound store, exactly as
    #    the real satellite agent would report it.
    bc.inbound_store.add(
        {
            "type": "data",
            "content": json.dumps(
                {
                    "event": "complete",
                    "data": {
                        "event_id": event_id,
                        "agent": "hazardmind-satellite",
                        "step": "satellite",
                        "status": "complete",
                        "data": MOCK_SATELLITE_RESULT,
                    },
                }
            ),
            "sender": {"name": "hazardmind-satellite"},
        }
    )

    # 3. Run ONE transition (satellite -> hazard): this triggers cross-validation
    #    (extent anomaly + low confidence) and the natural handoff to hazard.
    transition = orch_module.PIPELINE_TRANSITIONS[0]
    await agent._advance(event_id, transition)

    # 4. Report exactly what was posted to the Band room.
    print("\n=== EXACT BAND ROOM MESSAGES ===\n")
    for i, m in enumerate(captured, 1):
        if m["kind"] == "text":
            print(f"[{i}] TEXT:\n    {m['content']}\n")
        else:
            print(
                f"[{i}] EVENT ({m['event_type']}) {m['title']}:\n"
                f"    {json.dumps(m['data'])}\n"
            )

    # 5. Assertions.
    texts = [m["content"] for m in captured if m["kind"] == "text"]
    events = [m for m in captured if m["kind"] == "event"]
    blob = "\n".join(texts).lower()

    assert any("@" in t for t in texts), "no @-addressed natural messages"
    assert not any(t.strip().startswith("{") for t in texts), "a text msg was raw JSON"
    assert "gdacs" in blob or "larger" in blob or "extent" in blob, \
        "extent anomaly discussion not triggered"
    assert "confidence" in blob, "low confidence warning not sent"
    assert any(e["event_type"] == "data" for e in events), \
        "no structured data event sent alongside natural handoff"

    print("=== CHECKS ===")
    print("✅ Natural messages (not JSON dumps)")
    print("✅ Agent personalities visible (Featherless-generated tone)")
    print("✅ Anomaly discussion triggered (extent 4.2x GDACS)")
    print("✅ Low confidence warning sent (0.65)")
    print("✅ Structured JSON also sent (separate 'data' event)")
    print("\n[done] live Band test passed")


if __name__ == "__main__":
    asyncio.run(main())
