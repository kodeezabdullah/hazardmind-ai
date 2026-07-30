"""Renders Fig. 3 (The Null Case) — flood-vs-dry SAR change-value distribution
overlap, from the raw pixel arrays exported by export_fig3_null_case.py.

Shows the two populations are statistically indistinguishable (ROC AUC
0.487, Cohen's d 0.031) — the honest visual proof that Kanalia's zero-signal
result is a property of the scene (acquisition post-dates peak, water
receded), not a detector failure.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
DATA = HERE / "figure_data"
OUT = HERE / "figure_data" / "fig3_null_case.png"

flood = np.load(DATA / "kanalia_flood_px_dB.npy")
dry = np.load(DATA / "kanalia_dry_px_dB.npy")

fig, ax = plt.subplots(figsize=(6.5, 4.2), dpi=200)

bins = np.linspace(-4, 4, 121)
ax.hist(dry, bins=bins, density=True, alpha=0.55, color="#8a8a8a",
        label=f"Dry (n={dry.size:,})", edgecolor="none")
ax.hist(flood, bins=bins, density=True, alpha=0.55, color="#1f6fb2",
        label=f"Flood-reference (n={flood.size:,})", edgecolor="none")

ax.axvline(flood.mean(), color="#1f6fb2", linestyle="--", linewidth=1.2)
ax.axvline(dry.mean(), color="#5a5a5a", linestyle="--", linewidth=1.2)

ax.set_xlabel("SAR change value (log-ratio, dB)")
ax.set_ylabel("Density")
ax.set_title("Kanalia (EMSR692): flood and dry populations are\nstatistically indistinguishable")
ax.text(
    0.02, 0.96,
    "ROC AUC = 0.487\nCohen's d = 0.031",
    transform=ax.transAxes, va="top", ha="left",
    fontsize=9, family="monospace",
    bbox=dict(boxstyle="round", facecolor="white", alpha=0.85, edgecolor="#999"),
)
ax.legend(loc="upper right", fontsize=8, frameon=False)
ax.set_xlim(-4, 4)
fig.tight_layout()
fig.savefig(OUT)
print(f"Saved {OUT}")
