"""Extracts a single comparable flood-extent geometry from the real
pipeline's result dict, for scoring against an EMS reference polygon.

The pipeline's own `geojson_url` (fetched from R2 after upload) is a
FeatureCollection with one feature per hazard class (severity 1/2/3 —
see processor.py's `_SEVERITY_BY_CLASS`), each already reprojected to
WGS84. This module unions every hazard-class polygon into one "flood
extent" geometry, since the EMS reference delineation doesn't grade
severity the same way — comparing "any predicted hazard" against "any
observed flood" is the only apples-to-apples comparison available.
"""

from __future__ import annotations

from typing import Optional

import requests
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


def fetch_predicted_extent(geojson_url: Optional[str]) -> Optional[BaseGeometry]:
    """Returns the union of every hazard-class polygon in the pipeline's
    result GeoJSON (already WGS84 per processor.py's vectorize_classification),
    or None if there's no URL (a failed/degraded run — see
    `artifacts_incomplete`/`failed_artifacts` on the result dict) or the
    feature collection has zero features (a real "no zones detected" result,
    distinct from a fetch failure)."""
    if not geojson_url:
        return None

    resp = requests.get(geojson_url, timeout=60)
    resp.raise_for_status()
    fc = resp.json()

    polys = [shape(f["geometry"]) for f in fc.get("features", []) if f.get("geometry")]
    if not polys:
        return None

    return unary_union(polys)
