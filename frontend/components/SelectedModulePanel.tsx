"use client";

import { Activity, CheckCircle2, FileText, RadioTower, Satellite } from "lucide-react";
import type { ReactNode } from "react";
import { DialFocusedPanel } from "./DialFocusedPanel";
import { ReportActions } from "./ReportActions";
import { StatsGrid } from "./StatsGrid";
import type { HazardMindResult } from "../lib/types";
import type { AgentModule } from "./AgentNetwork";

type SelectedModulePanelProps = {
  currentEventId?: string;
  isFocused: boolean;
  module: AgentModule;
  result: HazardMindResult;
  onCloseFocus: () => void;
  onOpenFocus: () => void;
};

const moduleIcons = {
  satellite: Satellite,
  hazard: RadioTower,
  impact: Activity,
  report: FileText,
};

export function SelectedModulePanel({
  currentEventId,
  isFocused,
  module,
  result,
  onCloseFocus,
  onOpenFocus,
}: SelectedModulePanelProps) {
  const Icon = moduleIcons[module.id];

  return (
    <>
      <section className="selected-module-panel" aria-label={`${module.label} selected module`}>
        <header className="selected-module-header">
          <span className={`selected-module-avatar is-${module.tone}`}>
            <Icon className="h-5 w-5" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="hud-eyebrow">selected {module.type}</p>
            <h2>{module.label}</h2>
            <span>{module.codename ?? "Command module"}</span>
          </div>
          <button className="selected-module-expand" onClick={onOpenFocus} type="button">
            expand
          </button>
        </header>

        <div className="thin-scrollbar selected-module-body">
          {renderModuleContent(module.id, result, currentEventId)}
        </div>
      </section>

      {isFocused ? (
        <DialFocusedPanel moduleLabel={module.label} onClose={onCloseFocus}>
          {renderFocusedContent(module.id, result, currentEventId)}
        </DialFocusedPanel>
      ) : null}
    </>
  );
}

function renderModuleContent(
  id: AgentModule["id"],
  result: HazardMindResult,
  currentEventId?: string,
) {
  if (id === "satellite") {
    return (
      <ModuleBlock title="Satellite Intake">
        <InfoGrid
          items={[
            ["Sensor", result.satellite.type.toUpperCase()],
            ["Cloud cover", `${result.satellite.cloud_cover}%`],
            ["Scene ID", result.satellite.scene_id],
            ["Classification", result.artifacts.classification_url ? "active" : "pending artifact"],
          ]}
        />
        <p className="mt-3 text-sm leading-6 text-slate-300">{result.satellite.reason}</p>
      </ModuleBlock>
    );
  }

  if (id === "hazard") {
    return (
      <ModuleBlock title="Hazard Classification">
        <InfoGrid
          items={[
            ["Overall severity", result.overall_severity],
            ["Flood", result.hazard.flood_risk],
            ["Earthquake", result.hazard.earthquake_risk],
            ["Landslide", result.hazard.landslide_risk],
          ]}
        />
        <RiskBars result={result} />
      </ModuleBlock>
    );
  }

  if (id === "impact") {
    return (
      <ModuleBlock title="Impact Assessment">
        <StatsGrid result={result} />
        <InfoGrid
          className="mt-3"
          items={[
            ["Vulnerability", String(result.impact.vulnerability_score)],
            ["Facilities", String(result.impact.critical_facilities.length)],
            ["Routes", `${result.routes.evacuation_routes.features.length} active`],
            ["Area", `${result.analysis.affected_area_km2} km2`],
          ]}
        />
      </ModuleBlock>
    );
  }

  return (
    <ModuleBlock title="Report Output">
      <ReportActions result={result} currentEventId={currentEventId} />
      <InfoGrid
        className="mt-3"
        items={[
          ["PDF", result.report.pdf_url ? "ready" : "pending"],
          ["Map", result.report.map_url ? "ready" : "pending"],
          ["Recommendations", String(result.report.recommendations.length)],
          ["Package", "local demo"],
        ]}
      />
      <h3 className="mt-4 text-xs font-semibold uppercase tracking-[0.18em] text-cyan-100">Executive summary</h3>
      <p className="mt-2 text-sm leading-6 text-slate-300">{result.report.summary}</p>
      <h3 className="mt-4 text-xs font-semibold uppercase tracking-[0.18em] text-cyan-100">Immediate priorities</h3>
      <BulletList items={result.report.recommendations.slice(0, 3)} />
    </ModuleBlock>
  );
}

function renderFocusedContent(
  id: AgentModule["id"],
  result: HazardMindResult,
  currentEventId?: string,
) {
  return (
    <div className="grid gap-4">
      {renderModuleContent(id, result, currentEventId)}
      {id !== "report" ? (
        <ModuleBlock title="Executive Context">
          <p className="text-sm leading-6 text-slate-300">{result.report.summary}</p>
        </ModuleBlock>
      ) : null}
    </div>
  );
}

function ModuleBlock({ children, title }: { children: ReactNode; title: string }) {
  return (
    <section className="module-content-block">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function InfoGrid({ items, className = "" }: { items: Array<[string, string]>; className?: string }) {
  return (
    <div className={`grid grid-cols-2 gap-2 ${className}`}>
      {items.map(([label, value]) => (
        <div className="mini-metric" key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
}

function RiskBars({ result, expanded = false }: { result: HazardMindResult; expanded?: boolean }) {
  return (
    <div className="mt-3 space-y-3">
      <RiskBar expanded={expanded} label="Flood" value={result.hazard.confidence_scores.flood} tone="bg-red-400" />
      <RiskBar expanded={expanded} label="Earthquake" value={result.hazard.confidence_scores.earthquake} tone="bg-yellow-300" />
      <RiskBar expanded={expanded} label="Landslide" value={result.hazard.confidence_scores.landslide} tone="bg-emerald-300" />
    </div>
  );
}

function RiskBar({ label, value, tone, expanded = false }: { label: string; value: number; tone: string; expanded?: boolean }) {
  return (
    <div>
      <div className={`mb-1 flex justify-between font-mono uppercase tracking-[0.12em] ${expanded ? "text-sm" : "text-[10px]"}`}>
        <span className="text-slate-400">{label}</span>
        <span className="text-slate-100">{Math.round(value * 100)}%</span>
      </div>
      <div className={`${expanded ? "h-2.5" : "h-1.5"} overflow-hidden rounded-full bg-slate-950/80`}>
        <div className={`h-full rounded-full ${tone} shadow-[0_0_14px_currentColor]`} style={{ width: `${value * 100}%` }} />
      </div>
    </div>
  );
}

function BulletList({ items }: { items: string[] }) {
  return (
    <ul className="mt-2 space-y-2 text-sm text-slate-300">
      {items.map((item) => (
        <li className="flex gap-2" key={item}>
          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-cyan-200" />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}
