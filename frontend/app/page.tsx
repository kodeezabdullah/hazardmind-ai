"use client";

import { useState } from "react";
import { AgentTimeline } from "../components/AgentTimeline";
import { CommandHeader } from "../components/CommandHeader";
import { ControlPanel } from "../components/ControlPanel";
import { HazardMap } from "../components/HazardMap";
import { IntelligencePanel } from "../components/IntelligencePanel";
import { sampleResult } from "../lib/sampleResult";
import type { LayerKey, LayerState } from "../lib/types";

const initialLayers: LayerState = {
  hazardZones: true,
  boundary: true,
  facilities: true,
  evacuationRoutes: true,
  satellite: false,
  index: false,
  classification: false,
};

export default function Home() {
  const [layers, setLayers] = useState<LayerState>(initialLayers);

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

      <div className="relative z-10 flex h-screen flex-col gap-2 p-2.5 xl:p-3">
        <CommandHeader result={sampleResult} />

        <section className="grid min-h-0 flex-1 grid-cols-1 gap-2 lg:grid-cols-[280px_minmax(0,1fr)_350px] xl:grid-cols-[300px_minmax(0,1fr)_370px]">
          <ControlPanel result={sampleResult} layers={layers} onToggleLayer={toggleLayer} />

          <div className="glass-panel relative min-h-[420px] overflow-hidden">
            <HazardMap result={sampleResult} layers={layers} />
          </div>

          <IntelligencePanel result={sampleResult} />
        </section>

        <AgentTimeline entries={sampleResult.agent_log} />
      </div>
    </main>
  );
}
