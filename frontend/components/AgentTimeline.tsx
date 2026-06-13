import { CheckCircle2, Radio } from "lucide-react";
import type { AgentLogEntry } from "../lib/types";

type AgentTimelineProps = {
  entries: AgentLogEntry[];
};

export function AgentTimeline({ entries }: AgentTimelineProps) {
  return (
    <section className="glass-panel shrink-0 overflow-hidden p-2.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-2">
        <Radio className="h-4 w-4 text-cyan-200" />
          <h2 className="text-sm font-semibold text-slate-100">Agent Timeline / System Trace</h2>
        </span>
        <span className="status-chip">4 complete</span>
      </div>
      <div className="grid gap-1.5 md:grid-cols-4">
        {entries.map((entry) => (
          <article key={entry.agent} className="rounded-md border border-white/10 bg-white/[0.03] px-2.5 py-2 transition hover:border-cyan-300/25">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate font-mono text-xs text-cyan-100">{entry.agent}</span>
              <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-300" />
            </div>
            <p className="mt-1 truncate text-[11px] text-slate-400">{entry.message}</p>
            <time className="mt-1 block font-mono text-[9px] text-slate-600">{entry.timestamp}</time>
          </article>
        ))}
      </div>
    </section>
  );
}
