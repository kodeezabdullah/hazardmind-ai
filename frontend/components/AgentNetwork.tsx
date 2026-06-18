"use client";

import { Activity, FileText, RadioTower, Satellite } from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type AgentModuleId = "satellite" | "hazard" | "impact" | "report";

export type AgentModule = {
  id: AgentModuleId;
  label: string;
  codename: string;
  type: "agent";
  icon: LucideIcon;
  tone: "cyan" | "red" | "amber" | "violet";
};

export const agentModules: AgentModule[] = [
  { id: "satellite", label: "Satellite Agent", codename: "Orbital Eye", type: "agent", icon: Satellite, tone: "cyan" },
  { id: "hazard", label: "Hazard Agent", codename: "Risk Sentinel", type: "agent", icon: RadioTower, tone: "red" },
  { id: "impact", label: "Impact Agent", codename: "Civic Pulse", type: "agent", icon: Activity, tone: "amber" },
  { id: "report", label: "Report Agent", codename: "Briefing Core", type: "agent", icon: FileText, tone: "violet" },
];

type AgentNetworkProps = {
  activeAgentId: AgentModuleId;
  onSelectAgent: (agentId: AgentModuleId) => void;
};

export function AgentNetwork({ activeAgentId, onSelectAgent }: AgentNetworkProps) {
  return (
    <section className="agent-network" aria-label="HazardMind agent flow">
      {agentModules.map((agent, index) => {
        const Icon = agent.icon;
        const active = agent.id === activeAgentId;
        return (
          <div className="agent-network-step" key={agent.id}>
            <button
              className={`agent-network-node is-${agent.tone} ${active ? "is-active" : ""}`}
              onClick={() => onSelectAgent(agent.id)}
              type="button"
            >
              <span className="agent-network-avatar">
                <Icon className="h-4 w-4" />
              </span>
              <span className="agent-network-copy">
                <strong>{agent.label}</strong>
                <small>{agent.codename}</small>
              </span>
              <span className="agent-network-status" aria-hidden="true" />
            </button>
            {index < agentModules.length - 1 ? <span className="agent-network-connector" aria-hidden="true" /> : null}
          </div>
        );
      })}
    </section>
  );
}
