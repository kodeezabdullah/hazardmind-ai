// Fetches a city's REAL administrative boundary (not a mock square) from
// geoBoundaries — the same open dataset the satellite agent uses. Works for any
// city worldwide: infer the country, pull that country's admin polygons, and
// return the polygon whose name matches the city.

type LngLat = [number, number];

const NOMINATIM = "https://nominatim.openstreetmap.org/search";
const GB_META = "https://www.geoboundaries.org/api/current/gbOpen";

// Cache resolved boundaries per query so repeated lookups are instant.
const cache = new Map<string, GeoJSON.Feature | null>();

/** Infer the ISO3 country code for a place name via Nominatim. */
async function countryIso3(city: string): Promise<string | null> {
  try {
    const url = `${NOMINATIM}?q=${encodeURIComponent(city)}&format=json&addressdetails=1&accept-language=en&limit=1`;
    const res = await fetch(url, { headers: { "Accept-Language": "en" } });
    if (!res.ok) return null;
    const data = (await res.json()) as Array<{ address?: { country_code?: string } }>;
    const cc = data?.[0]?.address?.country_code?.toUpperCase();
    if (!cc) return null;
    return ALPHA2_TO_ISO3[cc] ?? null;
  } catch {
    return null;
  }
}

/** geoBoundaries metadata -> the GeoJSON download URL for a country + level.
 *  geoBoundaries returns a GitHub `raw.github` / `github.com/.../raw/` URL which
 *  GitHub serves WITHOUT CORS headers, so a browser fetch is blocked. We rewrite
 *  it to the jsDelivr CDN, which mirrors GitHub files with `Access-Control-
 *  Allow-Origin: *`, so the boundary loads in the browser. */
async function gjUrl(iso3: string, level: string): Promise<string | null> {
  try {
    const res = await fetch(`${GB_META}/${iso3}/${level}/`);
    if (!res.ok) return null;
    const meta = (await res.json()) as { gjDownloadURL?: string; simplifiedGeometryGeoJSON?: string };
    const raw = meta.simplifiedGeometryGeoJSON || meta.gjDownloadURL || null;
    return raw ? toCorsFriendly(raw) : null;
  } catch {
    return null;
  }
}

/** Rewrite a GitHub raw URL to the CORS-enabled jsDelivr CDN. */
function toCorsFriendly(url: string): string {
  // https://github.com/<owner>/<repo>/raw/<ref>/<path>
  let m = url.match(/github\.com\/([^/]+)\/([^/]+)\/raw\/([^/]+)\/(.+)$/);
  if (m) {
    const [, owner, repo, ref, path] = m;
    return `https://cdn.jsdelivr.net/gh/${owner}/${repo}@${ref}/${path}`;
  }
  // https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>
  m = url.match(/raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/([^/]+)\/(.+)$/);
  if (m) {
    const [, owner, repo, ref, path] = m;
    return `https://cdn.jsdelivr.net/gh/${owner}/${repo}@${ref}/${path}`;
  }
  return url;
}

/** Pull a country's admin polygons and return the one matching the city. */
async function matchBoundary(city: string, iso3: string): Promise<GeoJSON.Feature | null> {
  const want = normalise(city.split(",")[0]);
  // Try ADM2 (district — usually the clean "Peshawar"/"Lahore" unit) first, then
  // the finer ADM3 (tehsil), then ADM1. At each level prefer an EXACT name match
  // before a partial one, so "Peshawar" doesn't grab "Peshawar I".
  for (const level of ["ADM2", "ADM3", "ADM1"]) {
    const url = await gjUrl(iso3, level);
    if (!url) continue;
    try {
      const res = await fetch(url);
      if (!res.ok) continue;
      const fc = (await res.json()) as GeoJSON.FeatureCollection;
      const named = (fc.features ?? []).map((f) => ({
        feature: f,
        name: normalise(String((f.properties as { shapeName?: string })?.shapeName ?? "")),
      }));
      const exact = named.find((n) => n.name === want);
      const partial = named.find((n) => n.name.includes(want) || want.includes(n.name));
      const hit = exact ?? partial;
      if (hit) {
        return { type: "Feature", properties: { name: city, level }, geometry: hit.feature.geometry };
      }
    } catch {
      // try the next level
    }
  }
  return null;
}

function normalise(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
}

/**
 * Resolve a city to its real boundary Feature, or null if it can't be found.
 * Cached per city string.
 */
export async function loadCityBoundary(city: string): Promise<GeoJSON.Feature | null> {
  const key = city.trim().toLowerCase();
  if (cache.has(key)) return cache.get(key) ?? null;

  const iso3 = await countryIso3(city);
  const boundary = iso3 ? await matchBoundary(city, iso3) : null;
  cache.set(key, boundary);
  return boundary;
}

/** Bounding box [w, s, e, n] of a boundary feature (for the camera fly-to). */
export function boundaryBbox(feature: GeoJSON.Feature): [number, number, number, number] | null {
  const pts: LngLat[] = [];
  const walk = (v: unknown) => {
    if (Array.isArray(v)) {
      if (typeof v[0] === "number" && typeof v[1] === "number") {
        pts.push([v[0] as number, v[1] as number]);
      } else {
        v.forEach(walk);
      }
    }
  };
  walk((feature.geometry as { coordinates?: unknown }).coordinates);
  if (!pts.length) return null;
  const lngs = pts.map((p) => p[0]);
  const lats = pts.map((p) => p[1]);
  return [Math.min(...lngs), Math.min(...lats), Math.max(...lngs), Math.max(...lats)];
}

// Minimal alpha-2 -> ISO3 map for common countries (extend as needed). Covers
// the regions we demo; geoBoundaries keys on ISO3.
const ALPHA2_TO_ISO3: Record<string, string> = {
  PK: "PAK", IN: "IND", BD: "BGD", NP: "NPL", LK: "LKA", AF: "AFG",
  US: "USA", GB: "GBR", CA: "CAN", AU: "AUS", CN: "CHN", JP: "JPN",
  ID: "IDN", PH: "PHL", TR: "TUR", IR: "IRN", IQ: "IRQ", SA: "SAU",
  EG: "EGY", NG: "NGA", KE: "KEN", ET: "ETH", ZA: "ZAF", BR: "BRA",
  MX: "MEX", DE: "DEU", FR: "FRA", IT: "ITA", ES: "ESP", RU: "RUS",
  UA: "UKR", PL: "POL", TH: "THA", VN: "VNM", MY: "MYS", MM: "MMR",
};
