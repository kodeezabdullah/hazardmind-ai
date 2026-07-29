"""Phase 3b — name the permanent water bodies in the AOI.

**The problem this solves is not about pixels.** After Phase 3a the pipeline
correctly separates flood water from permanent water, but no agent knows
WHAT the permanent water is. The satellite agent reports water. The hazard
LLM reads that as flood signal. The impact agent computes population at
risk. The report says flood. None of them knows those pixels are a river
that is always there.

Naming closes that gap with the cheapest possible source: OSM via Overpass
(already integrated in the impact agent). The output is a small list —
`[{"name": "Ravi River", "area_km2": 12.4, "kind": "river"}, ...]` — that
rides the contract into hazard/impact/report and, critically, into the LLM
prompts that currently receive a bare water figure.

**Best-effort by construction.** Overpass is rate-limited and periodically
down. Every failure path returns an empty list with a logged reason; naming
is context, never a precondition for producing a flood answer.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Same endpoints/failover order the impact agent already uses — one of these
# is usually up when another is rate-limiting.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

REQUEST_TIMEOUT_SECONDS = 35

# Only NAMED features are useful here — an unnamed pond tells an LLM nothing
# it did not already know from the occurrence mask. The tag set is the one
# the task specifies, which covers the cases that actually matter for flood
# framing: rivers (linear, the dominant case), lakes/reservoirs (areal).
_TAG_FILTERS = (
    ('natural', 'water'),
    ('waterway', 'river'),
    ('waterway', 'riverbank'),
    ('landuse', 'reservoir'),
)


def _overpass_query(bbox: list) -> str:
    """Overpass QL for named water features intersecting the AOI bbox.

    bbox is [west, south, east, north]; Overpass wants south,west,north,east.
    Exact tag matching (no regex) for maximum endpoint compatibility — the
    same choice `agents/impact/tasks/infrastructure.py` made for the same
    reason.
    """
    w, s, e, n = bbox[0], bbox[1], bbox[2], bbox[3]
    bb = f"{s},{w},{n},{e}"
    parts = []
    for key, value in _TAG_FILTERS:
        # Require a name: unnamed geometry cannot contribute a NAME, which is
        # the entire point of this module.
        parts.append(f'  way["{key}"="{value}"]["name"]({bb});')
        parts.append(f'  relation["{key}"="{value}"]["name"]({bb});')
    body = "\n".join(parts)
    # `out tags center` keeps the payload tiny — we need names and a rough
    # position, never full geometry (areas come from the JRC mask, which is
    # a measurement; OSM polygon area would be a second, disagreeing number).
    return f"[out:json][timeout:30][maxsize:10485760];\n(\n{body}\n);\nout tags center;"


def _kind_for(tags: dict) -> str:
    if tags.get("waterway") in ("river", "riverbank"):
        return "river"
    if tags.get("landuse") == "reservoir":
        return "reservoir"
    water = tags.get("water")
    if water in ("lake", "reservoir", "pond", "lagoon"):
        return water
    if tags.get("natural") == "water":
        return "water_body"
    return "water"


def fetch_named_water_features(
    bbox: list,
    permanent_water_area_km2: Optional[float] = None,
    session=None,
) -> list[dict]:
    """Named permanent-water features intersecting `bbox`.

    Returns `[{"name", "kind", "osm_id", "area_km2"}]`, largest first, or
    `[]` on any failure (logged, never raised).

    **On `area_km2`:** the per-feature area is NOT measured from OSM
    geometry. The authoritative permanent-water area is the JRC occurrence
    mask already computed over the real clip (Phase 1c/3a); OSM polygons
    would give a second, slightly different number under the same name, and
    two disagreeing areas is worse than one. When
    `permanent_water_area_km2` is supplied it is attributed across the named
    features, and `area_basis` records that this is an ATTRIBUTION of the
    measured total, not an independent measurement. With a single named
    feature — the common case, one river — the attribution is exact.
    """
    if not bbox or len(bbox) < 4:
        return []

    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dep elsewhere
        logger.warning("[named_water] requests unavailable; skipping naming")
        return []

    query = _overpass_query(bbox)
    sess = session or requests
    elements = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            resp = sess.post(
                endpoint, data={"data": query}, timeout=REQUEST_TIMEOUT_SECONDS
            )
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
            logger.info(
                "[named_water] Overpass %s -> %d named water element(s)",
                endpoint, len(elements),
            )
            break
        except Exception as exc:  # noqa: BLE001 — naming is optional context
            logger.warning(
                "[named_water] Overpass %s failed (%s) — trying next",
                endpoint, str(exc)[:120],
            )
            continue

    if not elements:
        logger.info(
            "[named_water] no named water features resolved; the flood "
            "answer is unaffected, downstream prompts simply get no names"
        )
        return []

    # Deduplicate by name: OSM splits long rivers into many ways, and a
    # prompt listing "Ravi River" 14 times is noise, not context.
    by_name: dict[str, dict] = {}
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        entry = by_name.setdefault(
            name,
            {
                "name": name,
                "kind": _kind_for(tags),
                "osm_id": f"{el.get('type')}/{el.get('id')}",
                "segment_count": 0,
            },
        )
        entry["segment_count"] += 1

    features = sorted(
        by_name.values(), key=lambda f: -f["segment_count"]
    )

    # Attribute the MEASURED permanent-water total across the named features
    # in proportion to their OSM segment count — a rough proxy for extent
    # within the AOI, and labelled as such so no reader mistakes it for a
    # measurement of that specific feature.
    if permanent_water_area_km2 and features:
        total_segments = sum(f["segment_count"] for f in features) or 1
        for f in features:
            f["area_km2"] = round(
                permanent_water_area_km2 * f["segment_count"] / total_segments, 3
            )
            f["area_basis"] = (
                "attributed_from_jrc_total_by_osm_segment_share"
                if len(features) > 1
                else "jrc_measured_total_single_feature"
            )
    else:
        for f in features:
            f["area_km2"] = None
            f["area_basis"] = "unavailable"

    logger.info(
        "[named_water] resolved %d named feature(s): %s",
        len(features), ", ".join(f["name"] for f in features[:5]),
    )
    return features


def describe_for_prompt(
    features: list[dict],
    permanent_water_area_km2: Optional[float],
    flood_area_km2: Optional[float],
) -> Optional[str]:
    """One sentence an LLM prompt can use verbatim.

    Returns None when there is nothing to say, so a caller can omit the
    line entirely rather than injecting "no named water features found",
    which reads as a finding when it is really an absence of context.
    """
    if not features and not permanent_water_area_km2:
        return None
    if features:
        named = "; ".join(
            f"{f['name']}"
            + (f" ({f['area_km2']} km2)" if f.get("area_km2") is not None else "")
            for f in features[:4]
        )
        lead = f"The AOI contains these permanent water bodies: {named}."
    else:
        lead = (
            f"The AOI contains {permanent_water_area_km2} km2 of permanent "
            "water (unnamed in OSM)."
        )
    if flood_area_km2 is not None:
        lead += (
            f" Water detected BEYOND that permanent baseline is "
            f"{flood_area_km2} km2. Assess flood risk on the latter, not the total."
        )
    return lead
