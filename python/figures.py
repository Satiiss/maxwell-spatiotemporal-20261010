"""Automatic English-labelled probe figures, 600dpi PNG and editable PDF/SVG."""
from __future__ import annotations

from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
import numpy as np


def configure():
    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
                         "font.size": 6, "axes.labelcolor": "#303C43", "xtick.color": "#303C43",
                         "ytick.color": "#303C43", "axes.linewidth": 0.5, "svg.fonttype": "none",
                         "pdf.fonttype": 42, "figure.facecolor": "white", "axes.facecolor": "white"})


def save(fig, stem):
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("png", "pdf", "svg"):
        fig.savefig(stem.with_suffix("." + suffix), dpi=600, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def unit_map(mapping, units, stem):
    configure()
    fig, ax = plt.subplots(figsize=(3.50, 2.5))
    ax.scatter([r["x"] for r in mapping], [r["y"] for r in mapping], s=1, color="#D8DEE9")
    positions = {r["electrode"]: r for r in mapping}
    for i, unit in enumerate(units):
        color = plt.get_cmap("viridis")((i + 1) / max(len(units), 1))
        for electrode in unit["electrodes"]:
            row = positions[electrode]
            ax.scatter(row["x"], row["y"], s=12, marker="*" if electrode == unit.get("center") else "o", color=color)
        center = positions[unit.get("center", unit["electrodes"][0])]
        ax.annotate(f"#{unit.get('rank', i+1)}: {center['electrode']}", (center["x"], center["y"]), xytext=(2, 4), textcoords="offset points", fontsize=5)
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    length = 100.0
    x, y = xmin + (xmax - xmin) * .06, ymax - (ymax - ymin) * .08
    ax.plot([x, x + length], [y, y], color="#303C43", lw=1)
    ax.text(x + length / 2, y - (ymax - ymin) * .03, "100 µm", ha="center", fontsize=5)
    ax.set(xlabel="X (µm)", ylabel="Y (µm)", aspect="equal")
    ax.invert_yaxis()
    ax.grid(False)
    save(fig, stem)


def raster(session, stim_times, stem):
    configure()
    fig, ax = plt.subplots(figsize=(3.50, 2.3))
    # Probe files are short; retain every valid mapped spike.
    ax.scatter(session.times, session.channels, s=.03, color="#303C43", linewidths=0, rasterized=True)
    for t in stim_times:
        ax.axvline(t, color="#F26B59", lw=.3, alpha=.7)
    ax.set(xlabel="Time (s)", ylabel="Channel", xlim=(0, session.duration_sec))
    ax.grid(False)
    save(fig, stem)


def summary_table(rows, stem):
    configure()
    fig, ax = plt.subplots(figsize=(7.20, max(1.5, .17 * len(rows) + .5)))
    ax.axis("off")
    columns = ["Unit", "Electrode", "Success", "Artifact 0–3 ms", "Response 10–50 ms", "Eligible", "Status"]
    cells = [[r["unit_id"], str(r["electrode"]), f"{r['success_trials']}/{r['all_trials']}",
              "N/A" if r["artifact_0_3ms_mean"] is None else f"{r['artifact_0_3ms_mean']:.1f}",
              "N/A" if r["response_10_50ms_mean"] is None else f"{r['response_10_50ms_mean']:.1f}",
              "Y" if r["eligible"] else "N", r.get("status", "complete")] for r in rows]
    table = ax.table(cellText=cells, colLabels=columns, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(5)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#D8DEE9")
        cell.set_linewidth(.3)
        cell.set_facecolor("#A4C8E1" if row == 0 else ("#FFC0CB" if not rows[row - 1]["eligible"] else "white"))
    save(fig, stem)
