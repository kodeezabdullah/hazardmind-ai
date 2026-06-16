"use client";

import { Activity, FileText, Layers3, Radio, RadioTower, Satellite, ShieldAlert, Sparkles } from "lucide-react";
import { useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type WheelEvent } from "react";
import { AgentAvatarNode } from "./AgentAvatarNode";
import { HazardMap } from "./HazardMap";
import { MapLegendRail } from "./MapLegendRail";
import { RotatingDialShell } from "./RotatingDialShell";
import { SelectedModulePanel } from "./SelectedModulePanel";
import type { HazardMindResult, LayerKey, LayerState } from "../lib/types";

const DIAL_SIZE = "clamp(620px, min(82vh, 54vw), 840px)";
const MAP_RATIO = 0.78;
const NODE_RADIUS_RATIO = 0.48;
const WHEEL_THROTTLE_MS = 300;
const ACTIVE_ANGLE = 0;

export type CircularModuleId =
  | "layers"
  | "satellite"
  | "hazard"
  | "risk"
  | "impact"
  | "summary"
  | "report"
  | "timeline";

export type CircularModule = {
  id: CircularModuleId;
  label: string;
  codename?: string;
  type: "control" | "agent" | "analysis" | "brief" | "trace";
  icon: typeof Layers3;
  tone: "cyan" | "red" | "amber" | "violet" | "slate";
  angle: number;
};

const modules: CircularModule[] = [
  { id: "layers", label: "GIS Layers", type: "control", icon: Layers3, tone: "cyan", angle: 0 },
  { id: "satellite", label: "Satellite Agent", codename: "Orbital Eye", type: "agent", icon: Satellite, tone: "cyan", angle: 0 },
  { id: "hazard", label: "Hazard Agent", codename: "Risk Sentinel", type: "agent", icon: RadioTower, tone: "red", angle: 0 },
  { id: "risk", label: "Risk Confidence", type: "analysis", icon: ShieldAlert, tone: "amber", angle: 0 },
  { id: "impact", label: "Impact Agent", codename: "Civic Pulse", type: "agent", icon: Activity, tone: "amber", angle: 0 },
  { id: "summary", label: "Executive Summary", type: "brief", icon: Sparkles, tone: "violet", angle: 0 },
  { id: "report", label: "Report Agent", codename: "Briefing Core", type: "agent", icon: FileText, tone: "violet", angle: 0 },
  { id: "timeline", label: "System Trace", type: "trace", icon: Radio, tone: "slate", angle: 0 },
];

type CircularMapDialProps = {
  currentEventId?: string;
  layers: LayerState;
  result: HazardMindResult;
  onToggleLayer: (layer: LayerKey) => void;
};

export function CircularMapDial({ currentEventId, layers, result, onToggleLayer }: CircularMapDialProps) {
  const [activeIndex, setActiveIndex] = useState(0);
  const [isFocused, setIsFocused] = useState(false);
  const [isDialHot, setIsDialHot] = useState(false);
  const lastWheelAt = useRef(0);
  const mapCoreRef = useRef<HTMLDivElement>(null);
  const moduleCount = modules.length;
  const segmentAngle = 360 / moduleCount;
  const dialRotation = -activeIndex * segmentAngle;

  const positionedModules = useMemo(
    () =>
      modules.map((module, index) => ({
        ...module,
        angle: (index - activeIndex) * segmentAngle + ACTIVE_ANGLE,
        index,
      })),
    [activeIndex, segmentAngle],
  );

  const activeModule = positionedModules[activeIndex];

  function rotateBy(delta: number) {
    setIsFocused(false);
    setActiveIndex((current) => (current + delta + moduleCount) % moduleCount);
  }

  function handleWheel(event: WheelEvent<HTMLElement>) {
    if (mapCoreRef.current?.contains(event.target as Node)) {
      return;
    }

    event.preventDefault();
    const now = Date.now();
    if (now - lastWheelAt.current < WHEEL_THROTTLE_MS) {
      return;
    }
    lastWheelAt.current = now;
    rotateBy(event.deltaY > 0 ? 1 : -1);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      rotateBy(1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      rotateBy(-1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      setIsFocused(true);
    }
  }

  return (
    <section className="circular-command-stage" aria-label="HazardMind circular AI command interface">
      <MapLegendRail layers={layers} onToggleLayer={onToggleLayer} result={result} />

      <div
        className={`circular-map-dial ${isDialHot ? "is-hot" : ""}`}
        onKeyDown={handleKeyDown}
        onMouseEnter={() => setIsDialHot(true)}
        onMouseLeave={() => setIsDialHot(false)}
        role="application"
        style={
          {
            "--dial-size": DIAL_SIZE,
            "--map-size": `calc(var(--dial-size) * ${MAP_RATIO})`,
            "--node-radius": `calc(var(--dial-size) * ${NODE_RADIUS_RATIO})`,
          } as CSSProperties
        }
        tabIndex={0}
      >
        <div className="dial-backglow" />
        <div className="map-dial-core" ref={mapCoreRef}>
          <HazardMap result={result} layers={layers} showHud={false} />
        </div>

        <div className="dial-interaction-ring" aria-hidden="true" onWheel={handleWheel}>
          <span className="dial-wheel-zone dial-wheel-zone-top" />
          <span className="dial-wheel-zone dial-wheel-zone-right" />
          <span className="dial-wheel-zone dial-wheel-zone-bottom" />
          <span className="dial-wheel-zone dial-wheel-zone-left" />
        </div>

        <RotatingDialShell rotation={dialRotation} selectedAngle={ACTIVE_ANGLE - 90} />

        <svg className="dial-connector-field" aria-hidden="true" preserveAspectRatio="none" viewBox="0 0 100 100">
          {positionedModules.map((module) => {
            const radians = (module.angle * Math.PI) / 180;
            return (
              <line
                className={module.index === activeIndex ? "dial-connector is-active" : "dial-connector"}
                key={module.id}
                x1={50 + Math.cos(radians) * 38}
                y1={50 + Math.sin(radians) * 38}
                x2={50 + Math.cos(radians) * 45}
                y2={50 + Math.sin(radians) * 45}
              />
            );
          })}
        </svg>

        <div className="agent-avatar-layer">
          {positionedModules.map((module) => (
            <AgentAvatarNode
              active={module.index === activeIndex}
              angle={module.angle}
              codename={module.codename}
              icon={module.icon}
              isAgent={module.type === "agent"}
              key={module.id}
              label={module.label}
              onClick={() => {
                setActiveIndex(module.index);
                setIsFocused(module.index === activeIndex);
              }}
              tone={module.tone}
              type={module.type}
            />
          ))}
        </div>

        <div className="dial-hover-hint">
          <span>Wheel / arrows rotate</span>
          <span>Enter opens</span>
        </div>
      </div>

      <SelectedModulePanel
        currentEventId={currentEventId}
        isFocused={isFocused}
        layers={layers}
        module={activeModule}
        onCloseFocus={() => setIsFocused(false)}
        onOpenFocus={() => setIsFocused(true)}
        onToggleLayer={onToggleLayer}
        result={result}
      />
    </section>
  );
}
