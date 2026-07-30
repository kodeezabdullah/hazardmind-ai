"""Renders Fig. 2 (Bidirectional Mechanism) — two panels:
  (a) change-image histogram showing the decrease (drop) and increase (rise)
      populations with their independently-derived asymmetric thresholds
      marked
  (b) decrease-only detected extent vs. bidirectional detected extent,
      both plotted against the reference outline, side by side

This is the visual proof of the 45x F1 improvement documented in
SCIENCE_LOG.md and reproduced (42.9x on this re-run) via
export_fig2_bidirectional.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
DATA = HERE / "figure_data"
OUT = DATA / "fig2_bidirectional.png"

change = np.load(DATA / "keramidi_change_dB.npy")
drop_mask = np.load(DATA / "keramidi_drop_mask.npy")
rise_mask = np.load(DATA / "keramidi_rise_mask.npy")
bidir_mask = np.load(DATA / "keramidi_bidirectional_mask.npy")
ref_mask = np.load(DATA / "keramidi_reference_mask.npy")
summary = json.loads((DATA / "keramidi_bidirectional_summary.json").read_text())

drop_thr = summary["drop_threshold_db"]
rise_thr = summary["rise_threshold_db"]

fig = plt.figure(figsize=(11, 5.2), dpi=200)
gs = fig.add_gridspec(1, 3, width_ratios=[1.3, 1, 1], wspace=0.35, top=0.80, bottom=0.14)

# --- Panel (a): histogram with both thresholds --------------------------
ax0 = fig.add_subplot(gs[0])
bins = np.linspace(-8, 8, 161)
ax0.hist(change, bins=bins, density=True, color="#6f6f6f", alpha=0.75,
         edgecolor="none", label="All valid pixels")
ax0.axvline(drop_thr, color="#c0392b", linestyle="--", linewidth=1.4,
            label=f"Drop threshold: {drop_thr:+.2f} dB")
ax0.axvline(rise_thr, color="#1f6fb2", linestyle="--", linewidth=1.4,
            label=f"Rise threshold: {rise_thr:+.2f} dB")
ax0.axvspan(-8, drop_thr, color="#c0392b", alpha=0.08)
ax0.axvspan(rise_thr, 8, color="#1f6fb2", alpha=0.08)
ax0.set_xlabel("SAR change value (log-ratio, dB)")
ax0.set_ylabel("Density")
ax0.set_title("(a) Asymmetric thresholds,\nindependently derived")
ax0.legend(loc="upper right", fontsize=7.5, frameon=False)
ax0.set_xlim(-8, 8)

# --- Panel (b): decrease-only extent -------------------------------------
def _extent_panel(ax, detected_mask, title, color):
    h, w = ref_mask.shape
    rgb = np.ones((h, w, 3))
    # Reference outline: light fill
    rgb[ref_mask] = [1.0, 0.87, 0.70]
    # Detected pixels: colored
    rgb[detected_mask] = np.array(color) / 255.0
    # Overlap: darker blend
    overlap = detected_mask & ref_mask
    rgb[overlap] = (np.array(color) / 255.0 * 0.6 + np.array([1.0, 0.55, 0.0]) * 0.4)
    ax.imshow(rgb, interpolation="nearest")
    ax.set_title(title, fontsize=9.5)
    ax.set_xticks([]); ax.set_yticks([])

ax1 = fig.add_subplot(gs[1])
_extent_panel(
    ax1, drop_mask,
    f"(b) Decrease-only\nF1={summary['drop_only_score']['f1']:.3f}",
    (192, 57, 43),
)

ax2 = fig.add_subplot(gs[2])
_extent_panel(
    ax2, bidir_mask,
    f"(c) Bidirectional\nF1={summary['bidirectional_score']['f1']:.3f}  "
    f"({summary['f1_improvement_factor']:.1f}x)",
    (31, 111, 178),
)

# Shared legend for panels b/c
from matplotlib.patches import Patch
legend_elems = [
    Patch(facecolor="#ffddb3", label="Reference only"),
    Patch(facecolor="#ff8c00", label="Overlap"),
    Patch(facecolor="none", label=""),
]
fig.legend(handles=legend_elems, loc="lower center", ncol=2, fontsize=7.5,
           frameon=False, bbox_to_anchor=(0.68, -0.02))

fig.suptitle(
    "Keramidi (EMSR271): bidirectional detection recovers the double-bounce "
    f"rise signal ({summary['rise_px_count']:,} px vs {summary['drop_px_count']:,} drop px)",
    fontsize=10, y=0.98,
)
fig.savefig(OUT)
print(f"Saved {OUT}")
