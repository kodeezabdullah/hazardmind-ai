"""Phase 3b — named permanent-water features (offline, no network).

The point of naming is that downstream agents currently do not know WHAT the
water is. These checks cover the contract (dedup, ordering, area attribution
labelling) and — most importantly — that every failure path degrades to an
empty list rather than breaking the run, since naming is context and never a
precondition for a flood answer.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import named_water  # noqa: E402

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
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


class _Session:
    """Stub Overpass: per-endpoint scripted outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, url, data=None, timeout=None):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return _Resp(o)


BBOX = [74.2, 31.4, 74.5, 31.7]  # Lahore-ish, where the Ravi runs

_RAVI = {
    "elements": [
        # A river split into many OSM ways — the realistic case.
        {"type": "way", "id": 1, "tags": {"waterway": "river", "name": "Ravi River"}},
        {"type": "way", "id": 2, "tags": {"waterway": "river", "name": "Ravi River"}},
        {"type": "way", "id": 3, "tags": {"waterway": "river", "name": "Ravi River"}},
        {"type": "way", "id": 9, "tags": {"landuse": "reservoir", "name": "BRB Canal Pond"}},
        # Unnamed feature: must be dropped, it adds no context.
        {"type": "way", "id": 4, "tags": {"natural": "water"}},
    ]
}

print("\n=== 1. Named features: dedup, kind, ordering ===")
s = _Session([_RAVI])
feats = named_water.fetch_named_water_features(BBOX, 12.4, session=s)
check("returns features", len(feats) == 2, f"got {len(feats)}")
check("river ways deduplicated to ONE named feature",
      sum(1 for f in feats if f["name"] == "Ravi River") == 1)
check("unnamed feature dropped",
      all(f.get("name") for f in feats))
check("largest (most segments) first", feats[0]["name"] == "Ravi River",
      f"got {feats[0]['name']}")
check("kind classified", feats[0]["kind"] == "river", f"got {feats[0]['kind']}")
print(f"    {[(f['name'], f['kind'], f['area_km2']) for f in feats]}")

print("\n=== 2. Area is ATTRIBUTED from the JRC measurement, and says so ===")
total = sum(f["area_km2"] for f in feats)
check("attributed areas sum to the measured JRC total",
      abs(total - 12.4) < 0.01, f"got {total}")
check("area_basis names it as an attribution, not a measurement",
      all("attributed" in f["area_basis"] for f in feats),
      f"got {[f['area_basis'] for f in feats]}")

s2 = _Session([{"elements": [_RAVI["elements"][0]]}])
one = named_water.fetch_named_water_features(BBOX, 12.4, session=s2)
check("single feature gets the exact measured total",
      one[0]["area_km2"] == 12.4 and "single_feature" in one[0]["area_basis"],
      f"got {one[0]['area_km2']} / {one[0]['area_basis']}")

print("\n=== 3. Endpoint failover ===")
s3 = _Session([RuntimeError("rate limited"), _RAVI])
f3 = named_water.fetch_named_water_features(BBOX, 12.4, session=s3)
check("falls through to the second endpoint", len(f3) == 2 and s3.calls == 2,
      f"calls={s3.calls} n={len(f3)}")

print("\n=== 4. Every failure path degrades to [] — never breaks the run ===")
s4 = _Session([RuntimeError("a"), RuntimeError("b"), RuntimeError("c")])
check("all endpoints down -> []",
      named_water.fetch_named_water_features(BBOX, 12.4, session=s4) == [])
s5 = _Session([{"elements": []}])
check("empty response -> []",
      named_water.fetch_named_water_features(BBOX, 12.4, session=s5) == [])
check("missing bbox -> []", named_water.fetch_named_water_features([], 12.4) == [])
check("no permanent-water total -> areas None, not fabricated",
      all(f["area_km2"] is None
          for f in named_water.fetch_named_water_features(
              BBOX, None, session=_Session([_RAVI]))))

print("\n=== 5. The prompt sentence states the RIGHT number to assess on ===")
line = named_water.describe_for_prompt(feats, 12.4, 3.1)
check("names the feature", "Ravi River" in line, line)
check("gives the flood-only figure", "3.1 km2" in line, line)
check("instructs assessment on the flood figure, not the total",
      "not the total" in line, line)
print(f"    {line}")

check("no features and no water -> None (omit the line entirely)",
      named_water.describe_for_prompt([], None, 3.1) is None)
unnamed = named_water.describe_for_prompt([], 12.4, 3.1)
check("water but no names -> still states the baseline",
      unnamed is not None and "unnamed in OSM" in unnamed, unnamed)

print(f"\n{'='*58}\nTOTAL: {PASS} PASS / {FAIL} FAIL\n{'='*58}")
# Run as a script -> exit code. Imported by pytest -> assert instead, so
# collection of the whole directory is not aborted by a SystemExit.
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
else:
    assert FAIL == 0, f"{FAIL} check(s) failed"
