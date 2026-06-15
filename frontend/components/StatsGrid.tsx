import { Building2, GraduationCap, Route, UsersRound } from "lucide-react";
import type { HazardMindResult } from "../lib/types";

type StatsGridProps = {
  result: HazardMindResult;
};

export function StatsGrid({ result }: StatsGridProps) {
  const stats = [
    {
      label: "Population",
      value: result.impact.population_affected.toLocaleString(),
      detail: "affected",
      icon: UsersRound,
      tone: "text-red-200",
    },
    {
      label: "Hospitals",
      value: String(result.impact.hospitals_at_risk),
      detail: "at risk",
      icon: Building2,
      tone: "text-orange-200",
    },
    {
      label: "Roads",
      value: `${result.impact.roads_blocked_km}`,
      detail: "km blocked",
      icon: Route,
      tone: "text-yellow-200",
    },
    {
      label: "Schools",
      value: String(result.impact.schools_affected),
      detail: "affected",
      icon: GraduationCap,
      tone: "text-cyan-200",
    },
  ];

  return (
    <div className="grid grid-cols-4 gap-1.5">
      {stats.map((stat) => {
        const Icon = stat.icon;
        return (
          <div key={stat.label} className="rounded-lg border border-white/10 bg-white/[0.03] p-2 transition hover:border-cyan-300/25 hover:bg-cyan-300/[0.045]">
            <div className="mb-1 flex items-center justify-between gap-1">
              <span className="truncate text-[8px] uppercase tracking-[0.12em] text-slate-500">{stat.label}</span>
              <Icon className={`h-3.5 w-3.5 shrink-0 ${stat.tone}`} />
            </div>
            <div className={`truncate text-base font-semibold leading-none ${stat.tone}`}>{stat.value}</div>
            <div className="mt-0.5 truncate text-[10px] text-slate-500">{stat.detail}</div>
          </div>
        );
      })}
    </div>
  );
}
