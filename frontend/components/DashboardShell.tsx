"use client";

import { useEffect, useState } from "react";
import { AgentPanel } from "./AgentPanel";
import { CommandHeader } from "./CommandHeader";
import { CommandInput } from "./CommandInput";
import { HazardMap } from "./HazardMap";
import { LiveFeedPanel } from "./LiveFeedPanel";
import { MapLegendRail } from "./MapLegendRail";
import { runAnalysis } from "../lib/analyze";
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
  const [legendCollapsed, setLegendCollapsed] = useState(true);
  const [agentPanelCollapsed, setAgentPanelCollapsed] = useState(true);
  const [activeQuery, setActiveQuery] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;

    async function loadResult() {
      try {
        const loaded = await loadHazardResult(eventId);
        if (!ignore) {
          setResult(loaded.result);
          setDataSource(loaded.source);
        }
      } catch {
        if (!ignore) {
          setResult(sampleResult);
          setDataSource("demo-fallback");
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
    <main className="command-center-page">
      {/* Full-screen globe — idle spinning, then flies to the event area and
          reveals the heatmap/zones once a query is submitted. */}
      <div className="command-globe-bg">
        <HazardMap
          layers={layers}
          result={result}
          showHud={false}
          focus={activeQuery !== null}
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
              collapsed={agentPanelCollapsed}
              onToggle={() => setAgentPanelCollapsed((v) => !v)}
            />
          </div>
          {/* Center column intentionally empty so the globe shows through. */}
          <div className="command-map-center command-map-center--overlay" />
          {/* Right panel: live logs (top) + agent chat (bottom). */}
          <LiveFeedPanel result={result} />
        </section>

        {/* Bottom Gemini-style command input — always present. */}
        <CommandInput
          onSubmit={(query) => {
            setActiveQuery(query);
            setAgentPanelCollapsed(false); // open the pipeline panel so the user sees the analysis run
            // Run the live backend pipeline (analyze -> poll -> results). When no
            // backend is configured this resolves to the bundled demo result.
            runAnalysis(query)
              .then((loaded) => {
                setResult(loaded.result);
                setDataSource(loaded.source);
              })
              .catch(() => {
                /* keep the current result on failure */
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
