"use client";

import { X } from "lucide-react";
import { useEffect, type CSSProperties, type ReactNode } from "react";

type DialFocusedPanelProps = {
  moduleLabel: string;
  origin?: string;
  children: ReactNode;
  onClose: () => void;
};

export function DialFocusedPanel({ moduleLabel, origin = "0deg", children, onClose }: DialFocusedPanelProps) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="dial-focus-layer fixed inset-0 z-[90] flex items-center justify-center px-3 py-5 sm:px-6">
      <button
        aria-label="Close expanded module"
        className="dial-focus-backdrop absolute inset-0 cursor-default"
        onClick={onClose}
        type="button"
      />
      <section
        aria-label={moduleLabel}
        aria-modal="true"
        className="dial-focused-panel relative z-10 flex max-h-[88vh] w-full max-w-[1040px] flex-col overflow-hidden"
        role="dialog"
        style={{ "--focus-origin": origin } as CSSProperties}
      >
        <header className="relative z-10 flex shrink-0 items-center justify-between gap-3 border-b border-cyan-300/18 px-4 py-3">
          <div className="min-w-0">
            <p className="font-mono text-[10px] uppercase tracking-[0.26em] text-cyan-200/80">
              module expanded / dial focus
            </p>
            <h2 className="truncate text-lg font-semibold text-cyan-50">{moduleLabel}</h2>
          </div>
          <button className="dial-focus-close" onClick={onClose} type="button" aria-label="Close expanded module">
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="thin-scrollbar relative z-10 min-h-0 flex-1 overflow-y-auto p-4">
          {children}
        </div>
      </section>
    </div>
  );
}
