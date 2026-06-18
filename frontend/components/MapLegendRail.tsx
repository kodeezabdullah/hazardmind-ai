"use client";

import { Layers3, MapPinned, Radar, X } from "lucide-react";
import { LayerControls } from "./LayerControls";
import type { HazardMindResult, LayerKey, LayerState } from "../lib/types";

type MapLegendRailProps = {
  layers: LayerState;
  result: HazardMindResult;
  onToggleLayer: (layer: LayerKey) => void;
  collapsed?: boolean;
  onToggle?: () => void;
};

const legendItems = [
  ["Critical", "bg-red-500"],
  ["High", "bg-orange-500"],
  ["Medium", "bg-yellow-300"],
  ["Low", "bg-teal-400"],
] as const;

export function MapLegendRail({ layers, result, onToggleLayer, collapsed = false, onToggle }: MapLegendRailProps) {
  // Collapsed: just a floating layers icon. Click to expand the full rail.
  if (collapsed) {
    return (
      <button
        type="button"
        className="map-legend-fab"
        onClick={onToggle}
        aria-label="Show GIS legend and layer controls"
        title="Layers & legend"
      >
        <Layers3 className="h-5 w-5" />
      </button>
    );
  }

  return (
    <aside className="map-legend-rail" aria-label="Map intelligence and GIS legend">
      <header className="map-legend-header">
        <span className="map-legend-icon">
          <MapPinned className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <p className="hud-eyebrow">map intelligence</p>
          <h2>GIS Legend</h2>
        </div>
        {onToggle ? (
          <button
            type="button"
            className="map-legend-close"
            onClick={onToggle}
            aria-label="Hide legend"
            title="Hide"
          >
            <X className="h-4 w-4" />
          </button>
        ) : null}
      </header>

      <section className="map-rail-section">
        <div className="map-rail-title">
          <Layers3 className="h-3.5 w-3.5" />
          Layer Controls
        </div>
        <LayerControls artifacts={result.artifacts} layers={layers} onToggleLayer={onToggleLayer} />
      </section>

      <section className="map-rail-section">
        <div className="map-rail-title">
          <Radar className="h-3.5 w-3.5" />
          Risk Colour Legend
        </div>
        <div className="grid gap-1.5">
          {legendItems.map(([label, color]) => (
            <div className="map-legend-row" key={label}>
              <span className={`h-2.5 w-2.5 rounded-full ${color} shadow-[0_0_12px_currentColor]`} />
              <span>{label}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="map-rail-section">
        <div className="map-rail-title">Live Risk Surface</div>
        <div className="grid grid-cols-2 gap-2">
          <RailMetric label="Zones" value={String(result.analysis.total_zones)} />
          <RailMetric label="Area" value={`${result.analysis.affected_area_km2} km2`} />
          <RailMetric label="Damage" value={`${result.analysis.damage_percent}%`} />
          <RailMetric label="Index" value={result.analysis.index_type.toUpperCase()} />
        </div>
      </section>
    </aside>
  );
}

function RailMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="mini-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
