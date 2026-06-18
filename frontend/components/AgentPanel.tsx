"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  FileText,
  Loader2,
  Radar,
  Satellite,
  Users,
  Workflow,
  X,
} from "lucide-react";
import type { HazardMindResult } from "../lib/types";

type StageStatus = "idle" | "running" | "complete";

const STEPS = [
  {
    id: "orchestrator",
    label: "Orchestrator",
    icon: Workflow,
    running: "Dispatching the satellite team and coordinating the pipeline...",
    done: "All agents coordinated.",
  },
  {
    id: "satellite",
    label: "Satellite",
    icon: Satellite,
    running: "Resolving the area boundary and pulling the latest Sentinel-2 scene, computing NDWI...",
    done: "Imagery analysed — water extent measured, zones vectorized.",
  },
  {
    id: "hazard",
    label: "Hazard",
    icon: Radar,
    running: "Classifying flood (NDWI), earthquake (USGS) and landslide (DEM) risk...",
    done: "Multi-hazard risk levels assigned from real data.",
  },
  {
    id: "impact",
    label: "Impact",
    icon: Users,
    running: "Estimating exposed population and infrastructure from GeoNames...",
    done: "Impact assessment complete.",
  },
  {
    id: "report",
    label: "Report",
    icon: FileText,
    running: "Generating the executive report and map, uploading to storage...",
    done: "Executive report ready.",
  },
] as const;

type AgentPanelProps = {
  query: string | null;
  result: HazardMindResult;
  collapsed?: boolean;
  onToggle?: () => void;
};

export function AgentPanel({ query, result, collapsed = false, onToggle }: AgentPanelProps) {
  // activeStep: -1 idle, 0..STEPS.length-1 running that step, >=length all done.
  const [activeStep, setActiveStep] = useState(-1);
  const timersRef = useRef<number[]>([]);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    timersRef.current.forEach((t) => window.clearTimeout(t));
    timersRef.current = [];
    if (!query) {
      setActiveStep(-1);
      return;
    }
    setActiveStep(0);
    for (let i = 0; i < STEPS.length; i += 1) {
      const t = window.setTimeout(() => setActiveStep(i + 1), (i + 1) * 2400);
      timersRef.current.push(t);
    }
    return () => timersRef.current.forEach((t) => window.clearTimeout(t));
  }, [query]);

  // Auto-scroll to the latest step / result.
  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight, behavior: "smooth" });
  }, [activeStep]);

  const statusFor = (index: number): StageStatus => {
    if (!query || activeStep < 0) return "idle";
    if (index < activeStep) return "complete";
    if (index === activeStep) return "running";
    return "idle";
  };

  const allDone = query !== null && activeStep >= STEPS.length;
  const running = query !== null && activeStep >= 0 && activeStep < STEPS.length;

  if (collapsed) {
    return (
      <button
        type="button"
        className={`agent-fab ${running ? "is-running" : ""}`}
        onClick={onToggle}
        aria-label="Show analysis"
        title="Analysis pipeline"
      >
        <Cpu className="h-5 w-5" />
        {running ? <span className="agent-fab-pulse" /> : null}
      </button>
    );
  }

  return (
    <aside className="agent-panel" aria-label="Analysis pipeline">
      <header className="agent-panel-header">
        <span className="agent-panel-icon">
          <Cpu className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <p className="hud-eyebrow">multi-agent</p>
          <h2>Analysis</h2>
        </div>
        {onToggle ? (
          <button type="button" className="agent-panel-close" onClick={onToggle} aria-label="Hide">
            <X className="h-4 w-4" />
          </button>
        ) : null}
      </header>

      {query ? (
        <div className="agent-query-chip" title={query}>
          {query}
        </div>
      ) : null}

      <div ref={bodyRef} className="thin-scrollbar agent-panel-body">
        {!query ? (
          <p className="agent-panel-idle">Submit a query below to start the analysis.</p>
        ) : (
          <>
            <ol className="agent-steps">
              {STEPS.map((step, index) => {
                const status = statusFor(index);
                const Icon = step.icon;
                return (
                  <li key={step.id} className={`agent-step status-${status} tone-${step.id}`}>
                    <span className="agent-step-rail">
                      <span className="agent-step-icon">
                        {status === "running" ? (
                          <Loader2 className="h-3.5 w-3.5 agent-spin" />
                        ) : status === "complete" ? (
                          <CheckCircle2 className="h-3.5 w-3.5" />
                        ) : (
                          <Icon className="h-3.5 w-3.5" />
                        )}
                      </span>
                      {index < STEPS.length - 1 ? <span className="agent-step-line" /> : null}
                    </span>
                    <div className="agent-step-text">
                      <div className="agent-step-top">
                        <strong>{step.label}</strong>
                        <span className={`agent-step-tag status-${status}`}>
                          {status === "running" ? "Running" : status === "complete" ? "Done" : "Queued"}
                        </span>
                      </div>
                      <p className="agent-step-msg">
                        {status === "running" ? step.running : status === "complete" ? step.done : "Waiting..."}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ol>

            {allDone ? <FinalResult result={result} /> : null}
          </>
        )}
      </div>
    </aside>
  );
}

function FinalResult({ result }: { result: HazardMindResult }) {
  const severity = (result.overall_severity || "LOW").toUpperCase();
  const isAllClear = ["LOW", "NONE", "MINIMAL", "NEGLIGIBLE"].includes(severity);

  return (
    <section className={`agent-result ${isAllClear ? "is-clear" : "is-alert"}`}>
      <div className="agent-result-head">
        {isAllClear ? <CheckCircle2 className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
        <span>{isAllClear ? "All clear" : "Disaster detected"}</span>
      </div>

      <p className="agent-result-verdict">
        {result.hazard_type} risk in {result.location}:{" "}
        <strong>{severity}</strong>
      </p>

      <div className="agent-result-stats">
        <Stat label="Severity" value={severity} />
        <Stat label="Affected area" value={`${result.analysis.affected_area_km2} km2`} />
        <Stat label="Zones" value={String(result.analysis.total_zones)} />
        <Stat
          label="People affected"
          value={Number(result.impact.population_affected ?? 0).toLocaleString()}
        />
      </div>

      {result.report?.summary ? (
        <p className="agent-result-summary">{result.report.summary}</p>
      ) : null}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="agent-result-stat">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
