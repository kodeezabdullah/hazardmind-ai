"use client";

import { useEffect, useRef } from "react";
import mapboxgl from "mapbox-gl";
import type { HazardMindResult, LayerState } from "../lib/types";

type HazardMapProps = {
  result: HazardMindResult;
  layers: LayerState;
  perspective?: boolean;
  showHud?: boolean;
  // When true, the globe flies/zooms to the event area and reveals the heatmap
  // + zone overlays. When false, it idles (spinning globe).
  focus?: boolean;
};

type LngLatPair = [number, number];

mapboxgl.accessToken = process.env.NEXT_PUBLIC_MAPBOX_TOKEN ?? "";

// Real-world satellite imagery (blue earth) for the rotating globe.
const MAP_STYLE = "mapbox://styles/mapbox/satellite-streets-v12";

// Idle globe spin speed (degrees of longitude per animation frame tick).
const SPIN_DEGREES_PER_SECOND = 6;

// The public R2 bucket has no CORS headers, so browser fetches of the PNG maps /
// zones GeoJSON are blocked. Route them through our same-origin proxy.
function proxied(url?: string): string | undefined {
  if (!url) return url;
  if (url.includes("r2.dev")) {
    return `/api/r2?url=${encodeURIComponent(url)}`;
  }
  return url;
}

export function HazardMap({ result, layers, perspective = false, showHud = true, focus = false }: HazardMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<mapboxgl.Map | null>(null);
  const loadedRef = useRef(false);
  const markersRef = useRef<mapboxgl.Marker[]>([]);
  const spinningRef = useRef(true);
  const spinFrameRef = useRef<number | null>(null);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) {
      return;
    }

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      // Start zoomed out on the globe so the intro spin reads as a planet view.
      center: [60, 20],
      zoom: 1.6,
      minZoom: 1,
      maxZoom: 16,
      projection: "globe",
      attributionControl: false,
      dragRotate: true,
      dragPan: true,
      scrollZoom: true,
      pitchWithRotate: true,
      maxPitch: 85,
    });

    // Full cursor control: scroll to zoom, drag to pan/rotate the globe, etc.
    map.scrollZoom.enable();
    map.dragPan.enable();
    map.dragRotate.enable();
    map.touchZoomRotate.enable();
    map.keyboard.enable();
    map.doubleClickZoom.enable();

    map.addControl(
      new mapboxgl.NavigationControl({ showCompass: true, showZoom: true, visualizePitch: true }),
      "top-right",
    );
    map.addControl(new mapboxgl.AttributionControl({ compact: true }), "bottom-right");

    // ---- Idle globe rotation -------------------------------------------------
    // The globe slowly spins until we focus on an event. Any user interaction
    // (drag/zoom) stops the spin so they stay in control.
    let lastTime = performance.now();
    const spin = (now: number) => {
      const delta = (now - lastTime) / 1000;
      lastTime = now;
      if (spinningRef.current && map.getZoom() < 4) {
        const center = map.getCenter();
        center.lng -= SPIN_DEGREES_PER_SECOND * delta;
        map.setCenter(center);
      }
      spinFrameRef.current = requestAnimationFrame(spin);
    };

    const stopSpin = () => {
      spinningRef.current = false;
    };
    map.on("mousedown", stopSpin);
    map.on("touchstart", stopSpin);
    map.on("wheel", stopSpin);

    map.once("style.load", () => {
      // Atmosphere / fog gives the real-earth globe its blue glowing edge.
      map.setFog({
        color: "rgb(186, 210, 235)",
        "high-color": "rgb(36, 120, 220)",
        "horizon-blend": 0.04,
        "space-color": "rgb(4, 8, 20)",
        "star-intensity": 0.5,
      });
    });

    map.once("load", () => {
      loadedRef.current = true;
      spinFrameRef.current = requestAnimationFrame(spin);

      addEventLayers(map, result);
      attachInteractions(map);
      markersRef.current = addFacilityMarkers(map, result);
      applyVisibility(map, markersRef.current, layers);

      // Overlays + facility markers start hidden so the idle globe stays clean
      // (no pins, no zones); they are revealed when `focus` becomes true.
      setOverlayVisible(map, focus);
      setMarkersVisible(markersRef.current, focus);
      if (focus) {
        spinningRef.current = false;
        focusOnEvent(map, result, perspective, () => {});
      }
    });

    mapRef.current = map;

    return () => {
      if (spinFrameRef.current !== null) {
        cancelAnimationFrame(spinFrameRef.current);
      }
      markersRef.current.forEach((marker) => marker.remove());
      markersRef.current = [];
      loadedRef.current = false;
      map.remove();
      mapRef.current = null;
    };
    // Build the map ONCE on mount. Data changes are applied via the effect below
    // (sources updated in place) so the camera never resets mid-run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // When the result changes, refresh the data sources in place (no map rebuild,
  // no camera move).
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loadedRef.current) return;
    (map.getSource("hazard-zones") as mapboxgl.GeoJSONSource | undefined)?.setData(
      result.analysis.zones as GeoJSON.FeatureCollection,
    );
    (map.getSource("hazard-heat") as mapboxgl.GeoJSONSource | undefined)?.setData(
      zonesToWeightedPoints(result.analysis.zones as GeoJSON.FeatureCollection),
    );
  }, [result]);

  useEffect(() => {
    if (!mapRef.current || !loadedRef.current) {
      return;
    }
    // Layer toggles only apply once the globe is focused on an event; on the idle
    // globe everything stays hidden regardless of the (default-on) layer state.
    if (!focus) return;
    applyVisibility(mapRef.current, markersRef.current, layers);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layers, focus]);

  // Focus: ONLY when `focus` flips to true (pipeline done) do we stop the spin
  // and fly to the event. We intentionally do NOT depend on `result` here, so a
  // mid-run result update never triggers an early/wrong zoom — the globe just
  // keeps spinning until `focus` is set.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loadedRef.current) {
      return;
    }
    if (focus) {
      spinningRef.current = false;
      focusOnEvent(map, result, perspective, () => {});
      setOverlayVisible(map, true);
      setMarkersVisible(markersRef.current, true);
    } else {
      // Not focused: keep spinning, overlays + markers hidden.
      spinningRef.current = true;
      setOverlayVisible(map, false);
      setMarkersVisible(markersRef.current, false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus]);

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" />
      {showHud ? (
        <>
          <div className="pointer-events-none absolute left-4 top-4 rounded-md border border-cyan-300/20 bg-slate-950/70 px-3 py-2 backdrop-blur">
            <p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-cyan-200">
              Live Risk Surface
            </p>
            <p className="mt-1 text-xs text-slate-300">
              {result.analysis.total_zones} zones / {result.analysis.affected_area_km2} km2 affected
            </p>
          </div>
          <div className="pointer-events-none absolute bottom-4 left-4 rounded-md border border-violet-300/20 bg-slate-950/70 px-3 py-2 text-xs backdrop-blur">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-violet-200">
              Layer Status
            </p>
            <div className="mt-1 flex flex-wrap gap-1.5">
              <MapChip active={layers.hazardZones} label="zones" />
              <MapChip active={layers.boundary} label="boundary" />
              <MapChip active={layers.evacuationRoutes} label="routes" />
              <MapChip active={layers.facilities} label="facilities" />
            </div>
          </div>
          <div className="map-orbit-guide pointer-events-none absolute right-4 top-24 rounded-md border border-cyan-300/20 bg-slate-950/75 px-3 py-2 backdrop-blur">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-cyan-200">
              Orbit View
            </p>
            <p className="mt-1 text-[10px] leading-4 text-slate-300">
              Right-drag or Ctrl-drag to rotate and tilt
            </p>
            <p className="text-[10px] leading-4 text-slate-500">
              Drag the compass for direct camera control
            </p>
          </div>
        </>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Map layers, markers, interactions
// --------------------------------------------------------------------------- //

function addEventLayers(map: mapboxgl.Map, result: HazardMindResult) {
  // ---- PNG risk-map overlays (true-colour / NDWI index / classification) ----
  // These are the actual raster outputs from the pipeline, draped over the scene
  // footprint (bbox corners). Toggled via the layer panel; hidden by default.
  // PNG/zone overlays go on the real scene footprint (scene_bbox), which may
  // differ from the camera focus bbox (the city centre).
  const bbox = result.boundaries.scene_bbox ?? result.boundaries.bbox;
  if (bbox && bbox.length === 4) {
    const [w, s, e, n] = bbox;
    const corners: [LngLatPair, LngLatPair, LngLatPair, LngLatPair] = [
      [w, n], // top-left
      [e, n], // top-right
      [e, s], // bottom-right
      [w, s], // bottom-left
    ];
    const rasters: Array<{ id: string; url?: string }> = [
      { id: "img-true-color", url: proxied(result.artifacts?.true_color_url) },
      { id: "img-index", url: proxied(result.artifacts?.index_url) },
      { id: "img-classification", url: proxied(result.artifacts?.classification_url) },
    ];
    rasters.forEach(({ id, url }) => {
      if (!url) return;
      map.addSource(id, { type: "image", url, coordinates: corners });
      map.addLayer({
        id,
        type: "raster",
        source: id,
        paint: { "raster-opacity": 0.85, "raster-fade-duration": 300 },
        layout: { visibility: "none" },
      });
    });
  }

  map.addSource("hazard-zones", {
    type: "geojson",
    data: result.analysis.zones as GeoJSON.FeatureCollection,
  });

  // Swap in the REAL zone polygons from the pipeline's zones.geojson (R2).
  if (result.artifacts?.geojson_url) {
    fetch(proxied(result.artifacts.geojson_url) as string)
      .then((r) => (r.ok ? r.json() : null))
      .then((fc: GeoJSON.FeatureCollection | null) => {
        if (!fc) return;
        (map.getSource("hazard-zones") as mapboxgl.GeoJSONSource | undefined)?.setData(fc);
        (map.getSource("hazard-heat") as mapboxgl.GeoJSONSource | undefined)?.setData(
          zonesToWeightedPoints(fc),
        );
      })
      .catch(() => {});
  }
  // ---- Risk heatmap (severity-weighted) ----
  // Generate weighted points from the zone polygons so Mapbox's heatmap layer
  // can render a smooth risk surface (red = critical, fading out at low risk).
  const heatPoints = zonesToWeightedPoints(result.analysis.zones as GeoJSON.FeatureCollection);
  map.addSource("hazard-heat", { type: "geojson", data: heatPoints });
  map.addLayer({
    id: "hazard-heat",
    type: "heatmap",
    source: "hazard-heat",
    paint: {
      "heatmap-weight": ["coalesce", ["get", "weight"], 0.5],
      "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 5, 0.6, 12, 2.2],
      "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 5, 14, 12, 48],
      "heatmap-opacity": ["interpolate", ["linear"], ["zoom"], 5, 0.7, 14, 0.55],
      "heatmap-color": [
        "interpolate",
        ["linear"],
        ["heatmap-density"],
        0, "rgba(20,184,166,0)",
        0.2, "rgba(20,184,166,0.5)",
        0.4, "rgba(250,204,21,0.7)",
        0.6, "rgba(249,115,22,0.8)",
        0.85, "rgba(239,68,68,0.9)",
        1, "rgba(220,38,38,1)",
      ],
    },
  });

  map.addLayer({
    id: "hazard-zones-fill",
    type: "fill",
    source: "hazard-zones",
    paint: {
      "fill-color": [
        "match",
        ["get", "severity"],
        "critical",
        "#ef4444",
        "high",
        "#f97316",
        "medium",
        "#facc15",
        "#14b8a6",
      ],
      "fill-opacity": 0.4,
    },
  });
  map.addLayer({
    id: "hazard-zones-line",
    type: "line",
    source: "hazard-zones",
    paint: { "line-color": "#f8fafc", "line-opacity": 0.86, "line-width": 1.7 },
  });

  // Boundary outline removed by request — the PNG/zone overlays carry the footprint.

  map.addSource("evacuation-routes", {
    type: "geojson",
    data: result.routes.evacuation_routes as GeoJSON.FeatureCollection,
  });
  map.addLayer({
    id: "evacuation-routes-line",
    type: "line",
    source: "evacuation-routes",
    paint: { "line-color": "#a78bfa", "line-width": 3.5, "line-opacity": 0.88 },
  });
}

// Convert zone polygons to severity-weighted centroid points for the heatmap.
function zonesToWeightedPoints(zones: GeoJSON.FeatureCollection): GeoJSON.FeatureCollection {
  const weightFor = (severity: string): number => {
    switch ((severity || "").toLowerCase()) {
      case "critical":
        return 1;
      case "high":
        return 0.75;
      case "medium":
        return 0.5;
      default:
        return 0.28;
    }
  };

  const features: GeoJSON.Feature[] = (zones?.features ?? []).map((feature) => {
    const pts: LngLatPair[] = [];
    collectCoordinates(feature.geometry, pts);
    const lng = pts.reduce((s, p) => s + p[0], 0) / Math.max(pts.length, 1);
    const lat = pts.reduce((s, p) => s + p[1], 0) / Math.max(pts.length, 1);
    const severity = String((feature.properties as { severity?: string })?.severity ?? "");
    return {
      type: "Feature",
      properties: { weight: weightFor(severity) },
      geometry: { type: "Point", coordinates: [lng, lat] },
    };
  });

  return { type: "FeatureCollection", features };
}

// Show/hide the facility pins. Hidden on the idle globe; shown only on focus.
function setMarkersVisible(markers: mapboxgl.Marker[], visible: boolean) {
  markers.forEach((m) => {
    m.getElement().style.display = visible ? "grid" : "none";
  });
}

// Show/hide the analysis overlays (heatmap + zones + boundary + routes).
function setOverlayVisible(map: mapboxgl.Map, visible: boolean) {
  const ids = [
    "hazard-heat",
    "hazard-zones-fill",
    "hazard-zones-line",
    "evacuation-routes-line",
  ];
  ids.forEach((id) => {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    }
  });
}

function attachInteractions(map: mapboxgl.Map) {
  map.on("click", "hazard-zones-fill", (event) => {
    const feature = event.features?.[0];
    if (!feature) {
      return;
    }
    const properties = feature.properties as {
      zone_id?: string;
      severity?: string;
      class_name?: string;
      area_km2?: number;
    };
    new mapboxgl.Popup({ closeButton: false })
      .setLngLat(event.lngLat)
      .setHTML(
        `<strong>${properties.zone_id ?? "Hazard Zone"}</strong><br/>Severity: ${properties.severity ?? "unknown"}<br/>Class: ${properties.class_name ?? "n/a"}<br/>Area: ${properties.area_km2 ?? "n/a"} km2`,
      )
      .addTo(map);
  });

  map.on("mouseenter", "hazard-zones-fill", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "hazard-zones-fill", () => {
    map.getCanvas().style.cursor = "";
  });
}

function addFacilityMarkers(map: mapboxgl.Map, result: HazardMindResult): mapboxgl.Marker[] {
  return result.impact.critical_facilities.map((facility) => {
    const element = document.createElement("div");
    element.className = "facility-marker";
    element.title = `${facility.name} - ${facility.risk}`;
    return new mapboxgl.Marker({ element })
      .setLngLat([facility.lng, facility.lat])
      .setPopup(
        new mapboxgl.Popup({ closeButton: false }).setHTML(
          `<strong>${facility.name}</strong><br/>${facility.type}<br/>Risk: ${facility.risk}`,
        ),
      )
      .addTo(map);
  });
}

// --------------------------------------------------------------------------- //
// Cinematic "rotate then focus" camera move
// --------------------------------------------------------------------------- //

function focusOnEvent(
  map: mapboxgl.Map,
  result: HazardMindResult,
  perspective: boolean,
  onArrive: () => void,
) {
  const bounds = getInitialBounds(result);
  // fitBounds computes a camera centred on the analysis box; we read its target,
  // then fly there so the analysed footprint sits centred and fills the frame.
  const camera = map.cameraForBounds(bounds, {
    padding: { top: 70, bottom: 70, left: 70, right: 70 },
    maxZoom: 13,
  });

  const center = camera?.center ?? boundsCenter(bounds);
  const zoom = Math.max(2, camera?.zoom ?? 11);

  map.flyTo({
    center,
    zoom,
    pitch: perspective ? 45 : 0,
    bearing: 0,
    duration: 4200,
    essential: true,
    curve: 1.5,
  });

  map.once("moveend", onArrive);
}

function boundsCenter(bounds: [LngLatPair, LngLatPair]): LngLatPair {
  return [
    (bounds[0][0] + bounds[1][0]) / 2,
    (bounds[0][1] + bounds[1][1]) / 2,
  ];
}

// --------------------------------------------------------------------------- //
// Bounds helpers (unchanged logic, Mapbox-typed)
// --------------------------------------------------------------------------- //

function getInitialBounds(result: HazardMindResult): [LngLatPair, LngLatPair] {
  const bboxBounds = boundsFromBbox(result.boundaries.bbox);
  if (bboxBounds) {
    return bboxBounds;
  }
  return (
    boundsFromGeoJson(result.boundaries.merged_polygon) ??
    boundsFromGeoJson(result.boundaries.region_boundary) ??
    boundsFromGeoJson(result.analysis.zones) ??
    boundsFromGeoJson(result.routes.evacuation_routes) ??
    [
      [result.boundaries.bbox[0], result.boundaries.bbox[1]],
      [result.boundaries.bbox[2], result.boundaries.bbox[3]],
    ]
  );
}

function boundsFromBbox(bbox: [number, number, number, number]): [LngLatPair, LngLatPair] | null {
  const [west, south, east, north] = bbox;
  if (![west, south, east, north].every(Number.isFinite) || west === east || south === north) {
    return null;
  }
  return [
    [west, south],
    [east, north],
  ];
}

function boundsFromGeoJson(geojson: GeoJSON.GeoJsonObject | null | undefined): [LngLatPair, LngLatPair] | null {
  if (!geojson) {
    return null;
  }
  const points: LngLatPair[] = [];
  collectCoordinates(geojson, points);
  if (!points.length) {
    return null;
  }
  const lngs = points.map(([lng]) => lng);
  const lats = points.map(([, lat]) => lat);
  return [
    [Math.min(...lngs), Math.min(...lats)],
    [Math.max(...lngs), Math.max(...lats)],
  ];
}

function collectCoordinates(value: unknown, points: LngLatPair[]) {
  if (!Array.isArray(value)) {
    if (value && typeof value === "object") {
      Object.values(value).forEach((entry) => collectCoordinates(entry, points));
    }
    return;
  }
  if (
    value.length >= 2 &&
    typeof value[0] === "number" &&
    typeof value[1] === "number" &&
    Number.isFinite(value[0]) &&
    Number.isFinite(value[1])
  ) {
    points.push([value[0], value[1]]);
    return;
  }
  value.forEach((entry) => collectCoordinates(entry, points));
}

// --------------------------------------------------------------------------- //
// Layer visibility
// --------------------------------------------------------------------------- //

function applyVisibility(map: mapboxgl.Map, markers: mapboxgl.Marker[], layers: LayerState) {
  setLayerGroupVisibility(map, ["hazard-heat", "hazard-zones-fill", "hazard-zones-line"], layers.hazardZones);
  // boundary outline removed
  setLayerGroupVisibility(map, ["evacuation-routes-line"], layers.evacuationRoutes);
  // PNG raster risk-map overlays.
  setLayerGroupVisibility(map, ["img-true-color"], layers.satellite);
  setLayerGroupVisibility(map, ["img-index"], layers.index);
  setLayerGroupVisibility(map, ["img-classification"], layers.classification);
  markers.forEach((marker) => {
    marker.getElement().style.display = layers.facilities ? "grid" : "none";
  });
}

function setLayerGroupVisibility(map: mapboxgl.Map, ids: string[], visible: boolean) {
  ids.forEach((id) => {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    }
  });
}

function MapChip({ active, label }: { active: boolean; label: string }) {
  return (
    <span
      className={`rounded border px-1.5 py-0.5 text-[10px] ${active ? "border-cyan-300/30 bg-cyan-300/10 text-cyan-100" : "border-slate-600/40 bg-slate-800/50 text-slate-500"}`}
    >
      {label}
    </span>
  );
}
