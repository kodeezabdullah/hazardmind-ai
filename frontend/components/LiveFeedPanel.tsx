"use client";

import { useEffect, useRef } from "react";
import type { HazardMindResult, AgentLogEntry } from "../lib/types";

type LiveFeedPanelProps = {
  result: HazardMindResult;
};

const AGENT_LABELS: Record<string, string> = {
  "hazardmind-orchestrator": "orchestrator",
  "hazardmind-satellite": "satellite",
  "hazardmind-hazard": "hazard",
  "hazardmind-impact": "impact",
  "hazardmind-report": "report",
};

const AGENT_TONES: Record<string, string> = {
  "hazardmind-orchestrator": "tone-orchestrator",
  "hazardmind-satellite": "tone-satellite",
  "hazardmind-hazard": "tone-hazard",
  "hazardmind-impact": "tone-impact",
  "hazardmind-report": "tone-report",
};

function agentLabel(agent: string): string {
  return AGENT_LABELS[agent] ?? agent.replace("hazardmind-", "");
}

function agentTone(agent: string): string {
  return AGENT_TONES[agent] ?? "tone-default";
}

function clockTime(timestamp: string): string {
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

export function LiveFeedPanel({ result }: LiveFeedPanelProps) {
  const log: AgentLogEntry[] = result.agent_log ?? [];
  const termRef = useRef<HTMLDivElement>(null);
  const chatRef = useRef<HTMLDivElement>(null);

  // Auto-scroll both feeds to the newest entry.
  useEffect(() => {
    termRef.current?.scrollTo({ top: termRef.current.scrollHeight });
    chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight });
  }, [log.length]);

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
          {log.length === 0 ? (
            <div className="term-line">
              <span className="term-cursor">_</span>
            </div>
          ) : (
            log.map((entry, index) => (
              <div key={`log-${index}`} className="term-line">
                <span className="term-time">{clockTime(entry.timestamp)}</span>
                <span className={`term-agent ${agentTone(entry.agent)}`}>
                  [{agentLabel(entry.agent)}]
                </span>
                <span className={`term-status status-${entry.status}`}>
                  {entry.status.toUpperCase()}
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

      {/* ---- Agent Chat: chat room ---- */}
      <div className="live-feed-section live-feed-chat">
        <header className="chat-room-header">
          <span className="chat-room-live" />
          <span className="chat-room-title">Agent Chat</span>
          <span className="chat-room-sub">Band room</span>
        </header>
        <div ref={chatRef} className="thin-scrollbar chat-room-body">
          {log.length === 0 ? (
            <p className="chat-empty">No messages yet.</p>
          ) : (
            log.map((entry, index) => {
              // Orchestrator messages on the right (like "me"), others on the left.
              const isOrchestrator = entry.agent === "hazardmind-orchestrator";
              return (
                <div
                  key={`chat-${index}`}
                  className={`chat-msg ${isOrchestrator ? "chat-msg--right" : "chat-msg--left"}`}
                >
                  {!isOrchestrator ? (
                    <span className={`chat-avatar ${agentTone(entry.agent)}`}>
                      {agentLabel(entry.agent).charAt(0).toUpperCase()}
                    </span>
                  ) : null}
                  <div className="chat-bubble">
                    <div className="chat-bubble-meta">
                      <span className={`chat-name ${agentTone(entry.agent)}`}>
                        {agentLabel(entry.agent)}
                      </span>
                      <span className="chat-time">{clockTime(entry.timestamp)}</span>
                    </div>
                    <p className="chat-text">{entry.message}</p>
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
