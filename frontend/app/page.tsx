"use client";

import { useEffect, useState } from "react";
import { AgentTimeline } from "../components/AgentTimeline";
import { CommandHeader } from "../components/CommandHeader";
import { ControlPanel } from "../components/ControlPanel";
import { HazardMap } from "../components/HazardMap";
import { IntelligencePanel } from "../components/IntelligencePanel";
import { sampleResult } from "../lib/sampleResult";
import type { HazardMindResult, LayerKey, LayerState } from "../lib/types";

const initialLayers: LayerState = {
  hazardZones: true,
  boundary: true,
  facilities: true,
  evacuationRoutes: true,
  satellite: false,
  index: false,
  classification: false,
};

const reportJsonPath = "/demo-results/demo-peshawar-flood.json";

export default function Home() {
  const [layers, setLayers] = useState<LayerState>(initialLayers);
  const [result, setResult] = useState<HazardMindResult>(sampleResult);
  const [dataSource, setDataSource] = useState<"Report Agent JSON" | "Local fallback">(
    "Local fallback",
  );

  useEffect(() => {
    let ignore = false;

    async function loadReportJson() {
      try {
        const response = await fetch(reportJsonPath, { cache: "no-store" });
        if (!response.ok) {
          throw new Error(`Report JSON returned ${response.status}`);
        }

        const json = (await response.json()) as HazardMindResult;
        if (!ignore) {
          setResult(json);
          setDataSource("Report Agent JSON");
        }
      } catch {
        if (!ignore) {
          setResult(sampleResult);
          setDataSource("Local fallback");
        }
      }
    }

    loadReportJson();

    return () => {
      ignore = true;
    };
  }, []);

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
        <CommandHeader result={result} dataSource={dataSource} />

        <section className="grid min-h-0 flex-1 grid-cols-1 gap-2 lg:grid-cols-[280px_minmax(0,1fr)_350px] xl:grid-cols-[300px_minmax(0,1fr)_370px]">
          <ControlPanel result={result} layers={layers} onToggleLayer={toggleLayer} />

          <div className="glass-panel relative min-h-[420px] overflow-hidden">
            <HazardMap result={result} layers={layers} />
          </div>

          <IntelligencePanel result={result} />
        </section>

        <AgentTimeline entries={result.agent_log} />
      </div>
    </main>
  );
}
