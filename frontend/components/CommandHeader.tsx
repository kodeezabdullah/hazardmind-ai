"use client";

import { ChevronDown, Download, FileText, Map as MapIcon, MapPin } from "lucide-react";
import Image from "next/image";
import { useEffect, useRef, useState } from "react";
import type { HazardMindResult } from "../lib/types";

type CommandHeaderProps = {
  result: HazardMindResult;
  dataSource: string;
};

export function CommandHeader({ result }: CommandHeaderProps) {
  const [mapsOpen, setMapsOpen] = useState(false);
  const dropRef = useRef<HTMLDivElement>(null);

  // Close the maps dropdown on outside click.
  useEffect(() => {
    const onClick = (event: MouseEvent) => {
      if (dropRef.current && !dropRef.current.contains(event.target as Node)) {
        setMapsOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const pdfUrl = result.report?.pdf_url || "";
  const artifacts = result.artifacts ?? {
    true_color_url: "",
    index_url: "",
    classification_url: "",
    geojson_url: "",
  };

  const mapItems = [
    { label: "True-colour image", url: artifacts.true_color_url },
    { label: "NDWI index map", url: artifacts.index_url },
    { label: "Classification map", url: artifacts.classification_url },
    { label: "Hazard zones (GeoJSON)", url: artifacts.geojson_url },
  ];

  const download = (url: string) => {
    if (!url) return;
    window.open(url, "_blank", "noopener,noreferrer");
  };

  return (
    <header className="command-topbar">
      <div className="flex min-w-0 items-center gap-3">
        <div className="command-logo-frame">
          <Image
            src="/hazardmind-logo.png"
            alt="HazardMind AI"
            width={220}
            height={72}
            priority
            className="h-14 max-h-[56px] w-auto max-w-[220px] object-contain drop-shadow-[0_0_18px_rgba(34,211,238,0.35)]"
          />
        </div>

        <div className="command-head-meta">
          <span className="command-live-badge">
            <span className="command-live-dot" />
            Live
          </span>
          <span className="command-head-location">
            <MapPin className="h-4 w-4 text-cyan-200" />
            {result.location}
          </span>
        </div>
      </div>

      {/* Download actions */}
      <div className="command-actions">
        <button
          type="button"
          className="command-action-btn"
          onClick={() => download(pdfUrl)}
          disabled={!pdfUrl}
          title={pdfUrl ? "Download report PDF" : "Report not available yet"}
        >
          <FileText className="h-4 w-4" />
          Download Report
        </button>

        <div className="command-action-split" ref={dropRef}>
          <button
            type="button"
            className="command-action-btn command-action-main"
            onClick={() => download(artifacts.index_url)}
            disabled={!artifacts.index_url}
            title="Download risk map"
          >
            <MapIcon className="h-4 w-4" />
            Download Risk Map
          </button>
          <button
            type="button"
            className="command-action-caret"
            onClick={() => setMapsOpen((v) => !v)}
            aria-label="More map downloads"
          >
            <ChevronDown className="h-4 w-4" />
          </button>

          {mapsOpen ? (
            <div className="command-maps-menu">
              <p className="command-maps-title">All map layers</p>
              {mapItems.map((item) => (
                <button
                  key={item.label}
                  type="button"
                  className="command-maps-item"
                  onClick={() => {
                    download(item.url);
                    setMapsOpen(false);
                  }}
                  disabled={!item.url}
                >
                  <Download className="h-3.5 w-3.5" />
                  {item.label}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}
