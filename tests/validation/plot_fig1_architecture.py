"""Renders Fig. 1 (System Architecture) — the four-stage HazardMind pipeline
(Satellite -> Hazard -> Impact -> Report), with the uncertainty/confidence
propagation path drawn explicitly alongside the data path, and the design
invariant ("a stage lacking sufficient evidence withholds a determination")
called out.

This is a static, publication-quality diagram (matplotlib, vector-clean at
high DPI), not a screenshot of a live tool -- appropriate for direct
embedding in a paper figure.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
from matplotlib.lines import Line2D

HERE = Path(__file__).resolve().parent
OUT = HERE / "figure_data" / "fig1_architecture.png"
OUT.parent.mkdir(exist_ok=True)

# --- Palette --------------------------------------------------------------
STAGE_FILL = "#eef3f8"
STAGE_EDGE = "#2c5f8a"
DATA_ARROW = "#2c5f8a"
CONF_ARROW = "#c0392b"
GATE_FILL = "#fdecea"
GATE_EDGE = "#c0392b"
TEXT_DARK = "#1a1a1a"

fig, ax = plt.subplots(figsize=(12.2, 6.4), dpi=220)
ax.set_xlim(-6, 122)
ax.set_ylim(-3, 52)
ax.axis("off")

stage_names = ["Satellite", "Hazard", "Impact", "Report"]
stage_subtitles = [
    "SAR / optical\nchange detection",
    "Flood / EQ / landslide\nrisk determination",
    "Population &\ninfrastructure exposure",
    "Confidence-annotated\nexecutive output",
]
stage_w, stage_h = 20, 20
stage_y = 22
xs = [8, 36, 64, 92]

# --- Stage boxes ------------------------------------------------------------
for x, name, sub in zip(xs, stage_names, stage_subtitles):
    box = FancyBboxPatch(
        (x, stage_y), stage_w, stage_h,
        boxstyle="round,pad=0.6,rounding_size=1.5",
        linewidth=1.8, edgecolor=STAGE_EDGE, facecolor=STAGE_FILL,
    )
    ax.add_patch(box)
    ax.text(x + stage_w / 2, stage_y + stage_h - 4.2, name,
            ha="center", va="center", fontsize=13, fontweight="bold",
            color=TEXT_DARK)
    ax.text(x + stage_w / 2, stage_y + stage_h - 11, sub,
            ha="center", va="center", fontsize=8.3, color="#444444",
            linespacing=1.4)

    # Per-stage evidence gate chip
    gate = FancyBboxPatch(
        (x + 1.2, stage_y + 1.5), stage_w - 2.4, 5.2,
        boxstyle="round,pad=0.3,rounding_size=1.0",
        linewidth=1.1, edgecolor=GATE_EDGE, facecolor=GATE_FILL,
    )
    ax.add_patch(gate)
    ax.text(x + stage_w / 2, stage_y + 4.1, "evidence gate",
            ha="center", va="center", fontsize=7.3, color=GATE_EDGE,
            style="italic")

# --- Data-flow arrows (top) -------------------------------------------------
for x0, x1 in zip(xs[:-1], xs[1:]):
    arr = FancyArrowPatch(
        (x0 + stage_w, stage_y + stage_h - 5),
        (x1, stage_y + stage_h - 5),
        arrowstyle="-|>", mutation_scale=16,
        linewidth=1.8, color=DATA_ARROW,
    )
    ax.add_patch(arr)
ax.text(xs[0] - 6.5, stage_y + stage_h - 5, "data", rotation=90,
        ha="center", va="center", fontsize=8, color=DATA_ARROW)

# --- Confidence-propagation arrows (bottom, curved) -------------------------
for x0, x1 in zip(xs[:-1], xs[1:]):
    arr = FancyArrowPatch(
        (x0 + stage_w, stage_y + 3),
        (x1, stage_y + 3),
        arrowstyle="-|>", mutation_scale=16,
        linewidth=1.8, color=CONF_ARROW, linestyle=(0, (5, 2)),
    )
    ax.add_patch(arr)
ax.text(xs[0] - 6.5, stage_y + 3, "confidence", rotation=90,
        ha="center", va="center", fontsize=8, color=CONF_ARROW)

# --- Withheld-determination branch (Hazard stage, as an example) -----------
wx = xs[1] + stage_w / 2
ax.annotate(
    "", xy=(wx, stage_y - 7), xytext=(wx, stage_y),
    arrowprops=dict(arrowstyle="-|>", color=GATE_EDGE, linewidth=1.6,
                     linestyle="dashed"),
)
withheld_box = FancyBboxPatch(
    (wx - 15, stage_y - 15), 30, 7,
    boxstyle="round,pad=0.5,rounding_size=1.2",
    linewidth=1.3, edgecolor=GATE_EDGE, facecolor="white",
)
ax.add_patch(withheld_box)
ax.text(wx, stage_y - 11.5,
        "insufficient evidence →\nWITHHELD, not degraded",
        ha="center", va="center", fontsize=7.8, color=GATE_EDGE,
        fontweight="bold", linespacing=1.3)

# --- LLM boundary note -------------------------------------------------------
note_y = 1.6
ax.add_patch(Rectangle((0, note_y - 2.6), 116, 5.6, linewidth=1.0,
                        edgecolor="#999999", facecolor="#fbfbfb"))
ax.text(
    58, note_y + 1.0,
    "Risk-level determinations are computed deterministically at every stage.",
    ha="center", va="center", fontsize=8.6, color="#333333", fontweight="bold",
)
ax.text(
    58, note_y - 1.1,
    "An LLM may draft narrative text or flag anomalies for review, but never sets a risk level or overrides an evidence gate.",
    ha="center", va="center", fontsize=8.6, color="#333333",
)

# --- Legend -------------------------------------------------------------
legend_elems = [
    Line2D([0], [0], color=DATA_ARROW, lw=1.8, label="Data flow"),
    Line2D([0], [0], color=CONF_ARROW, lw=1.8, linestyle=(0, (5, 2)),
           label="Confidence propagation"),
]
ax.legend(handles=legend_elems, loc="upper center", ncol=2, fontsize=9,
          frameon=False, bbox_to_anchor=(0.5, 1.0))

ax.set_title(
    "HazardMind: four-stage pipeline with per-stage evidence gating",
    fontsize=12.5, fontweight="bold", pad=10,
)

fig.subplots_adjust(top=0.86, bottom=0.02, left=0.03, right=0.99)
fig.savefig(OUT, bbox_inches="tight")
print(f"Saved {OUT}")
