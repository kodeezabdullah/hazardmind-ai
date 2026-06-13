import { Download, FileText, MapPinned } from "lucide-react";
import type { HazardMindResult } from "../lib/types";

type ReportActionsProps = {
  result: HazardMindResult;
};

export function ReportActions({ result }: ReportActionsProps) {
  const disabled = !result.report.pdf_url;

  return (
    <section className="mt-3 grid grid-cols-2 gap-2">
      <button
        className="flex items-center justify-center gap-2 rounded-md border border-cyan-300/24 bg-cyan-300/10 px-2.5 py-1.5 text-xs font-semibold text-cyan-50 transition hover:border-cyan-200/50 hover:bg-cyan-300/16 disabled:cursor-not-allowed disabled:opacity-55"
        disabled={disabled}
        type="button"
      >
        <FileText className="h-4 w-4" />
        PDF Report
      </button>
      <button
        className="flex items-center justify-center gap-2 rounded-md border border-violet-300/24 bg-violet-300/10 px-2.5 py-1.5 text-xs font-semibold text-violet-50 transition hover:border-violet-200/50 hover:bg-violet-300/16 disabled:cursor-not-allowed disabled:opacity-55"
        disabled={!result.report.map_url}
        type="button"
      >
        <MapPinned className="h-4 w-4" />
        Map Export
      </button>
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
