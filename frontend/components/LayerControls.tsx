import type { Artifacts, LayerKey, LayerState } from "../lib/types";

type LayerControl = {
  key: LayerKey;
  label: string;
  artifactKey?: keyof Artifacts;
};

const controls: LayerControl[] = [
  { key: "hazardZones", label: "Hazard zones" },
  { key: "boundary", label: "Boundary" },
  { key: "facilities", label: "Facilities" },
  { key: "evacuationRoutes", label: "Evac routes" },
  { key: "satellite", label: "Satellite", artifactKey: "true_color_url" },
  { key: "index", label: "Index raster", artifactKey: "index_url" },
  { key: "classification", label: "Classification", artifactKey: "classification_url" },
];

type LayerControlsProps = {
  artifacts: Artifacts;
  layers: LayerState;
  onToggleLayer: (layer: LayerKey) => void;
};

export function LayerControls({ artifacts, layers, onToggleLayer }: LayerControlsProps) {
  return (
    <div className="space-y-1.5">
      {controls.map((control) => {
        const pending = control.artifactKey ? !artifacts[control.artifactKey] : false;
        const active = layers[control.key];
        return (
          <label
            key={control.key}
            className={`flex cursor-pointer items-center justify-between gap-2 rounded-md border px-2.5 py-1.5 text-[11px] transition ${
              active
                ? "border-cyan-300/35 bg-cyan-300/[0.075] text-cyan-50"
                : "border-white/10 bg-white/[0.025] text-slate-300 hover:border-cyan-300/25 hover:bg-cyan-300/[0.045]"
            }`}
          >
            <span className="flex min-w-0 items-center gap-2">
              <span
                className={`h-2 w-2 shrink-0 rounded-full ${
                  pending ? "bg-amber-300 shadow-[0_0_10px_rgba(252,211,77,0.5)]" : "bg-cyan-300 shadow-[0_0_10px_rgba(34,211,238,0.7)]"
                }`}
              />
              <span className="truncate font-medium">{control.label}</span>
            </span>
            <span className="flex shrink-0 items-center gap-2">
              <span
                className={`rounded border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.12em] ${
                  pending
                    ? "border-amber-300/24 bg-amber-300/10 text-amber-200"
                    : active
                      ? "border-cyan-200/35 bg-cyan-200/12 text-cyan-100"
                      : "border-slate-500/25 bg-slate-500/10 text-slate-400"
                }`}
              >
                {pending ? "pending" : active ? "active" : "off"}
              </span>
              <span
                className={`relative h-4 w-7 rounded-full border transition ${
                  active ? "border-cyan-200/50 bg-cyan-300/25" : "border-slate-600 bg-slate-900"
                }`}
              >
                <span
                  className={`absolute top-1/2 h-2.5 w-2.5 -translate-y-1/2 rounded-full transition ${
                    active ? "left-3.5 bg-cyan-100 shadow-[0_0_9px_rgba(34,211,238,0.8)]" : "left-1 bg-slate-500"
                  }`}
                />
              </span>
              <input
                checked={active}
                className="sr-only"
                onChange={() => onToggleLayer(control.key)}
                type="checkbox"
              />
            </span>
          </label>
        );
      })}
    </div>
  );
}
