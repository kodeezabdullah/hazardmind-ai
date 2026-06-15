"use client";

import { useState } from "react";
import { CheckCircle2, ChevronDown, ChevronUp, Radio } from "lucide-react";
import type { AgentLogEntry } from "../lib/types";

type AgentTimelineProps = {
  entries: AgentLogEntry[];
};

export function AgentTimeline({ entries }: AgentTimelineProps) {
  const [isOpen, setIsOpen] = useState(false);
  const completeCount = entries.filter((entry) => entry.status === "complete").length;

  return (
    <section
      className={`fixed inset-x-2 bottom-2 z-40 mx-auto max-w-[1280px] overflow-hidden rounded-t-xl border border-cyan-300/24 bg-[#06101D]/88 shadow-[0_-18px_60px_rgba(0,0,0,0.45),0_0_34px_rgba(34,211,238,0.14)] backdrop-blur-xl transition-[max-height,border-color,background-color] duration-300 ease-out sm:inset-x-4 ${
        isOpen ? "max-h-[58vh] border-cyan-200/40 md:max-h-[44vh]" : "max-h-[50px]"
      }`}
    >
      <button
        aria-expanded={isOpen}
        className="flex h-[50px] w-full items-center justify-between gap-3 border-b border-cyan-300/10 px-3 text-left transition hover:bg-cyan-300/[0.04] sm:px-4"
        onClick={() => setIsOpen((current) => !current)}
        type="button"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-md border border-cyan-300/22 bg-cyan-300/10">
            <Radio className="h-4 w-4 text-cyan-200" />
          </span>
          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold text-slate-100">
              Agent Timeline / System Trace
            </span>
            <span className="hidden font-mono text-[10px] uppercase tracking-[0.16em] text-cyan-200/75 sm:block">
              trace stream | {completeCount}/{entries.length} complete
            </span>
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-2">
          <span className="status-chip hidden sm:inline-flex">{completeCount} complete</span>
          <span className="grid h-7 w-7 place-items-center rounded-md border border-cyan-300/22 bg-cyan-300/10 text-cyan-100">
            {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
          </span>
        </span>
      </button>

      <div className="thin-scrollbar max-h-[calc(58vh-50px)] overflow-y-auto px-3 pb-3 pt-2 md:max-h-[calc(44vh-50px)] sm:px-4">
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          {entries.map((entry, index) => (
            <article
              key={`${entry.agent}-${entry.timestamp}-${index}`}
              className="rounded-md border border-white/10 bg-white/[0.035] px-2.5 py-2 transition hover:border-cyan-300/25 hover:bg-cyan-300/[0.04]"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-xs text-cyan-100">{entry.agent}</span>
                <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-300" />
              </div>
              <p className="mt-1 text-[11px] leading-4 text-slate-400">{entry.message}</p>
              <time className="mt-1 block font-mono text-[9px] text-slate-600">{entry.timestamp}</time>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
