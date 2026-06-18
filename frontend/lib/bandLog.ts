// Fetches the live Band room conversation for an event and cleans it for display.
// This is the real agent-to-agent collaboration happening THROUGH Band:
// the orchestrator dispatches, agents acknowledge and hand off, and the verdict
// is posted — all as messages in the per-event Band room.

export type BandMessage = {
  agent: string; // normalized short name: orchestrator | satellite | hazard | impact | report
  content: string;
  timestamp?: string;
  type: string;
  // True when the handoff carried a structured JSON payload through Band — this
  // is the "data plane through Band" signal we surface in the UI.
  hasPayload?: boolean;
};

const apiBase = () => process.env.NEXT_PUBLIC_API_URL?.trim().replace(/\/$/, "") ?? "";

// Map the various raw agent identifiers Band returns to a clean short name.
const AGENT_ALIASES: Array<[RegExp, string]> = [
  [/orchestrat/i, "orchestrator"],
  [/satellite/i, "satellite"],
  [/hazard/i, "hazard"],
  [/impact/i, "impact"],
  [/report/i, "report"],
];

function normalizeAgent(raw: string | null | undefined, content: string): string {
  // Trust the explicit sender field first (e.g. "HazardMind Satellite",
  // "hazardmind-orchestrator"); only fall back to the content if it's empty.
  if (raw) {
    for (const [re, name] of AGENT_ALIASES) {
      if (re.test(raw)) return name;
    }
  }
  // Content fallback: use the FIRST agent word that appears (the speaker often
  // names itself, e.g. "Satellite here"), ignoring @mentions of other agents.
  const withoutMentions = content.replace(/@[\w./[\]-]+/g, "");
  for (const [re, name] of AGENT_ALIASES) {
    if (re.test(withoutMentions)) return name;
  }
  return "agent";
}

// Separate the natural prose from the structured JSON payload appended to the
// tail of a handoff message (band_client.send_handoff appends compact JSON).
function splitPayload(content: string): { prose: string; hasPayload: boolean } {
  const idx = content.indexOf("{");
  if (idx > 0 && content.trimEnd().endsWith("}")) {
    const tail = content.slice(idx).trim();
    try {
      JSON.parse(tail);
      return { prose: content.slice(0, idx).trim(), hasPayload: true };
    } catch {
      // not valid JSON — leave as-is
    }
  }
  return { prose: content, hasPayload: false };
}

// Strip raw Band mention tokens like "@[[uuid]]" and "@handle/agent-name" so the
// chat reads cleanly, and collapse whitespace.
function cleanContent(content: string): string {
  return content
    .replace(/@\[\[[^\]]+\]\]/g, "") // @[[uuid]]
    .replace(/@[\w.-]+\/[\w-]+/g, (m) => "@" + m.split("/").pop()!.replace(/^hazardmind-/, "")) // @user/hazardmind-x -> @x
    .replace(/@hazardmind-([\w]+)/g, "@$1")
    .replace(/\s+/g, " ")
    .trim();
}

/** Fetch + clean + de-duplicate the Band room messages for a job. */
export async function loadBandLog(jobId: string, signal?: AbortSignal): Promise<BandMessage[]> {
  const base = apiBase();
  if (!base || !jobId) return [];

  const res = await fetch(`${base}/band-log/${encodeURIComponent(jobId)}`, {
    cache: "no-store",
    signal,
  });
  if (!res.ok) return [];

  const data = (await res.json()) as { messages?: Array<Record<string, unknown>> };
  const raw = data.messages ?? [];

  const seen = new Set<string>();
  const out: BandMessage[] = [];
  for (const m of raw) {
    const rawContent = String(m.content ?? "");
    // Split off the structured JSON tail (the data carried through Band) so the
    // chat shows clean prose plus a "payload attached" marker.
    const { prose, hasPayload } = splitPayload(rawContent);
    const content = cleanContent(prose);
    if (!content) continue;
    const agent = normalizeAgent(m.agent as string | undefined, rawContent);
    const key = `${agent}:${content}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      agent,
      content,
      timestamp: m.timestamp ? String(m.timestamp) : undefined,
      type: String(m.type ?? "text"),
      hasPayload,
    });
  }
  return out;
}
