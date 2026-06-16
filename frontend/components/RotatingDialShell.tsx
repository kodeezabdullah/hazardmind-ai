"use client";

import type { CSSProperties } from "react";

type RotatingDialShellProps = {
  rotation: number;
  selectedAngle?: number;
};

export function RotatingDialShell({ rotation, selectedAngle = 0 }: RotatingDialShellProps) {
  return (
    <div
      aria-hidden="true"
      className="rotating-dial-shell"
      style={{ "--dial-rotation": `${rotation}deg`, "--selected-angle": `${selectedAngle}deg` } as CSSProperties}
    >
      <div className="dial-ring dial-ring-outer" />
      <div className="dial-ring dial-ring-mid" />
      <div className="dial-ring dial-ring-inner" />
      <div className="dial-radar-sweep" />
      {Array.from({ length: 48 }, (_, index) => (
        <span
          className={index % 6 === 0 ? "dial-tick is-major" : "dial-tick"}
          key={index}
          style={{ "--tick-angle": `${index * 7.5}deg` } as CSSProperties}
        />
      ))}
      {Array.from({ length: 8 }, (_, index) => (
        <span
          className="dial-sector-line"
          key={index}
          style={{ "--sector-angle": `${index * 45}deg` } as CSSProperties}
        />
      ))}
      <span className="dial-selected-sector" />
    </div>
  );
}
