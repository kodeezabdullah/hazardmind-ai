import { Activity, AlertTriangle, Cpu, RadioTower } from "lucide-react";
import type { HazardMindResult } from "../lib/types";

type CommandHeaderProps = {
  result: HazardMindResult;
  dataSource: "Report Agent JSON" | "Local fallback";
};

export function CommandHeader({ result, dataSource }: CommandHeaderProps) {
  return (
    <header className="glass-panel flex shrink-0 flex-col gap-2 px-3.5 py-2.5 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex min-w-0 items-center gap-2.5">
        <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-cyan-300/30 bg-cyan-300/10 shadow-[0_0_20px_rgba(34,211,238,0.2)]">
          <Cpu className="h-5 w-5 text-cyan-200" />
        </div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-lg font-semibold tracking-[0.16em] text-cyan-50">
              HAZARDMIND AI
            </h1>
            <span className="rounded border border-violet-300/30 bg-violet-400/10 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-[0.2em] text-violet-100">
              Command Center
            </span>
          </div>
          <p className="mt-0.5 truncate text-xs text-slate-400">
            {result.location} / {result.event_id}
          </p>
          <p className="mt-0.5 text-[10px] uppercase tracking-[0.14em] text-cyan-200/80">
            Data source: {dataSource}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-1.5 text-xs sm:grid-cols-4 lg:min-w-[500px]">
        <StatusPill icon={<AlertTriangle className="h-4 w-4" />} label="Severity" value={result.overall_severity} tone="critical" />
        <StatusPill icon={<Activity className="h-4 w-4" />} label="Hazard" value={result.hazard_type} tone="cyan" />
        <StatusPill icon={<RadioTower className="h-4 w-4" />} label="Status" value="Active Trace" tone="violet" />
        <StatusPill icon={<Cpu className="h-4 w-4" />} label="Agents" value="4 Complete" tone="green" />
      </div>
    </header>
  );
}

type StatusPillProps = {
  icon: React.ReactNode;
  label: string;
  value: string;
  tone: "critical" | "cyan" | "violet" | "green";
};

function StatusPill({ icon, label, value, tone }: StatusPillProps) {
  const tones = {
    critical: "border-red-400/35 bg-red-500/10 text-red-100",
    cyan: "border-cyan-300/30 bg-cyan-400/10 text-cyan-100",
    violet: "border-violet-300/30 bg-violet-400/10 text-violet-100",
    green: "border-emerald-300/30 bg-emerald-400/10 text-emerald-100",
  };

  return (
    <div className={`flex items-center gap-2 rounded-md border px-2.5 py-1.5 ${tones[tone]}`}>
      <span className="shrink-0">{icon}</span>
      <span className="min-w-0">
        <span className="block text-[9px] uppercase tracking-[0.16em] text-slate-400">{label}</span>
        <span className="block truncate font-semibold">{value}</span>
      </span>
    </div>
  );
}
