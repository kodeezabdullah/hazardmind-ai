"use client";

import { useEffect, useRef } from "react";
import type { BandMessage } from "../lib/bandLog";
import type { HazardMindResult, AgentLogEntry } from "../lib/types";

type LiveFeedPanelProps = {
  result: HazardMindResult;
  // Live Band room conversation (real agent-to-agent messages) — drives the chat.
  bandLog?: BandMessage[];
  // Whether a query is active (pipeline running/just-run) and the current backend
  // step — used to drive the workflow-style technical log lines.
  active?: boolean;
  step?: string | null;
  complete?: boolean;
};

// Technical workflow lines per pipeline step — what the agent is actually doing
// (the "logs" view, distinct from the agent-to-agent chat). Each step reveals its
// lines as the pipeline reaches it.
const WORKFLOW: Array<{ step: string; agent: string; lines: string[] }> = [
  { step: "received", agent: "orchestrator", lines: ["Pipeline dispatched", "Creating Band room + adding agents"] },
  {
    step: "satellite",
    agent: "satellite",
    lines: [
      "Resolving administrative boundary (geoBoundaries)",
      "Selecting latest Sentinel scene",
      "Downloading imagery from Copernicus",
      "Computing NDWI water index",
      "Classifying surface + vectorizing zones",
      "Uploading true-colour / index / classification + zones to R2",
    ],
  },
  {
    step: "hazard",
    agent: "hazard",
    lines: [
      "Reading satellite result",
      "Flood risk from NDWI",
      "Earthquake risk from USGS seismicity",
      "Landslide risk from SRTM DEM slope",
      "Assigning severity + confidence",
    ],
  },
  {
    step: "impact",
    agent: "impact",
    lines: [
      "Estimating exposed population (GeoNames)",
      "Checking hospitals / schools / roads",
      "Computing vulnerability score",
    ],
  },
  {
    step: "report",
    agent: "report",
    lines: ["Generating executive report (LLM)", "Rendering risk map + PDF", "Uploading report to R2"],
  },
];

const STEP_ORDER = ["received", "satellite", "hazard", "impact", "report", "complete"];

type LogLine = { agent: string; message: string; done: boolean };

function buildWorkflowLogs(active: boolean, step: string | null | undefined, complete: boolean): LogLine[] {
  if (!active) {
    return [
      { agent: "system", message: "HazardMind ready. Awaiting a query...", done: true },
    ];
  }
  const stepIdx = STEP_ORDER.indexOf((step || "received").toLowerCase());
  const lines: LogLine[] = [];
  for (const wf of WORKFLOW) {
    const wfIdx = STEP_ORDER.indexOf(wf.step);
    if (complete || wfIdx < stepIdx) {
      // finished stage — all lines done
      wf.lines.forEach((l) => lines.push({ agent: wf.agent, message: l, done: true }));
    } else if (wfIdx === stepIdx) {
      // current stage — lines streaming (last one "running")
      wf.lines.forEach((l, i) =>
        lines.push({ agent: wf.agent, message: l, done: i < wf.lines.length - 1 }),
      );
    }
  }
  if (complete) lines.push({ agent: "orchestrator", message: "Pipeline complete. Verdict posted.", done: true });
  return lines;
}

const AGENT_LABELS: Record<string, string> = {
  "hazardmind-orchestrator": "orchestrator",
  "hazardmind-satellite": "satellite",
  "hazardmind-hazard": "hazard",
  "hazardmind-impact": "impact",
  "hazardmind-report": "report",
};

const AGENT_TONES: Record<string, string> = {
  orchestrator: "tone-orchestrator",
  satellite: "tone-satellite",
  hazard: "tone-hazard",
  impact: "tone-impact",
  report: "tone-report",
};

function agentLabel(agent: string): string {
  return AGENT_LABELS[agent] ?? agent.replace("hazardmind-", "");
}

function agentTone(agent: string): string {
  const short = agentLabel(agent);
  return AGENT_TONES[short] ?? "tone-default";
}

function clockTime(timestamp?: string): string {
  if (!timestamp) return "";
  try {
    return new Date(timestamp).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return timestamp;
  }
}

export function LiveFeedPanel({ result, bandLog, active = false, step = null, complete = false }: LiveFeedPanelProps) {
  const termRef = useRef<HTMLDivElement>(null);
  const chatRef = useRef<HTMLDivElement>(null);

  // CHAT = the real agent-to-agent Band conversation (messages / handoffs).
  const chat: ChatLine[] = (bandLog ?? []).map((m) => ({
    agent: m.agent,
    message: m.content,
    timestamp: m.timestamp,
    hasPayload: m.hasPayload,
  }));

  // LOGS = the technical workflow (what each agent is actually doing), derived
  // from the pipeline step — deliberately DIFFERENT from the chat.
  const logs = buildWorkflowLogs(active, step, complete);

  useEffect(() => {
    termRef.current?.scrollTo({ top: termRef.current.scrollHeight });
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight });
  }, [chat.length, logs.length]);

  return (
    <section className="live-feed-panel" aria-label="Live agent logs and chat">
      {/* ---- Live Logs: terminal ---- */}
      <div className="live-feed-section live-feed-logs">
        <div className="terminal-titlebar">
          <span className="term-dot term-dot-red" />
          <span className="term-dot term-dot-amber" />
          <span className="term-dot term-dot-green" />
          <span className="terminal-title">hazardmind — live logs</span>
        </div>
        <div ref={termRef} className="thin-scrollbar terminal-body">
          {logs.length === 0 ? (
            <div className="term-line">
              <span className="term-cursor">_</span>
            </div>
          ) : (
            logs.map((entry, index) => (
              <div key={`log-${index}`} className="term-line">
                <span className={`term-agent ${agentTone(entry.agent)}`}>[{entry.agent}]</span>
                <span className={`term-status ${entry.done ? "status-complete" : "status-running"}`}>
                  {entry.done ? "OK" : "..."}
                </span>
                <span className="term-msg">{entry.message}</span>
              </div>
            ))
          )}
          <div className="term-line term-prompt">
            <span className="term-arrow">&gt;</span>
            <span className="term-cursor">_</span>
          </div>
        </div>
      </div>

      <div className="live-feed-divider" />

      {/* ---- Agent Chat: chat room (real Band room conversation) ---- */}
      <div className="live-feed-section live-feed-chat">
        <header className="chat-room-header">
          <span className="chat-room-live" />
          <span className="chat-room-title">Agent Chat</span>
          <span className="chat-room-sub">live Band room</span>
        </header>
        <div ref={chatRef} className="thin-scrollbar chat-room-body">
          {chat.length === 0 ? (
            <p className="chat-empty">No messages yet.</p>
          ) : (
            chat.map((entry, index) => {
              const short = agentLabel(entry.agent);
              const isOrchestrator = short === "orchestrator";
              return (
                <div
                  key={`chat-${index}`}
                  className={`chat-msg ${isOrchestrator ? "chat-msg--right" : "chat-msg--left"}`}
                >
                  {!isOrchestrator ? (
                    <span className={`chat-avatar ${agentTone(entry.agent)}`}>
                      {short.charAt(0).toUpperCase()}
                    </span>
                  ) : null}
                  <div className="chat-bubble">
                    <div className="chat-bubble-meta">
                      <span className={`chat-name ${agentTone(entry.agent)}`}>{short}</span>
                      <span className="chat-time">{clockTime(entry.timestamp)}</span>
                    </div>
                    <p className="chat-text">{entry.message}</p>
                    {entry.hasPayload ? (
                      <span className="chat-payload" title="Structured result passed through Band">
                        + data payload
                      </span>
                    ) : null}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>
    </section>
  );
}

type ChatLine = {
  agent: string;
  message: string;
  timestamp?: string;
  status?: AgentLogEntry["status"];
  hasPayload?: boolean;
};
