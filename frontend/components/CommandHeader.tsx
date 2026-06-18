import { Activity, AlertTriangle, Cpu, RadioTower } from "lucide-react";
import Image from "next/image";
import type { ReactNode } from "react";
import type { HazardMindResult } from "../lib/types";

type CommandHeaderProps = {
  result: HazardMindResult;
  dataSource: string;
};

export function CommandHeader({ result, dataSource }: CommandHeaderProps) {
  return (
    <header className="command-topbar">
      <div className="flex min-w-0 items-center gap-3">
        <div className="command-logo-frame">
          <Image
            src="/hazardmind-logo.png"
            alt="HazardMind AI"
            width={220}
            height={72}
            priority
            className="h-14 max-h-[56px] w-auto max-w-[220px] object-contain drop-shadow-[0_0_18px_rgba(34,211,238,0.35)]"
          />
        </div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="command-badge">Command Center</span>
            <span className="command-badge is-live">Live HUD</span>
          </div>
          <p className="mt-0.5 truncate font-mono text-[11px] text-slate-300">
            EVENT: {result.event_id} / {result.location}
          </p>
          <p className="text-[9px] uppercase tracking-[0.14em] text-cyan-200/80">
            Data source: {dataSource}
          </p>
        </div>
      </div>

      <div className="command-status-strip">
        <StatusPill icon={<AlertTriangle className="h-3.5 w-3.5" />} label="Severity" value={result.overall_severity} tone="critical" />
        <StatusPill icon={<Activity className="h-3.5 w-3.5" />} label="Hazard" value={result.hazard_type} tone="cyan" />
        <StatusPill icon={<RadioTower className="h-3.5 w-3.5" />} label="Status" value="Active Trace" tone="violet" />
        <StatusPill icon={<Cpu className="h-3.5 w-3.5" />} label="Agents" value="4 Complete" tone="green" />
      </div>
    </header>
  );
}

type StatusPillProps = {
  icon: ReactNode;
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
    <div className={`command-status-pill ${tones[tone]}`}>
      <span className="shrink-0">{icon}</span>
      <span className="min-w-0">
        <span className="block text-[8px] uppercase tracking-[0.14em] text-slate-400">{label}</span>
        <span className="block truncate font-semibold">{value}</span>
      </span>
    </div>
  );
}
