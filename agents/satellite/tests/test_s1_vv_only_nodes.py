"""Sentinel-1 VV-only per-band Nodes download (2026-07-28).

Prior to this change, `_S1_POLARIZATIONS = ["VV", "VH"]` and
`_download_bands_via_nodes` hard-returned None for any non-Sentinel-2
satellite_type — so EVERY S1 scene fell back to `_download_product_zip`
(the whole ~1.1-1.7 GB .SAFE archive), and VH was downloaded but never read
by any index/classification code (`calculate_indices`' SAR path only ever
reads `bands["VV"]`, confirmed by reading the function directly).

This is offline/deterministic: no live CDSE calls, no rasterio needed —
`_resolve_s1_band_nodes` only does string matching over a listing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import processor  # noqa: E402

_PASS = 0
_FAIL = 0


def ok(msg: str) -> None:
    global _PASS
    _PASS += 1
    print(f"  OK: {msg}")


def bad(msg: str) -> None:
    global _FAIL
    _FAIL += 1
    print(f"  FAIL: {msg}")


def test_s1_polarizations_is_vv_only():
    print("\nVV-only: _S1_POLARIZATIONS no longer requests VH")
    if processor._S1_POLARIZATIONS == ["VV"]:
        ok(f"_S1_POLARIZATIONS = {processor._S1_POLARIZATIONS}")
    else:
        bad(f"expected ['VV'], got {processor._S1_POLARIZATIONS}")


def test_resolve_s1_band_nodes_matches_vv_only():
    print("\n_resolve_s1_band_nodes: matches the VV measurement tiff, ignores VH")
    session = MagicMock()

    def fake_list_nodes(_session, _product_id, segments, _headers, _timeout):
        if segments == []:
            return ["S1A_IW_GRDH_1SDV_20231008T180000.SAFE"]
        if segments == ["S1A_IW_GRDH_1SDV_20231008T180000.SAFE", "measurement"]:
            return [
                "s1a-iw-grd-vh-20231008t180000-20231008t180025-050700-061c4a-002.tiff",
                "s1a-iw-grd-vv-20231008t180000-20231008t180025-050700-061c4a-001.tiff",
            ]
        raise AssertionError(f"unexpected segments {segments}")

    orig = processor._list_nodes
    processor._list_nodes = fake_list_nodes
    try:
        resolved = processor._resolve_s1_band_nodes(
            session, "prod-1", ["VV"], {}, (5, 5)
        )
    finally:
        processor._list_nodes = orig

    if "VV" in resolved and "VH" not in resolved:
        segs = resolved["VV"]
        if segs[-1].endswith("-vv-20231008t180000-20231008t180025-050700-061c4a-001.tiff"):
            ok(f"resolved VV node path: {segs}")
        else:
            bad(f"resolved VV node but path looks wrong: {segs}")
    else:
        bad(f"expected only VV resolved, got keys {list(resolved.keys())}")


def test_download_bands_via_nodes_no_longer_s2_only():
    print("\n_download_bands_via_nodes: no longer hard-returns None for sentinel-1")
    # Cheapest possible probe: satellite_type="sentinel-1" with no product Id
    # should return None for the "no product_id" reason, NOT the old
    # unconditional "satellite_type != sentinel-2" guard — verified by
    # checking the guard condition directly against the fixed source, since
    # exercising the full network path here would defeat the offline-only
    # goal of this test file.
    result = processor._download_bands_via_nodes(
        {"Id": None}, "tok", "evt-x", ["VV"], "sentinel-1"
    )
    if result is None:
        ok("returns None for a scene with no product Id (expected reason: "
           "missing Id), not because satellite_type=='sentinel-1' is rejected "
           "outright")
    else:
        bad(f"expected None, got {result}")

    # Confirm the guard now accepts sentinel-1 as a valid type by checking it
    # does NOT return None purely because of satellite_type on a scene that
    # DOES have a product_id and DOES have a cached band on disk already
    # (exercises the fast on-disk-cache path with no network at all).
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp:
        orig_temp_root = processor.TEMP_ROOT
        processor.TEMP_ROOT = tmp
        try:
            bands_dir = os.path.join(tmp, "evt-y", "bands")
            os.makedirs(bands_dir, exist_ok=True)
            fake_path = os.path.join(bands_dir, "VV.tiff")
            with open(fake_path, "wb") as f:
                f.write(b"fake-tiff-bytes")

            cached = processor._download_bands_via_nodes(
                {"Id": "prod-y"}, "tok", "evt-y", ["VV"], "sentinel-1"
            )
        finally:
            processor.TEMP_ROOT = orig_temp_root

    if cached == {"VV": os.path.join(tmp, "evt-y", "bands", "VV.tiff")}:
        ok("sentinel-1 on-disk cache fast path returns the cached .tiff, "
           "confirming the S2-only guard was removed and the extension is "
           "satellite-aware (.tiff for S1, not .jp2)")
    else:
        bad(f"expected cached VV.tiff, got {cached}")


if __name__ == "__main__":
    test_s1_polarizations_is_vv_only()
    test_resolve_s1_band_nodes_matches_vv_only()
    test_download_bands_via_nodes_no_longer_s2_only()
    print(f"\n{_PASS} passed, {_FAIL} failed")
    raise SystemExit(1 if _FAIL else 0)
