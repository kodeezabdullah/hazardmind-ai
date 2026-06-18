"use client";

import { MapPinned, Radar } from "lucide-react";
import { HazardMap } from "./HazardMap";
import type { HazardMindResult, LayerState } from "../lib/types";

type MapLibre3DStageProps = {
  layers: LayerState;
  result: HazardMindResult;
};

export function MapLibre3DStage({ layers, result }: MapLibre3DStageProps) {
  return (
    <section className="map-3d-stage" aria-label="3D event map stage">
      <div className="map-3d-header">
        <div>
          <p className="hud-eyebrow">3D GIS command map</p>
          <h2>{result.location}</h2>
        </div>
        <div className="map-3d-stat">
          <Radar className="h-4 w-4 text-cyan-200" />
          <span>{result.analysis.total_zones} zones</span>
        </div>
      </div>

      <div className="map-3d-frame">
        <HazardMap layers={layers} perspective result={result} showHud />
        <div className="map-3d-badge">
          <MapPinned className="h-4 w-4 text-cyan-200" />
          <span>{result.analysis.affected_area_km2} km2 affected</span>
        </div>
      </div>
    </section>
  );
}
