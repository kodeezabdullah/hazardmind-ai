import { Crosshair, Database, Layers3, Satellite } from "lucide-react";
import { LayerControls } from "./LayerControls";
import type { HazardMindResult, LayerKey, LayerState } from "../lib/types";

type ControlPanelProps = {
  result: HazardMindResult;
  layers: LayerState;
  onToggleLayer: (layer: LayerKey) => void;
};

export function ControlPanel({ result, layers, onToggleLayer }: ControlPanelProps) {
  return (
    <aside className="glass-panel thin-scrollbar min-h-0 overflow-y-auto p-3">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <p className="text-[9px] font-semibold uppercase tracking-[0.22em] text-cyan-300">
            Mission Control
          </p>
          <h2 className="mt-0.5 text-base font-semibold text-slate-50">GIS Layer Stack</h2>
        </div>
        <Crosshair className="h-5 w-5 text-cyan-200" />
      </div>

      <div className="panel-section space-y-1.5 p-2.5">
        <InfoRow label="Event" value={result.event_id} />
        <InfoRow label="Location" value={result.location} />
        <InfoRow label="Hazard" value={result.hazard_type} />
        <InfoRow label="Severity" value={result.overall_severity} emphasis />
      </div>

      <div className="my-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-100">
          <Layers3 className="h-4 w-4 text-cyan-200" />
          Map Layers
        </div>
        <span className="status-chip">live layer</span>
      </div>
      <LayerControls artifacts={result.artifacts} layers={layers} onToggleLayer={onToggleLayer} />

      <section className="panel-section mt-3 p-2.5">
        <div className="mb-2 flex items-center justify-between gap-2 text-sm font-semibold text-slate-100">
          <span className="flex items-center gap-2">
            <Satellite className="h-4 w-4 text-cyan-200" />
            Satellite Intake
          </span>
          <span className="status-chip">active</span>
        </div>
        <div className="space-y-1.5 text-xs text-slate-300">
          <InfoRow label="Sensor" value={result.satellite.type.toUpperCase()} compact />
          <InfoRow label="Cloud" value={`${result.satellite.cloud_cover}%`} compact />
          <InfoRow label="Scene" value={result.satellite.scene_id} compact />
        </div>
      </section>

      <section className="panel-section mt-3 border-violet-300/15 bg-violet-400/[0.04] p-2.5">
        <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-100">
          <Database className="h-4 w-4 text-violet-200" />
          Analysis Signal
        </div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <Metric label="Index" value={result.analysis.index_type} />
          <Metric label="Zones" value={String(result.analysis.total_zones)} />
          <Metric label="Area" value={`${result.analysis.affected_area_km2} km2`} />
          <Metric label="Damage" value={`${result.analysis.damage_percent}%`} />
        </div>
      </section>
    </aside>
  );
}

function InfoRow({
  label,
  value,
  emphasis = false,
  compact = false,
}: {
  label: string;
  value: string;
  emphasis?: boolean;
  compact?: boolean;
}) {
  return (
    <div className={`flex items-center justify-between gap-3 ${compact ? "" : "text-xs"}`}>
      <span className="shrink-0 text-slate-500">{label}</span>
      <span className={`min-w-0 truncate text-right font-medium ${emphasis ? "text-red-200" : "text-slate-100"}`}>
        {value}
      </span>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-white/10 bg-white/[0.03] p-2">
      <span className="block text-[9px] uppercase tracking-[0.14em] text-slate-500">{label}</span>
      <span className="mt-1 block truncate font-semibold text-slate-100">{value}</span>
    </div>
  );
}
