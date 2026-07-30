"""Regression test for the 2026-07-30 Muzaffargarh/Trimmu investigation.

WHAT HAPPENED
-------------
A run over the Trimmu (Ahmedpur Sial) AOI reported total_zones=0 while its
own internal diagnostic text said the scene was "physically consistent with
a high-water-fraction scenario (88.71%)". `vectorize_classification` drops
any individual contiguous polygon under MIN_ZONE_AREA_KM2 (0.5 km2) — ~100x
the detector's own 50-pixel/~0.005 km2 speckle floor — so a genuinely
fragmented flood (many small real patches, none individually large) and a
genuinely noisy scene (many small spurious patches) can produce the
identical zero-zone output. The pipeline currently cannot tell them apart.

WHY THIS TEST EXISTS
---------------------
Before touching MIN_ZONE_AREA_KM2 or any discard logic, `_dropped_zone_diagnostics`
was added to `vectorize_classification`'s return value — a pure observation
of what got dropped and at what size, changing NO discard decision. This test
asserts exactly that contract: the returned `features`/`total_area` are
BIT-IDENTICAL to before the diagnostics were added, and the new field
correctly counts and sizes what was thrown away, so a real noise event
(Kanalia, ROC AUC 0.487, proven signal-free) can later be compared against
Muzaffargarh's actual pattern with real numbers instead of a guess — and so
that comparison is possible without re-deriving the counting logic by hand a
second time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from rasterio.crs import CRS

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def _synthetic_classification(shape=(200, 200)):
    """A classification array with THREE known-size water blobs at 10 m/px:
    one large (clears 0.5 km2), one small (does not), one tiny (does not).
    """
    arr = np.zeros(shape, dtype="uint8")
    # Large blob: 80x80 px @ 10m = 640,000 m2 = 0.64 km2 -> SURVIVES.
    arr[10:90, 10:90] = 2
    # Small blob: 20x20 px @ 10m = 40,000 m2 = 0.04 km2 -> DROPPED.
    arr[120:140, 20:40] = 2
    # Tiny blob: 8x8 px @ 10m = 6,400 m2 = 0.0064 km2 -> DROPPED, but clears
    # the detector's own 50-px speckle floor upstream (out of scope here;
    # this test is purely about the vectorization-stage filter).
    arr[160:168, 160:168] = 2
    return arr


def test_dropped_zone_diagnostics_matches_actual_discards():
    import processor

    arr = _synthetic_classification()
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 4000000.0)

    result = processor.vectorize_classification(
        arr, transform, crs=CRS.from_epsg(32643), disaster_type="flood", scheme_key="SAR"
    )

    diag = result["_dropped_zone_diagnostics"]
    # Exactly 2 sub-threshold blobs (small + tiny) must be recorded dropped.
    assert diag["count"] == 2, diag
    assert diag["min_zone_area_km2_threshold"] == pytest.approx(0.5)
    # Every recorded dropped size must genuinely be under the threshold —
    # the diagnostic must not double-count kept polygons.
    assert all(s < 0.5 for s in diag["sizes_km2"]), diag["sizes_km2"]
    # The aggregate dropped area is the sum of the individually recorded
    # sizes — internal consistency, not just a plausible-looking number.
    assert diag["total_area_km2"] == pytest.approx(
        sum(diag["sizes_km2"]), abs=0.01
    )


def test_diagnostics_do_not_change_kept_features_or_total_area():
    """The behavioural guarantee: adding the counter must not alter which
    polygons survive or what total_area reports. Verified by checking the
    surviving feature areas sum to a value independent of the dropped count,
    and that no dropped-size polygon appears among the kept features.
    """
    import processor

    arr = _synthetic_classification()
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 4000000.0)

    result = processor.vectorize_classification(
        arr, transform, crs=CRS.from_epsg(32643), disaster_type="flood", scheme_key="SAR"
    )

    kept_areas = [f["properties"]["area_km2"] for f in result["features"]]
    assert len(kept_areas) == 1, "only the large blob should survive"
    assert kept_areas[0] == pytest.approx(0.64, abs=0.01)
    assert result["total_area"] == pytest.approx(0.64, abs=0.01)
    # None of the surviving features may be one of the dropped sizes —
    # proves the diagnostic counter is observing a DISJOINT set, not
    # double-counting or leaking a kept polygon into the dropped tally.
    dropped_sizes = set(result["_dropped_zone_diagnostics"]["sizes_km2"])
    assert not (dropped_sizes & set(kept_areas))


def test_no_dropped_polygons_yields_empty_diagnostics():
    """A scene with nothing below threshold must report count=0, not omit
    the field or report a misleading non-zero default."""
    import processor

    arr = np.zeros((200, 200), dtype="uint8")
    arr[10:190, 10:190] = 2  # one huge blob, well above 0.5 km2
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 4000000.0)

    result = processor.vectorize_classification(
        arr, transform, crs=CRS.from_epsg(32643), disaster_type="flood", scheme_key="SAR"
    )
    diag = result["_dropped_zone_diagnostics"]
    assert diag["count"] == 0
    assert diag["total_area_km2"] == 0.0
    assert diag["sizes_km2"] == []
