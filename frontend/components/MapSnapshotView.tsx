"use client";

import { AlertTriangle, ArrowLeft, CheckCircle2, MapPinned } from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useEffect, useState } from "react";
import { HazardMap } from "./HazardMap";
import { loadHazardResult, type HazardResultSource } from "../lib/loadHazardResult";
import { emptyResult as sampleResult } from "../lib/sampleResult";
import type { HazardMindResult, LayerState } from "../lib/types";

type MapSnapshotViewProps = {
  eventId: string;
};

const snapshotLayers: LayerState = {
  hazardZones: true,
  boundary: true,
  facilities: true,
  evacuationRoutes: true,
  satellite: false,
  index: false,
  classification: false,
};

const severityTone = {
  CRITICAL: "border-red-400/35 bg-red-500/12 text-red-100",
  HIGH: "border-orange-300/35 bg-orange-400/12 text-orange-100",
  MEDIUM: "border-yellow-300/35 bg-yellow-300/12 text-yellow-100",
  LOW: "border-emerald-300/35 bg-emerald-300/12 text-emerald-100",
};

export function MapSnapshotView({ eventId }: MapSnapshotViewProps) {
  const [result, setResult] = useState<HazardMindResult>(sampleResult);
  const [source, setSource] = useState<HazardResultSource>("demo-fallback");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let ignore = false;

    async function loadResult() {
      setIsLoading(true);
      try {
        const loaded = await loadHazardResult(eventId);
        if (!ignore) {
          setResult(loaded.result);
          setSource(loaded.source);
          setWarnings(loaded.warnings);
        }
      } catch {
        if (!ignore) {
          setResult(sampleResult);
          setSource("demo-fallback");
          setWarnings(["Unable to load requested result; showing bundled demo fallback."]);
        }
      } finally {
        if (!ignore) {
          setIsLoading(false);
        }
      }
    }

    loadResult();

    return () => {
      ignore = true;
    };
  }, [eventId]);

  return (
    <main className="map-snapshot-page">
      <div className="command-bg-grid" />
      <div className="command-bg-glow" />
      <div className="command-scanlines" />

      <section className="map-snapshot-shell">
        <header className="map-snapshot-header">
          <div className="map-snapshot-brand">
            <Image
              alt="HazardMind AI"
              className="h-12 w-auto object-contain drop-shadow-[0_0_18px_rgba(34,211,238,0.35)]"
              height={64}
              priority
              src="/hazardmind-logo.png"
              width={220}
            />
            <div className="min-w-0">
              <p className="hud-eyebrow">map snapshot</p>
              <h1>{result.location}</h1>
              <span>EVENT: {result.event_id || eventId}</span>
            </div>
          </div>

          <div className="map-snapshot-actions">
            <span className={`map-snapshot-chip ${severityTone[result.overall_severity]}`}>
              {result.overall_severity}
            </span>
            <span className="map-snapshot-chip border-cyan-300/30 bg-cyan-300/10 text-cyan-100">
              {result.hazard_type}
            </span>
            <Link className="map-snapshot-back" href="/">
              <ArrowLeft className="h-4 w-4" />
              Back to dashboard
            </Link>
          </div>
        </header>

        <section className="map-snapshot-body">
          <div className="map-snapshot-frame">
            <HazardMap result={result} layers={snapshotLayers} showHud />
            <div className="map-snapshot-watermark">
              <MapPinned className="h-4 w-4 text-cyan-200" />
              <span>{isLoading ? "Loading result" : `Data source: ${source === "backend" ? "Backend" : "Demo fallback"}`}</span>
            </div>
          </div>

          <aside className="map-snapshot-legend" aria-label="Map snapshot legend">
            <h2>Layer Status</h2>
            <SnapshotLayer active label="Hazard zones" />
            <SnapshotLayer active label="Boundary" />
            <SnapshotLayer active label="Facilities" />
            <SnapshotLayer active label="Evac routes" />
            <div className="mt-4 grid grid-cols-2 gap-2">
              <SnapshotMetric label="Zones" value={String(result.analysis.total_zones)} />
              <SnapshotMetric label="Area" value={`${result.analysis.affected_area_km2} km2`} />
              <SnapshotMetric label="Damage" value={`${result.analysis.damage_percent}%`} />
              <SnapshotMetric label="Hospitals" value={String(result.impact.hospitals_at_risk)} />
            </div>
            {warnings.length ? (
              <div className="map-snapshot-warning">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                <span>{warnings[0]}</span>
              </div>
            ) : (
              <div className="map-snapshot-ready">
                <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
                Snapshot layers ready
              </div>
            )}
          </aside>
        </section>
      </section>
    </main>
  );
}

function SnapshotLayer({ active, label }: { active: boolean; label: string }) {
  return (
    <div className="map-snapshot-layer-row">
      <span className={active ? "is-active" : ""} />
      <strong>{label}</strong>
      <small>{active ? "active" : "off"}</small>
    </div>
  );
}

function SnapshotMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="mini-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
