"use client";

import { useEffect, useState } from "react";
import { AgentNetwork, agentModules, type AgentModuleId } from "./AgentNetwork";
import { CommandHeader } from "./CommandHeader";
import { MapLegendRail } from "./MapLegendRail";
import { MapLibre3DStage } from "./MapLibre3DStage";
import { SelectedModulePanel } from "./SelectedModulePanel";
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
  const [activeAgentId, setActiveAgentId] = useState<AgentModuleId>("satellite");
  const [isFocused, setIsFocused] = useState(false);
  const activeAgent = agentModules.find((agent) => agent.id === activeAgentId) ?? agentModules[0];

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
      <div className="command-bg-grid" />
      <div className="command-bg-glow" />
      <div className="command-scanlines" />

      <div className="command-center-shell">
        <CommandHeader result={result} dataSource={formatDataSource(dataSource)} />

        <section className="command-map-layout">
          <MapLegendRail layers={layers} onToggleLayer={toggleLayer} result={result} />
          <div className="command-map-center">
            <MapLibre3DStage layers={layers} result={result} />
            <AgentNetwork
              activeAgentId={activeAgentId}
              onSelectAgent={(agentId) => {
                setActiveAgentId(agentId);
                setIsFocused(false);
              }}
            />
          </div>
          <SelectedModulePanel
            currentEventId={routeMode === "map" ? eventId : undefined}
            isFocused={isFocused}
            module={activeAgent}
            onCloseFocus={() => setIsFocused(false)}
            onOpenFocus={() => setIsFocused(true)}
            result={result}
          />
        </section>
      </div>
    </main>
  );
}

function formatDataSource(source: HazardResultSource) {
  return source === "backend" ? "Backend" : "Demo fallback";
}
