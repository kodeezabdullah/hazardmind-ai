"""Read satellite data written by the Satellite Agent from Cloudflare R2 (public bucket)."""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

R2_BASE_URL = os.environ.get(
    "R2_PUBLIC_BASE_URL",
    "https://pub-720f47eaad2f4997a76a02f8bf14f58a.r2.dev",
)


async def fetch_zones_geojson(event_id: str) -> dict:
    """
    GET {R2_BASE_URL}/events/{event_id}/zones.geojson
    Falls back to events/demo-dhaka/ if the event path is not found.
    Returns a GeoJSON FeatureCollection dict.
    """
    primary_url = f"{R2_BASE_URL}/events/{event_id}/zones.geojson"
    fallback_url = f"{R2_BASE_URL}/events/demo-dhaka/zones.geojson"

    for url, label in [(primary_url, event_id), (fallback_url, "demo-dhaka")]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                logger.info("[r2] zones.geojson fetched from %s (event_id=%s)", url, label)
                return data
        except Exception as exc:
            logger.warning("[r2] Could not fetch zones.geojson from %s: %s", url, exc)

    logger.error("[r2] All R2 attempts failed for event_id=%s — returning empty FeatureCollection", event_id)
    return {"type": "FeatureCollection", "features": []}


def get_satellite_urls(event_id: str) -> dict:
    """
    Return public R2 URLs for the three satellite images produced by the Satellite Agent.
    No download needed — these URLs are passed to Band for Agent 4 (Zohair) to display.
    """
    base = f"{R2_BASE_URL}/events/{event_id}"
    return {
        "true_color": f"{base}/true_color.png",
        "index_map": f"{base}/index_map.png",
        "classification": f"{base}/classification.png",
    }
