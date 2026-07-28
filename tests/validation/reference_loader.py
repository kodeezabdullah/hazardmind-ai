"""Downloads and parses Copernicus EMS Rapid Mapping delineation ZIP products.

Each product ZIP (e.g. `EMSR773_AOI01_DEL_MONIT03_v1.zip`) contains a
GeoPackage/shapefile set with several layers (`maximumFloodExtentA`,
`observedEventA`, `floodDepthA`, ...). The event YAML files under
`reference_events/` name which layer is "of record" for this harness
(`vector_layer_of_record`) — see SELECTION.md for why `maximumFloodExtentA`
was chosen over `observedEventA` for both selected events.

Downloads are cached under `tests/validation/cache/` so re-running the
harness doesn't re-fetch multi-MB ZIPs from the EMS server every time.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Optional

import geopandas as gpd
import requests
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

CACHE_DIR = Path(__file__).resolve().parent / "cache"


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def _find_layer_file(extract_dir: Path, layer_name_fragment: str) -> Optional[Path]:
    """EMS ZIPs bundle multiple formats (shp/gpkg/geojson) per layer, all
    sharing the layer name as a filename fragment. Prefer .shp, since every
    EMS product observed in this harness ships one."""
    candidates = []
    for ext in (".shp", ".gpkg", ".geojson", ".json"):
        candidates.extend(extract_dir.rglob(f"*{layer_name_fragment}*{ext}"))
    return candidates[0] if candidates else None


def load_reference_geometry(
    download_url: str,
    vector_layer_of_record: str,
    event_key: str,
) -> tuple[BaseGeometry, str]:
    """Returns (geometry, crs) for the named layer, dissolved into a single
    (multi)polygon. Raises FileNotFoundError if the layer isn't in the ZIP —
    this is a hard failure, not a silent empty-geometry fallback, since a
    missing reference layer means the harness has nothing to score against.
    """
    zip_path = CACHE_DIR / f"{event_key}.zip"
    _download(download_url, zip_path)

    extract_dir = CACHE_DIR / event_key
    if not extract_dir.exists():
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

    layer_file = _find_layer_file(extract_dir, vector_layer_of_record)
    if layer_file is None:
        raise FileNotFoundError(
            f"Layer matching {vector_layer_of_record!r} not found in "
            f"{download_url} (extracted to {extract_dir}); cannot score "
            f"{event_key} without the reference layer of record."
        )

    gdf = gpd.read_file(layer_file)
    if gdf.crs is None:
        raise ValueError(
            f"{layer_file} has no CRS defined — refusing to guess one for "
            f"an area/overlap computation (see metrics.py's docstring on why "
            f"unit mislabeling is treated as a hard failure in this harness)."
        )

    geom = unary_union(gdf.geometry.values)
    return geom, str(gdf.crs)
