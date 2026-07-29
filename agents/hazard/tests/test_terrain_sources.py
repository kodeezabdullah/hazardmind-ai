"""SoilGrids + Overpass road proximity (offline, stubbed network).

These supply MEASUREMENTS into susceptibility, not verdicts. The checks that
matter are the degradation ones: an unreachable service must reduce the
score's COMPLETENESS (reported in factors_absent) and never take the run
down, and "no roads here" must not be scored as "roads at distance zero".
"""
import os
import sys

import numpy as np

_HAZARD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HAZARD)

import terrain_sources as ts  # noqa: E402
import susceptibility as su  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


class _Sess:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def _next(self):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return _Resp(o)

    def get(self, *a, **k):
        return self._next()

    def post(self, *a, **k):
        return self._next()


def soil_payload(clay_x10):
    return {"properties": {"layers": [
        {"name": "clay", "depths": [{"values": {"mean": clay_x10}}]}
    ]}}


print("\n=== 1. SoilGrids clay -> weakness score ===")
r = ts.fetch_soil_weakness(35.9, 74.3, session=_Sess([soil_payload(400)]))
check("returns a result", r is not None)
check("clay scaled g/kg x10 -> percent", r["clay_percent"] == 40.0,
      str(r.get("clay_percent")))
check("40% clay -> fully weak (1.0)", r["weakness_score"] == 1.0,
      str(r.get("weakness_score")))
low = ts.fetch_soil_weakness(35.9, 74.3, session=_Sess([soil_payload(100)]))
check("10% clay -> low weakness", 0.2 < low["weakness_score"] < 0.3,
      str(low["weakness_score"]))
check("declared a POINT sample, not a field", "POINT sample" in r["sampling"])
check("basis admits it is not a fitted relationship",
      "NOT a fitted" in r["basis"])
print(f"    clay 40% -> {r['weakness_score']}, clay 10% -> "
      f"{low['weakness_score']}")

print("\n=== 2. SoilGrids failure degrades to None, never raises ===")
check("network error -> None",
      ts.fetch_soil_weakness(0, 0, session=_Sess([RuntimeError("down")])) is None)
check("malformed payload -> None",
      ts.fetch_soil_weakness(0, 0, session=_Sess([{"properties": {}}])) is None)

print("\n=== 3. Road distance grid ===")
BBOX = [74.30, 35.90, 74.40, 36.00]
roads = {"elements": [{"type": "way", "geometry": [
    {"lat": 35.95, "lon": 74.30 + i * 0.002} for i in range(50)
]}]}
grid = ts.fetch_road_distance_grid(BBOX, (100, 100), session=_Sess([roads]))
check("returns a grid of the requested shape",
      grid is not None and grid.shape == (100, 100))
check("distance is 0 on the road line", float(grid.min()) == 0.0)
check("and grows away from it", float(grid.max()) > 1000.0,
      f"max={float(grid.max()):.0f} m")
# The road runs along lat 35.95 -> the middle row; edges must be farther.
mid = float(grid[50, :].mean())
edge = float(grid[0, :].mean())
check("cells near the road are closer than cells at the AOI edge",
      mid < edge, f"mid={mid:.0f} edge={edge:.0f}")
print(f"    near-road mean {mid:.0f} m vs edge mean {edge:.0f} m")

print("\n=== 4. 'No roads' is NOT scored as 'distance zero' ===")
check("empty Overpass response -> None (factor omitted)",
      ts.fetch_road_distance_grid(BBOX, (50, 50),
                                  session=_Sess([{"elements": []}])) is None)
check("all endpoints down -> None",
      ts.fetch_road_distance_grid(
          BBOX, (50, 50),
          session=_Sess([RuntimeError("a"), RuntimeError("b"),
                         RuntimeError("c")])) is None)
check("missing bbox -> None", ts.fetch_road_distance_grid([], (10, 10)) is None)

print("\n=== 5. Absent factors reduce COMPLETENESS, not correctness ===")
N = 60
yy, xx = np.mgrid[0:N, 0:N].astype("float32")
dem = (N - xx) * 25.0
full = su.compute_susceptibility(
    dem, 30.0,
    lithology_score=np.full((N, N), r["weakness_score"], dtype="float32"),
    distance_to_roads_m=np.full((N, N), 30.0, dtype="float32"),
)
partial = su.compute_susceptibility(dem, 30.0)
check("full run uses both extra factors",
      {"lithology", "distance_to_roads"} <= set(full["factors_used"]))
check("partial run declares them absent",
      set(partial["factors_absent"]) == {"lithology", "distance_to_roads"})
for label, res in (("full", full), ("partial", partial)):
    check(f"{label}: weights still sum to 1",
          abs(sum(res["weights_applied"].values()) - 1.0) < 1e-6)
    check(f"{label}: score bounded [0,1]",
          0.0 <= res["mean_susceptibility"] <= 1.0)
print(f"    full {full['mean_susceptibility']} "
      f"({len(full['factors_used'])} factors) vs partial "
      f"{partial['mean_susceptibility']} ({len(partial['factors_used'])})")

print("\n=== 6. Measurements, not verdicts ===")
src = open(os.path.join(_HAZARD, "terrain_sources.py"), encoding="utf-8").read()
check("module states these are measurements, not verdicts",
      "MEASUREMENTS, not verdicts" in src)
check("no hazard product is imported (no LHASA/ShakeMap/PAGER)",
      not any(x in src for x in ("LHASA", "ShakeMap", "PAGER")))

print(f"\n{'='*62}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*62}")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
