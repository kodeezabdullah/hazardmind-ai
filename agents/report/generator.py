import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI


BASE_DIR = Path(__file__).resolve().parent
MODEL_NAME = "claude-opus-4-8"

MOCK_EVENT_DATA = {
    "event_id": "demo-dhaka-flood",
    "location": "Dhaka, Bangladesh",
    "hazard_type": "Flood",
    "overall_severity": "CRITICAL",
    "flood_risk": "CRITICAL",
    "earthquake_risk": "MEDIUM",
    "landslide_risk": "LOW",
    "population_affected": 540000,
    "hospitals_at_risk": 14,
    "roads_blocked_km": 89,
    "schools_affected": 67,
    "vulnerability_score": 8.2,
}

RECOMMENDATIONS = [
    "Prioritize evacuation and rescue operations in critical flood-risk zones.",
    "Deploy emergency medical support near the 14 hospitals currently at risk.",
    "Clear blocked road corridors needed for relief movement and hospital access.",
    "Open temporary shelters and continuity plans for affected schools and families.",
]


def build_prompt(data: dict) -> str:
    return f"""
Create a concise executive disaster response summary for decision makers.
Return only the summary text in 3 to 5 sentences, with no markdown.

Event ID: {data["event_id"]}
Location: {data["location"]}
Hazard type: {data["hazard_type"]}
Overall severity: {data["overall_severity"]}
Flood risk: {data["flood_risk"]}
Earthquake risk: {data["earthquake_risk"]}
Landslide risk: {data["landslide_risk"]}
Population affected: {data["population_affected"]}
Hospitals at risk: {data["hospitals_at_risk"]}
Roads blocked: {data["roads_blocked_km"]} km
Schools affected: {data["schools_affected"]}
Vulnerability score: {data["vulnerability_score"]}/10
""".strip()


async def generate_report(event_id: str):
    if event_id != MOCK_EVENT_DATA["event_id"]:
        raise ValueError(
            f"Only local mock event '{MOCK_EVENT_DATA['event_id']}' is available right now."
        )

    load_dotenv(BASE_DIR / ".env")

    api_key = os.getenv("AIML_API_KEY")
    if not api_key:
        raise RuntimeError("AIML_API_KEY is not set")

    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.aimlapi.com/v1",
    )

    try:
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": "You write clear, fast executive disaster response reports.",
                },
                {
                    "role": "user",
                    "content": build_prompt(MOCK_EVENT_DATA),
                },
            ],
        )
    finally:
        await client.close()

    summary = response.choices[0].message.content.strip()

    return {
        "event_id": event_id,
        "map_url": "",
        "pdf_url": "",
        "summary": summary,
        "statistics": {
            "location": MOCK_EVENT_DATA["location"],
            "hazard_type": MOCK_EVENT_DATA["hazard_type"],
            "overall_severity": MOCK_EVENT_DATA["overall_severity"],
            "risk_levels": {
                "flood": MOCK_EVENT_DATA["flood_risk"],
                "earthquake": MOCK_EVENT_DATA["earthquake_risk"],
                "landslide": MOCK_EVENT_DATA["landslide_risk"],
            },
            "population_affected": MOCK_EVENT_DATA["population_affected"],
            "hospitals_at_risk": MOCK_EVENT_DATA["hospitals_at_risk"],
            "roads_blocked_km": MOCK_EVENT_DATA["roads_blocked_km"],
            "schools_affected": MOCK_EVENT_DATA["schools_affected"],
            "vulnerability_score": MOCK_EVENT_DATA["vulnerability_score"],
        },
        "recommendations": RECOMMENDATIONS,
    }
