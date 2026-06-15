"use client";

import { AlertTriangle, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { AgentTimeline } from "./AgentTimeline";
import { CommandHeader } from "./CommandHeader";
import { ControlPanel } from "./ControlPanel";
import { HazardMap } from "./HazardMap";
import { IntelligencePanel } from "./IntelligencePanel";
import { loadHazardResult, type HazardResultSource } from "../lib/loadHazardResult";
import { sampleResult } from "../lib/sampleResult";
import type { HazardMindResult, LayerKey, LayerState } from "../lib/types";

type DashboardShellProps = {
  eventId?: string;
  routeMode?: "home" | "map";
};

const initialLayers: LayerState = {
  hazardZones: true,
  boundary: true,
  facilities: true,
  evacuationRoutes: true,
  satellite: false,
  index: false,
  classification: false,
};

export function DashboardShell({ eventId = "demo-peshawar-flood", routeMode = "home" }: DashboardShellProps) {
  const [layers, setLayers] = useState<LayerState>(initialLayers);
  const [result, setResult] = useState<HazardMindResult>(sampleResult);
  const [dataSource, setDataSource] = useState<HazardResultSource>("demo-fallback");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let ignore = false;

    async function loadResult() {
      setIsLoading(true);
      setError("");
      try {
        const loaded = await loadHazardResult(eventId);
        if (!ignore) {
          setResult(loaded.result);
          setDataSource(loaded.source);
          setWarnings(loaded.warnings);
        }
      } catch (loadError) {
        if (!ignore) {
          setResult(sampleResult);
          setDataSource("demo-fallback");
          setError(loadError instanceof Error ? loadError.message : "Unable to load hazard result.");
        }
      } finally {
        if (!ignore) {
          setIsLoading(false);
        }
      }
    }

    loadResult();

    return () => {
      ignore = true;
    };
  }, [eventId]);

  function toggleLayer(layer: LayerKey) {
    setLayers((current) => ({
      ...current,
      [layer]: !current[layer],
    }));
  }

  return (
    <main className="min-h-screen overflow-hidden bg-[#050B14] text-slate-100">
      <div className="command-grid fixed inset-0 opacity-35" />
      <div className="scanline fixed inset-0 pointer-events-none" />

      <div className="relative z-10 flex h-screen flex-col gap-2 p-2.5 pb-16 xl:p-3 xl:pb-16">
        <CommandHeader result={result} dataSource={formatDataSource(dataSource)} />

        {(routeMode === "map" || isLoading || warnings.length > 0 || error) ? (
          <div className="glass-panel flex shrink-0 flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs text-slate-300">
            <div className="flex min-w-0 items-center gap-2">
              {isLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin text-cyan-200" /> : null}
              <span className="font-mono uppercase tracking-[0.14em] text-cyan-100">
                Event {eventId}
              </span>
              <span className="text-slate-500">/</span>
              <span>{isLoading ? "Loading result" : `Data source: ${formatDataSource(dataSource)}`}</span>
            </div>
            {error ? (
              <span className="flex items-center gap-1.5 text-red-200">
                <AlertTriangle className="h-3.5 w-3.5" />
                {error}
              </span>
            ) : warnings.length ? (
              <span className="flex min-w-0 items-center gap-1.5 text-amber-100">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{warnings[0]}</span>
              </span>
            ) : null}
          </div>
        ) : null}

        <section className="grid min-h-0 flex-1 grid-cols-1 gap-2 lg:grid-cols-[280px_minmax(0,1fr)_350px] xl:grid-cols-[300px_minmax(0,1fr)_370px]">
          <ControlPanel result={result} layers={layers} onToggleLayer={toggleLayer} />

          <div className="glass-panel relative min-h-[420px] overflow-hidden">
            <HazardMap result={result} layers={layers} />
          </div>

          <IntelligencePanel result={result} currentEventId={routeMode === "map" ? eventId : undefined} />
        </section>

        <AgentTimeline entries={result.agent_log} />
      </div>
    </main>
  );
}

function formatDataSource(source: HazardResultSource) {
  return source === "backend" ? "Backend" : "Demo fallback";
}
