"use client";

import { useEffect, useState } from "react";
import { AgentPanel } from "./AgentPanel";
import { CommandHeader } from "./CommandHeader";
import { CommandInput } from "./CommandInput";
import { HazardMap } from "./HazardMap";
import { LiveFeedPanel } from "./LiveFeedPanel";
import { MapLegendRail } from "./MapLegendRail";
import { runAnalysis } from "../lib/analyze";
import { loadBandLog, type BandMessage } from "../lib/bandLog";
import { loadHazardResult, type HazardResultSource } from "../lib/loadHazardResult";
import { emptyResult } from "../lib/sampleResult";
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

export function DashboardShell({ routeMode = "home" }: DashboardShellProps) {
  const [layers, setLayers] = useState<LayerState>(initialLayers);
  // Start blank — no city, no data. Real numbers/links appear only after a
  // query's backend result lands. (No demo/Rawalpindi data ever shown.)
  const [result, setResult] = useState<HazardMindResult>(emptyResult);
  const [dataSource, setDataSource] = useState<HazardResultSource>("backend");
  const [legendCollapsed, setLegendCollapsed] = useState(true);
  const [agentPanelCollapsed, setAgentPanelCollapsed] = useState(true);
  const [activeQuery, setActiveQuery] = useState<string | null>(null);
  const [bandLog, setBandLog] = useState<BandMessage[]>([]);
  const [pipelineComplete, setPipelineComplete] = useState(false);
  const [pipelineStep, setPipelineStep] = useState<string | null>(null);
  // The globe only flies to the event once the REAL result for THIS query has
  // arrived — otherwise it would zoom to the stale (previous) result's location.
  const [resultReady, setResultReady] = useState(false);

  // Idle on mount: the globe spins and waits for a query. Each query triggers a
  // fresh, independent backend event (parallel-safe).

  function toggleLayer(layer: LayerKey) {
    setLayers((current) => ({
      ...current,
      [layer]: !current[layer],
    }));
  }

  return (
    <main className="command-center-page">
      {/* Full-screen globe — idle spinning, then flies to the event area and
          reveals the heatmap/zones once a query is submitted. */}
      <div className="command-globe-bg">
        <HazardMap
          layers={layers}
          result={result}
          showHud={false}
          focus={resultReady}
        />
      </div>

      <div className="command-bg-grid" />
      <div className="command-bg-glow" />
      <div className="command-scanlines" />

      {/* All UI floats on top of the globe. */}
      <div className="command-center-shell command-center-shell--overlay">
        <CommandHeader result={result} dataSource={formatDataSource(dataSource)} />

        <section className="command-map-layout command-map-layout--overlay">
          {/* Left column: two collapsible panels (layers + agent pipeline). */}
          <div className="command-left-stack">
            <MapLegendRail
              layers={layers}
              onToggleLayer={toggleLayer}
              result={result}
              collapsed={legendCollapsed}
              onToggle={() => setLegendCollapsed((v) => !v)}
            />
            <AgentPanel
              query={activeQuery}
              result={result}
              activeAgents={Array.from(new Set(bandLog.map((m) => m.agent)))}
              complete={pipelineComplete}
              collapsed={agentPanelCollapsed}
              onToggle={() => setAgentPanelCollapsed((v) => !v)}
            />
          </div>
          {/* Center column intentionally empty so the globe shows through. */}
          <div className="command-map-center command-map-center--overlay" />
          {/* Right panel: live logs (top) + agent chat (bottom). */}
          <LiveFeedPanel
            result={result}
            bandLog={bandLog}
            active={activeQuery !== null}
            step={pipelineStep}
            complete={pipelineComplete}
          />
        </section>

        {/* Bottom Gemini-style command input — always present. */}
        <CommandInput
          onSubmit={(query) => {
            setActiveQuery(query);
            setAgentPanelCollapsed(false); // open the pipeline panel so the user sees the analysis run
            setBandLog([]);
            setPipelineComplete(false);
            setPipelineStep("received");
            setResultReady(false); // globe keeps spinning until this query's real result lands
            // Trigger a FRESH, independent backend event (POST /analyze -> new
            // job id), then stream that job's real Band conversation + status and
            // load its result when complete. Each query is its own event, so two
            // queries never collide on one event.
            runAnalysis(query, {
              onBandLog: (messages) => setBandLog(messages),
              onProgress: (p) => {
                if (p.step) setPipelineStep(p.step);
                if (p.status === "complete" || p.status === "failed") setPipelineComplete(true);
              },
            })
              .then((loaded) => {
                setResult(loaded.result);
                setDataSource(loaded.source);
                setPipelineComplete(true);
                setResultReady(true); // now the globe flies to the real event location
              })
              .catch(() => {
                /* keep showing whatever streamed so far */
              });
          }}
        />
      </div>
    </main>
  );
}

function formatDataSource(source: HazardResultSource) {
  return source === "backend" ? "Backend" : "Demo fallback";
}
