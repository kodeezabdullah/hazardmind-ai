"use client";

import type { CSSProperties } from "react";
import type { LucideIcon } from "lucide-react";

type AgentAvatarNodeProps = {
  active: boolean;
  angle: number;
  codename?: string;
  icon: LucideIcon;
  isAgent: boolean;
  label: string;
  tone: "cyan" | "red" | "amber" | "violet" | "slate";
  type: string;
  onClick: () => void;
};

export function AgentAvatarNode({
  active,
  angle,
  codename,
  icon: Icon,
  isAgent,
  label,
  tone,
  type,
  onClick,
}: AgentAvatarNodeProps) {
  return (
    <button
      className={`agent-avatar-node is-${tone} ${isAgent ? "is-agent" : "is-module"} ${active ? "is-active" : ""}`}
      onClick={onClick}
      style={
        {
          transform: `translate(-50%, -50%) rotate(${angle}deg) translateX(calc(var(--node-radius) + var(--node-boost, 0px))) rotate(${-angle}deg)`,
        } as CSSProperties
      }
      type="button"
    >
      <span className="agent-avatar-ring">
        <Icon className="h-4 w-4" />
      </span>
      <span className="agent-avatar-copy">
        <span>{label}</span>
        <small>{codename ?? type}</small>
      </span>
      <span className="agent-avatar-status" aria-hidden="true" />
    </button>
  );
}
