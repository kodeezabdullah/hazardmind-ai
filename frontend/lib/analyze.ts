// Drives the backend pipeline from a free-text query:
//   POST /analyze  -> job_id
//   GET  /status   -> poll until complete
//   GET  /results  -> final result (adapted by loadHazardResult)
//   GET  /band-log -> live agent chat messages
//
// Backend contract (unchanged):
//   POST /analyze   { location, disaster_type, magnitude? } -> { job_id, status, band_room_id }
//   GET  /status/{id}                                       -> { status, step, progress }
//   GET  /results/{id}                                      -> { satellite, hazard, impact, report }
//   GET  /band-log/{id}                                     -> { messages: [{agent, content, timestamp, type}] }

import { loadBandLog, type BandMessage } from "./bandLog";
import { loadHazardResult, type HazardResultLoad } from "./loadHazardResult";

export type AnalyzeProgress = { status: string; step: string; progress: number };

const apiBase = () => process.env.NEXT_PUBLIC_API_URL?.trim().replace(/\/$/, "") ?? "";

/** Parse "flood in Rawalpindi" / "check landslide islamabad" into {location, disaster_type}. */
export function parseQuery(query: string): { location: string; disaster_type: string } {
  const q = query.trim();
  const lower = q.toLowerCase();

  // Disaster type — typo-tolerant (landlside/landslyde, quak, etc.).
  let disaster_type = "flood";
  if (/(earthquak|quak|seismic|tremor)/i.test(lower)) {
    disaster_type = "earthquake";
  } else if (/(land\w*l?sl?\w*|land\s*sl\w*|slide|mudslide)/i.test(lower)) {
    disaster_type = "landslide";
  }

  // Strip disaster words + common filler verbs/words, leaving just the place.
  // Disaster stems use \w* so typos (floodn, floooding, quak) are still removed.
  const FILLER =
    /\b(flood\w*|earthquak\w*|earth\b|quak\w*|seismic\w*|tremor\w*|land\w*sl?\w*|land\w*lside|mud\s*slide|slide\w*|disaster\w*|hazard\w*|risk\w*|emergenc\w*|event|new|check|show|run|analy\w*|assess\w*|detect\w*|near|around|in|at|the|for|of|a|please|me)\b/gi;
  const location = q.replace(FILLER, " ").replace(/[^\p{L}\p{N}\s,'-]/gu, " ").replace(/\s+/g, " ").trim();

  return { location: location || q, disaster_type };
}

export type RunHandlers = {
  onProgress?: (p: AnalyzeProgress) => void;
  onBandLog?: (messages: BandMessage[]) => void;
  signal?: AbortSignal;
};

/**
 * Run the full pipeline for a query. Returns the adapted result. If no backend is
 * configured (NEXT_PUBLIC_API_URL unset), falls back to the bundled demo result.
 */
export async function runAnalysis(query: string, handlers: RunHandlers = {}): Promise<HazardResultLoad> {
  const base = apiBase();
  if (!base) {
    // No backend: demo mode.
    return loadHazardResult(query);
  }

  const { location, disaster_type } = parseQuery(query);

  // 1) Start the job.
  const startRes = await fetch(`${base}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ location, disaster_type }),
    signal: handlers.signal,
  });
  if (!startRes.ok) {
    throw new Error(`/analyze failed: ${startRes.status}`);
  }
  const { job_id } = (await startRes.json()) as { job_id: string };

  // 2) Poll status + band log until complete. The real pipeline can run for many
  // minutes, so we keep streaming the Band conversation for a long window.
  const deadline = Date.now() + 40 * 60_000;
  let lastStatus = "processing";
  while (Date.now() < deadline) {
    if (handlers.signal?.aborted) throw new DOMException("aborted", "AbortError");

    const [statusRes, bandRes] = await Promise.allSettled([
      fetch(`${base}/status/${job_id}`, { cache: "no-store", signal: handlers.signal }),
      loadBandLog(job_id, handlers.signal),
    ]);

    if (statusRes.status === "fulfilled" && statusRes.value.ok) {
      const s = (await statusRes.value.json()) as AnalyzeProgress;
      lastStatus = s.status;
      handlers.onProgress?.(s);
    }
    if (bandRes.status === "fulfilled" && bandRes.value.length) {
      handlers.onBandLog?.(bandRes.value);
    }

    if (lastStatus === "complete" || lastStatus === "failed") break;
    await new Promise((r) => setTimeout(r, 2500));
  }

  // Final band-log pull so the LAST messages (report agent's completion, verdict)
  // are never missed by the break above.
  try {
    const finalLog = await loadBandLog(job_id, handlers.signal);
    if (finalLog.length) handlers.onBandLog?.(finalLog);
  } catch {
    /* ignore */
  }

  // 3) Fetch the final result (loadHazardResult adapts the backend shape).
  return loadHazardResult(job_id);
}
