"use client";

import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import type { HazardMindResult, LayerState } from "../lib/types";

type HazardMapProps = {
  result: HazardMindResult;
  layers: LayerState;
};

const mapStyle: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    cartoDark: {
      type: "raster",
      tiles: [
        "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      attribution: "&copy; OpenStreetMap contributors &copy; CARTO",
    },
  },
  layers: [
    {
      id: "carto-dark",
      type: "raster",
      source: "cartoDark",
      minzoom: 0,
      maxzoom: 20,
    },
  ],
};

export function HazardMap({ result, layers }: HazardMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const loadedRef = useRef(false);
  const markersRef = useRef<maplibregl.Marker[]>([]);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) {
      return;
    }

    const [west, south, east, north] = result.boundaries.bbox;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: mapStyle,
      center: [(west + east) / 2, (south + north) / 2],
      zoom: 10,
      attributionControl: false,
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");

    map.once("load", () => {
      loadedRef.current = true;

      map.addSource("hazard-zones", {
        type: "geojson",
        data: result.analysis.zones as GeoJSON.FeatureCollection,
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
          "fill-opacity": 0.48,
        },
      });
      map.addLayer({
        id: "hazard-zones-line",
        type: "line",
        source: "hazard-zones",
        paint: {
          "line-color": "#f8fafc",
          "line-opacity": 0.86,
          "line-width": 1.7,
        },
      });

      map.addSource("boundary", {
        type: "geojson",
        data: result.boundaries.merged_polygon as GeoJSON.Feature,
      });
      map.addLayer({
        id: "boundary-fill",
        type: "fill",
        source: "boundary",
        paint: {
          "fill-color": "#22d3ee",
          "fill-opacity": 0.06,
        },
      });
      map.addLayer({
        id: "boundary-line",
        type: "line",
        source: "boundary",
        paint: {
          "line-color": "#67e8f9",
          "line-dasharray": [2, 2],
          "line-width": 2,
        },
      });

      map.addSource("evacuation-routes", {
        type: "geojson",
        data: result.routes.evacuation_routes as GeoJSON.FeatureCollection,
      });
      map.addLayer({
        id: "evacuation-routes-line",
        type: "line",
        source: "evacuation-routes",
        paint: {
          "line-color": "#a78bfa",
          "line-width": 3.5,
          "line-opacity": 0.88,
        },
      });

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

        new maplibregl.Popup({ closeButton: false })
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

      markersRef.current = result.impact.critical_facilities.map((facility) => {
        const element = document.createElement("div");
        element.className = "facility-marker";
        element.title = `${facility.name} - ${facility.risk}`;
        return new maplibregl.Marker({ element })
          .setLngLat([facility.lng, facility.lat])
          .setPopup(
            new maplibregl.Popup({ closeButton: false }).setHTML(
              `<strong>${facility.name}</strong><br/>${facility.type}<br/>Risk: ${facility.risk}`,
            ),
          )
          .addTo(map);
      });

      map.fitBounds(
        [
          [west, south],
          [east, north],
        ],
        { padding: 54, duration: 0 },
      );

      applyVisibility(map, markersRef.current, layers);
    });

    mapRef.current = map;

    return () => {
      markersRef.current.forEach((marker) => marker.remove());
      markersRef.current = [];
      loadedRef.current = false;
      map.remove();
      mapRef.current = null;
    };
  }, [result]);

  useEffect(() => {
    if (!mapRef.current || !loadedRef.current) {
      return;
    }
    applyVisibility(mapRef.current, markersRef.current, layers);
  }, [layers]);

  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" />
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
    </div>
  );
}

function MapChip({ active, label }: { active: boolean; label: string }) {
  return (
    <span className={`rounded border px-1.5 py-0.5 text-[10px] ${active ? "border-cyan-300/30 bg-cyan-300/10 text-cyan-100" : "border-slate-600/40 bg-slate-800/50 text-slate-500"}`}>
      {label}
    </span>
  );
}

function applyVisibility(
  map: maplibregl.Map,
  markers: maplibregl.Marker[],
  layers: LayerState,
) {
  setLayerGroupVisibility(map, ["hazard-zones-fill", "hazard-zones-line"], layers.hazardZones);
  setLayerGroupVisibility(map, ["boundary-fill", "boundary-line"], layers.boundary);
  setLayerGroupVisibility(map, ["evacuation-routes-line"], layers.evacuationRoutes);
  markers.forEach((marker) => {
    marker.getElement().style.display = layers.facilities ? "grid" : "none";
  });
}

function setLayerGroupVisibility(map: maplibregl.Map, ids: string[], visible: boolean) {
  ids.forEach((id) => {
    if (map.getLayer(id)) {
      map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    }
  });
}
