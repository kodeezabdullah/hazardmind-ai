import { BrainCircuit, ShieldAlert, Sparkles } from "lucide-react";
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

      {result.model_sources ? (
        <section className="panel-section mt-2.5 p-2.5">
          <h3 className="text-sm font-semibold text-cyan-50">Model Sources</h3>
          <div className="mt-2 grid grid-cols-2 gap-1.5 text-[11px] text-slate-300">
            <SourceRow label="Detailed" value={result.model_sources.detailed_report} />
            <SourceRow label="Summary" value={result.model_sources.executive_summary} />
            <SourceRow label="Fallback" value={result.model_sources.fallback_used ? "used" : "not used"} />
            <SourceRow label="Model" value={result.model_sources.featherless_model ?? "n/a"} />
          </div>
        </section>
      ) : null}

      <ReportActions result={result} />
    </aside>
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
