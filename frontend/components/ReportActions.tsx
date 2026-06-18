"use client";

import { Download, FileText, MapPinned } from "lucide-react";
import type { HazardMindResult } from "../lib/types";

type ReportActionsProps = {
  result: HazardMindResult;
  currentEventId?: string;
};

export function ReportActions({ result, currentEventId }: ReportActionsProps) {
  const pdfDisabled = !result.report.pdf_url;
  const eventId = currentEventId || result.event_id;
  const mapDisabled = !eventId;
  const mapHref = eventId ? resolveMapSnapshotUrl(eventId, result.report.map_url) : "";
  const mapIsCurrentPage = Boolean(currentEventId && currentEventId === eventId);

  function handleOpenMapSnapshot() {
    if (!eventId) {
      return;
    }
    window.open(resolveMapSnapshotUrl(eventId, result.report.map_url), "_blank", "noopener,noreferrer");
  }

  return (
    <section className="mt-3 grid grid-cols-2 gap-2">
      {pdfDisabled ? (
        <button
          className="flex cursor-not-allowed items-center justify-center gap-2 rounded-md border border-cyan-300/24 bg-cyan-300/10 px-2.5 py-1.5 text-xs font-semibold text-cyan-50 opacity-55 transition"
          disabled
          type="button"
        >
          <FileText className="h-4 w-4" />
          PDF Pending
        </button>
      ) : (
        <a
          className="flex items-center justify-center gap-2 rounded-md border border-cyan-300/24 bg-cyan-300/10 px-2.5 py-1.5 text-xs font-semibold text-cyan-50 transition hover:border-cyan-200/50 hover:bg-cyan-300/16"
          download
          href={result.report.pdf_url}
          rel="noreferrer"
          target="_blank"
        >
          <FileText className="h-4 w-4" />
          PDF Report
        </a>
      )}
      {mapDisabled || mapIsCurrentPage ? (
        <button
          className="flex cursor-not-allowed items-center justify-center gap-2 rounded-md border border-violet-300/24 bg-violet-300/10 px-2.5 py-1.5 text-xs font-semibold text-violet-50 opacity-55 transition"
          disabled
          type="button"
        >
          <MapPinned className="h-4 w-4" />
          {mapIsCurrentPage ? "Current Map View" : "Map Pending"}
        </button>
      ) : (
        <a
          className="flex items-center justify-center gap-2 rounded-md border border-violet-300/24 bg-violet-300/10 px-2.5 py-1.5 text-xs font-semibold text-violet-50 transition hover:border-violet-200/50 hover:bg-violet-300/16"
          href={mapHref}
          onClick={(event) => {
            event.preventDefault();
            handleOpenMapSnapshot();
          }}
          rel="noreferrer"
          target="_blank"
        >
          <MapPinned className="h-4 w-4" />
          Open Map Snapshot
        </a>
      )}
      <button
        className="col-span-2 flex items-center justify-center gap-2 rounded-md border border-white/10 bg-white/[0.04] px-2.5 py-1.5 text-xs font-semibold text-slate-200 transition hover:border-cyan-300/28 hover:bg-cyan-300/[0.06]"
        type="button"
      >
        <Download className="h-4 w-4" />
        Final Package Pending
      </button>
    </section>
  );
}

function resolveMapSnapshotUrl(eventId: string, mapUrl?: string | null) {
  const fallbackPath = `/map/${encodeURIComponent(eventId)}`;

  if (typeof window === "undefined") {
    return fallbackPath;
  }

  const origin = window.location.origin;

  if (origin.includes("localhost") || origin.includes("127.0.0.1")) {
    return `${origin}${fallbackPath}`;
  }

  if (!mapUrl || mapUrl.includes("vercel.app")) {
    return `${origin}${fallbackPath}`;
  }

  if (mapUrl.startsWith("/")) {
    return `${origin}${mapUrl}`;
  }

  return mapUrl;
}
