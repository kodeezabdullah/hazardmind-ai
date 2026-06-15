import { AlertTriangle, BrainCircuit, CheckCircle2, Clock3, MapPinned, RadioTower, ShieldAlert, Sparkles } from "lucide-react";
import { ReportActions } from "./ReportActions";
import { StatsGrid } from "./StatsGrid";
import type { HazardMindResult } from "../lib/types";

type IntelligencePanelProps = {
  result: HazardMindResult;
};

export function IntelligencePanel({ result }: IntelligencePanelProps) {
  return (
    <aside className="glass-panel thin-scrollbar min-h-0 overflow-y-auto p-3">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <p className="text-[9px] font-semibold uppercase tracking-[0.22em] text-violet-200">
            Executive Intelligence
          </p>
          <h2 className="mt-0.5 text-base font-semibold text-slate-50">Report Output</h2>
        </div>
        <div className="grid h-8 w-8 place-items-center rounded-lg border border-violet-300/24 bg-violet-300/10">
          <BrainCircuit className="h-4 w-4 text-violet-200" />
        </div>
      </div>

      <ReportActions result={result} />

      <StatsGrid result={result} />

      <section className="panel-section mt-2.5 border-red-300/18 bg-red-500/[0.045] p-2.5">
        <div className="mb-2 flex items-center justify-between gap-2 text-sm font-semibold text-red-100">
          <span className="flex items-center gap-2">
            <ShieldAlert className="h-4 w-4" />
            Risk Confidence
          </span>
          <span className="rounded border border-red-300/25 bg-red-400/10 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em] text-red-100">
            critical
          </span>
        </div>
        <div className="space-y-1.5">
          <Confidence label="Flood" value={result.hazard.confidence_scores.flood} tone="bg-red-400" />
          <Confidence label="Earthquake" value={result.hazard.confidence_scores.earthquake} tone="bg-yellow-300" />
          <Confidence label="Landslide" value={result.hazard.confidence_scores.landslide} tone="bg-emerald-300" />
        </div>
      </section>

      <section className="panel-section mt-2.5 border-violet-300/16 bg-violet-300/[0.045] p-2.5">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-violet-200" />
          <h3 className="text-sm font-semibold text-cyan-50">Executive Summary</h3>
        </div>
        <p className="mt-1.5 text-[13px] leading-5 text-slate-300">{result.report.summary}</p>
      </section>

      <SuperBrainIntelligence result={result} />

      {result.model_sources ? (
        <section className="panel-section mt-2.5 p-2.5">
          <h3 className="text-sm font-semibold text-cyan-50">Model Sources</h3>
          <div className="mt-2 grid grid-cols-2 gap-1.5 text-[11px] text-slate-300">
            <SourceRow label="Detailed" value={result.model_sources.detailed_report} />
            <SourceRow label="Summary" value={result.model_sources.executive_summary} />
            <SourceRow label="Fallback" value={result.model_sources.fallback_used ? "used" : "not used"} />
            <SourceRow label="Model" value={result.model_sources.featherless_model ?? "n/a"} />
            {result.model_sources.intelligence ? (
              <>
                <SourceRow label="Criticality" value={result.model_sources.intelligence.criticality ?? "n/a"} />
                <SourceRow label="Map" value={result.model_sources.intelligence.map_narrative ?? "n/a"} />
                <SourceRow label="Timeline" value={result.model_sources.intelligence.priority_recommendations ?? "n/a"} />
                <SourceRow label="Quality" value={result.model_sources.intelligence.quality_check ?? "n/a"} />
              </>
            ) : null}
          </div>
        </section>
      ) : null}

      <section className="panel-section mt-2.5 p-2.5">
        <h3 className="text-sm font-semibold text-cyan-50">Recommended Actions</h3>
        <ul className="mt-2 space-y-1.5">
          {result.report.recommendations.map((recommendation) => (
            <li key={recommendation} className="flex gap-2 text-[13px] leading-5 text-slate-300">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-300 shadow-[0_0_12px_rgba(34,211,238,0.8)]" />
              <span>{recommendation}</span>
            </li>
          ))}
        </ul>
      </section>

      {result.report.detailed_body ? (
        <section className="panel-section mt-2.5 border-cyan-300/16 bg-cyan-300/[0.035] p-2.5">
          <h3 className="text-sm font-semibold text-cyan-50">Operational Analysis</h3>
          <p className="mt-1.5 text-[13px] leading-5 text-slate-300">{result.report.detailed_body}</p>
        </section>
      ) : null}

      {result.report.technical_analysis ? (
        <section className="panel-section mt-2.5 p-2.5">
          <h3 className="text-sm font-semibold text-cyan-50">Technical Analysis</h3>
          <p className="mt-1.5 text-[13px] leading-5 text-slate-300">{result.report.technical_analysis}</p>
        </section>
      ) : null}

      <InsightList title="Response Priorities" items={result.report.response_priorities} />
      <InsightList title="Assumptions" items={result.report.assumptions} compact />
      <InsightList title="Limitations" items={result.report.limitations} compact />
    </aside>
  );
}

function SuperBrainIntelligence({ result }: { result: HazardMindResult }) {
  const intelligence = result.intelligence;

  if (!intelligence) {
    return null;
  }

  const confidence = Math.round(intelligence.criticality.overall_confidence * 100);
  const qualityTone =
    intelligence.quality_check.status === "ready"
      ? "border-emerald-300/30 bg-emerald-300/10 text-emerald-100"
      : intelligence.quality_check.status === "ready_with_warnings"
        ? "border-yellow-300/30 bg-yellow-300/10 text-yellow-100"
        : "border-red-300/30 bg-red-300/10 text-red-100";

  return (
    <>
      <section className="panel-section mt-2.5 border-cyan-300/20 bg-cyan-300/[0.045] p-2.5">
        <div className="mb-2 flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            <BrainCircuit className="h-4 w-4 text-cyan-200" />
            <h3 className="text-sm font-semibold text-cyan-50">Super Brain Assessment</h3>
          </div>
          <span className="rounded border border-red-300/30 bg-red-400/10 px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em] text-red-100">
            {intelligence.criticality.criticality}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-1.5 text-[11px] text-slate-300">
          <MetricChip label="Confidence" value={`${confidence}%`} />
          <MetricChip label="Escalation" value={intelligence.criticality.escalation_required ? "required" : "not required"} />
        </div>
        <p className="mt-2 text-[13px] leading-5 text-slate-300">{intelligence.criticality.rationale}</p>
        <InlineInsightList title="Trigger Factors" items={intelligence.criticality.trigger_factors} />
      </section>

      <section className="panel-section mt-2.5 p-2.5">
        <div className="flex items-center gap-2">
          <MapPinned className="h-4 w-4 text-cyan-200" />
          <h3 className="text-sm font-semibold text-cyan-50">Map Narrative</h3>
        </div>
        <p className="mt-1.5 text-[13px] leading-5 text-slate-300">{intelligence.map_narrative.map_narrative}</p>
        <InlineInsightList title="Spatial Findings" items={intelligence.map_narrative.key_spatial_findings} />
        <InlineInsightList title="Hotspots" items={intelligence.map_narrative.hotspots} />
      </section>

      <section className="panel-section mt-2.5 border-blue-300/16 bg-blue-300/[0.035] p-2.5">
        <div className="flex items-center gap-2">
          <Clock3 className="h-4 w-4 text-blue-200" />
          <h3 className="text-sm font-semibold text-cyan-50">Priority Timeline</h3>
        </div>
        <TimelineBlock label="Next 6h" items={intelligence.priority_timeline.next_6_hours} />
        <TimelineBlock label="Next 24h" items={intelligence.priority_timeline.next_24_hours} />
        <TimelineBlock label="Next 72h" items={intelligence.priority_timeline.next_72_hours} />
      </section>

      <section className="panel-section mt-2.5 p-2.5">
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="flex items-center gap-2 text-sm font-semibold text-cyan-50">
            <CheckCircle2 className="h-4 w-4 text-emerald-200" />
            Quality Gate
          </span>
          <span className={`rounded border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.14em] ${qualityTone}`}>
            {intelligence.quality_check.status.replaceAll("_", " ")}
          </span>
        </div>
        {intelligence.anomalies.anomalies_detected ? (
          <InlineInsightList
            title="Anomalies"
            items={intelligence.anomalies.anomalies.map((item) => `${item.severity}: ${item.description}`)}
          />
        ) : (
          <div className="flex items-center gap-2 text-[12px] text-slate-300">
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-200" />
            No blocking anomalies detected.
          </div>
        )}
        <InlineInsightList title="Warnings" items={intelligence.quality_check.warnings} />
      </section>

      <section className="panel-section mt-2.5 border-violet-300/16 bg-violet-300/[0.04] p-2.5">
        <div className="flex items-center gap-2">
          <RadioTower className="h-4 w-4 text-violet-200" />
          <h3 className="text-sm font-semibold text-cyan-50">Band-Ready Message</h3>
        </div>
        <div className="mt-2 flex items-center justify-between gap-2 text-[11px]">
          <span className="font-mono text-violet-100">{intelligence.band_ready_message.target}</span>
          <span className="rounded border border-white/10 bg-white/[0.04] px-1.5 py-0.5 font-mono text-slate-300">
            {intelligence.band_ready_message.status}
          </span>
        </div>
        <p className="mt-1.5 text-[12px] leading-5 text-slate-300">{intelligence.band_ready_message.message}</p>
      </section>

      {intelligence.decision_brief.human_review_required ? (
        <section className="panel-section mt-2.5 border-yellow-300/20 bg-yellow-300/[0.05] p-2.5 text-[12px] text-yellow-100">
          <span className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4" />
            Human review required before external release.
          </span>
        </section>
      ) : null}
    </>
  );
}

function TimelineBlock({ label, items }: { label: string; items: string[] }) {
  if (!items.length) {
    return null;
  }

  return (
    <div className="mt-2">
      <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-blue-200">{label}</div>
      <ul className="mt-1 space-y-1">
        {items.map((item) => (
          <li key={item} className="flex gap-2 text-[12px] leading-5 text-slate-300">
            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-blue-300" />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function MetricChip({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-cyan-300/15 bg-slate-950/40 px-2 py-1">
      <span className="block text-[9px] uppercase tracking-[0.12em] text-slate-500">{label}</span>
      <span className="mt-0.5 block font-mono text-cyan-100">{value}</span>
    </div>
  );
}

function InlineInsightList({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) {
    return null;
  }

  return (
    <div className="mt-2">
      <h4 className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">{title}</h4>
      <ul className="mt-1 space-y-1">
        {items.map((item) => (
          <li key={item} className="flex gap-2 text-[12px] leading-5 text-slate-300">
            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-300/80" />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function InsightList({
  title,
  items,
  compact = false,
}: {
  title: string;
  items?: string[];
  compact?: boolean;
}) {
  if (!items?.length) {
    return null;
  }

  return (
    <section className="panel-section mt-2.5 p-2.5">
      <h3 className="text-sm font-semibold text-cyan-50">{title}</h3>
      <ul className="mt-2 space-y-1.5">
        {items.map((item) => (
          <li key={item} className={`flex gap-2 ${compact ? "text-xs" : "text-[13px]"} leading-5 text-slate-300`}>
            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-violet-300 shadow-[0_0_12px_rgba(167,139,250,0.65)]" />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function SourceRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-white/10 bg-white/[0.03] px-2 py-1">
      <span className="block text-[9px] uppercase tracking-[0.12em] text-slate-500">{label}</span>
      <span className="mt-0.5 block truncate font-mono text-cyan-100">{value}</span>
    </div>
  );
}

function Confidence({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div>
      <div className="mb-1 flex justify-between text-[11px]">
        <span className="text-slate-400">{label}</span>
        <span className="font-mono text-slate-200">{Math.round(value * 100)}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-800">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${value * 100}%` }} />
      </div>
    </div>
  );
}
