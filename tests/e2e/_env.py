"""Merge every service's .env into os.environ for the single-process E2E.

In production each agent runs in its own container with its own cwd, so its
`load_dotenv()` picks up that agent's .env. The E2E loads all four agent node.py
modules into ONE process, so no single cwd can satisfy all of them. This helper
front-loads every service's .env into the process environment BEFORE the graph
is imported. Combined with the per-agent `load_dotenv(..., override=False)` fix,
each agent's later load is a no-op over the already-present vars.

DB target
---------
The e2e writes to the LIVE Neon DB by default (its quota was extended, and its
schema is already correct — see schema-test.sql for the derived spec / the
mismatch table in README.md). To isolate on a local Postgres instead, export
HAZARDMIND_TEST_DSN before running; it OVERRIDES NEON_DATABASE_URL for the run.
This is the seam the original "point NEON at local postgis" plan uses — it just
defaults to Neon now that quota is available. No agent .env on disk is modified.
"""

import glob
import os
import sys
import sysconfig
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _pin_proj_data() -> str:
    """Pin PROJ_LIB/PROJ_DATA to rasterio's bundled proj.db, BEFORE any rasterio
    or GDAL import.

    Two problems on this machine: a global PROJ_LIB points at the PostgreSQL/
    PostGIS proj.db (old layout), and pyproj ships an OLDER PROJ db than
    rasterio's bundled GDAL expects. GDAL's PROJ search path is locked at first
    GDAL/rasterio import, so this MUST run at module-import time and must NOT
    import rasterio itself (that would lock the path against the leaked env).
    We locate rasterio/proj_data (then pyproj) via the venv site-packages dir by
    path, clear the polluting vars, and set the good one.

    Returns a short status string for logging.
    """
    site = sysconfig.get_paths().get("purelib") or ""
    candidates = []
    if site:
        candidates.append(os.path.join(site, "rasterio", "proj_data"))
        candidates.append(os.path.join(site, "pyproj", "proj_dir", "share", "proj"))
    # Fallback: search under sys.prefix if the layout differs.
    if not any(os.path.exists(os.path.join(c, "proj.db")) for c in candidates):
        for hit in glob.glob(
            os.path.join(sys.prefix, "**", "rasterio", "proj_data", "proj.db"),
            recursive=True,
        ):
            candidates.insert(0, os.path.dirname(hit))
            break
    for proj_dir in candidates:
        if os.path.exists(os.path.join(proj_dir, "proj.db")):
            os.environ["PROJ_LIB"] = proj_dir
            os.environ["PROJ_DATA"] = proj_dir
            # GDAL also honors PROJ_NETWORK/other; clearing GDAL_DATA leak too.
            return f"pinned to {proj_dir}"
    return "could not pin PROJ (no bundled proj.db found)"


# Run at import time, before the test imports graph.py -> rasterio.
_PROJ_STATUS = _pin_proj_data()

# Agent .envs first, backend last (backend wins on any shared key).
_ENV_FILES = [
    REPO_ROOT / "agents" / "satellite" / ".env",
    REPO_ROOT / "agents" / "hazard" / ".env",
    REPO_ROOT / "agents" / "impact" / ".env",
    REPO_ROOT / "agents" / "report" / ".env",
    REPO_ROOT / "backend" / ".env",
]


def load_all_service_envs() -> dict:
    """Populate os.environ from every service .env. Returns a per-file summary.

    A real (non-empty) value wins over an empty/placeholder already present; the
    first non-empty value for a key wins over later conflicting non-empty ones
    (agent order above), so a shared key like NEON_DATABASE_URL is stable.
    """
    loaded: dict[str, str] = {}
    for path in _ENV_FILES:
        if not path.exists():
            loaded[str(path)] = "MISSING"
            continue
        values = dotenv_values(path)
        applied = 0
        for key, val in values.items():
            if val in (None, ""):
                continue
            existing = os.environ.get(key)
            if existing in (None, ""):
                os.environ[key] = val
                applied += 1
            # else: keep the first non-empty value (silent, deterministic).
        loaded[str(path.relative_to(REPO_ROOT))] = f"loaded ({applied} new keys)"

    # PROJ was pinned at module-import time (see _pin_proj_data), before any
    # rasterio/GDAL import could lock the search path to the leaked global.
    loaded["_proj"] = _PROJ_STATUS

    # Optional local-DB override. Unset by default → the run targets Neon.
    override = os.environ.get("HAZARDMIND_TEST_DSN")
    if override:
        os.environ["NEON_DATABASE_URL"] = override
        loaded["_db_target"] = "HAZARDMIND_TEST_DSN override (local)"
    else:
        host = ""
        dsn = os.environ.get("NEON_DATABASE_URL", "")
        if "@" in dsn:
            host = dsn.split("@", 1)[1].split("/", 1)[0]
        loaded["_db_target"] = f"Neon ({host})"
    return loaded
