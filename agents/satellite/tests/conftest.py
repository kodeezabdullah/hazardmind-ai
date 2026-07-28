"""Test bootstrap (coherence pass, feat/durable-evidence-trail, 2026-07-28).

Pins PROJ_LIB/PROJ_DATA to rasterio's OWN bundled proj database, BEFORE
rasterio (or anything that imports it -- GDAL registers its PROJ context at
`import rasterio` time, not at first CRS lookup) is imported anywhere in this
process. Without this, a system PostGIS install's proj.db (this dev box has
PostgreSQL 17 + postgis-3.6 on PATH/PROJ_LIB) shadows rasterio's bundled one
-- GDAL/PROJ reads whichever proj.db PROJ_LIB points at, and an older
postgis-bundled proj.db fails rasterio's CRS/reprojection calls with:

    PROJ: proj_create_from_database: .../postgis-3.6/proj/proj.db contains
    DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number >= 6 is expected.

CRITICAL ORDERING NOTE (found the hard way): setting os.environ["PROJ_LIB"]
AFTER `import rasterio` does NOT work, even though it looks like it should --
GDAL's PROJ context is initialized at import/driver-registration time using
whatever PROJ_LIB was already in the process environment, and once PROJ has
failed once with a bad proj.db it stays broken for the rest of the process;
no later os.environ reassignment can undo it. The path below is therefore
computed WITHOUT importing rasterio (a plain filesystem walk from this
file's own location to the sibling venv), so the env var is correct before
rasterio's first import anywhere in the test session.

This previously required a developer to manually override PROJ_LIB in their
shell before running the suite; CLAUDE.md documented it as a known
environment quirk (originally reported as 2 failing tests, actually 8 across
test_bug_fixes.py and one in test_correctness_fixes_20260727.py -- confirmed
via `git stash` to be identical before/after the durable-evidence-trail
changes, i.e. pre-existing, not a regression). Fixing it here means the
suite is green without any manual env setup, for any developer, on any box
that happens to have a different/older PROJ install on PATH.

pytest imports conftest.py before collecting/importing test modules in the
same directory, so this runs before any test's `import rasterio` -- as long
as conftest itself never imports rasterio before setting the env var.
"""
import os
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_VENV_SITE_PACKAGES_CANDIDATES = [
    _TESTS_DIR.parent / "venv" / "Lib" / "site-packages",  # Windows venv layout
    _TESTS_DIR.parent / "venv" / "lib" / "python3" / "site-packages",
]

for _site_packages in _VENV_SITE_PACKAGES_CANDIDATES:
    _bundled_proj_data = _site_packages / "rasterio" / "proj_data"
    if _bundled_proj_data.is_dir():
        os.environ["PROJ_LIB"] = str(_bundled_proj_data)
        os.environ["PROJ_DATA"] = str(_bundled_proj_data)
        break
else:
    # No local venv rasterio proj_data found (e.g. a differently-laid-out
    # environment, or rasterio not installed) -- leave PROJ_LIB untouched;
    # tests that need it will surface their own clear error rather than a
    # silently wrong path being set here.
    pass
