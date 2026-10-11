"""Offline spontaneous-network figures using every qualified edge."""
from __future__ import annotations

from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize


def render_network_figures(session, result, candidates, output, config, revision=1, progress=None):
    """Save PNG/PDF/SVG; raster decimation affects display only, never metrics."""
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 6,
        "axes.labelsize": 6, "xtick.labelsize": 6, "ytick.labelsize": 6,
        "axes.linewidth": .5, "figure.facecolor": "white", "axes.facecolor": "white",
        "axes.grid": False, "svg.fonttype": "none", "pdf.fonttype": 42,
        "axes.labelcolor": "#303C43", "xtick.color": "#303C43", "ytick.color": "#303C43"})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    metrics = result["metrics"]
    positions = {r["electrode"]: (r["x_um"], r["y_um"]) for r in metrics}
    xy = np.asarray(list(positions.values()), dtype=float).reshape(-1, 2)
    cmap = LinearSegmentedColormap.from_list("spontaneous", ["#7f3f98", "#e46240", "#ffde17"])
    stars = candidates[:config["star_count"]]
    manifest = []

    def save(fig, name, **metadata):
        fig.text(.01, .01, f"Parameter revision: {revision}", fontsize=6)
        fig.tight_layout(rect=(0, .035, 1, 1))
        for extension in config["figure_formats"]:
            fig.savefig(output / f"{name}.{extension}", dpi=config["dpi"], facecolor="white")
        plt.close(fig)
        manifest.append(dict(name=name, **metadata))
        if progress:
            progress(len(manifest), 6, name)

    def spatial():
        fig, ax = plt.subplots(figsize=(3.4, 2.8))
        ax.set(xlabel="x (um)", ylabel="y (um)")
        ax.set_aspect("equal")
        ax.spines[["top", "right"]].set_visible(False)
        if len(xy):
            span = np.maximum(np.ptp(xy, axis=0), [120, 120])
            lo, hi = xy.min(axis=0), xy.max(axis=0)
            mid = (lo+hi)/2
            ax.set_xlim(mid[0] - .58*span[0], mid[0] + .58*span[0])
            ax.set_ylim(mid[1] - .80*span[1], mid[1] + .58*span[1])
            x, y = lo[0], mid[1] - .55*span[1]
            ax.plot([x, x+100], [y, y], color="#303C43", lw=1)
            ax.text(x+50, y-.035*span[1], "100 um", ha="center", va="top")
        return fig, ax

    def draw_stars(ax, include_electrode=False):
        if stars:
            values = [s["out_degree"] for s in stars]
            norm = Normalize(vmin=min(values)-.5, vmax=max(values)+.5)
            artist = ax.scatter([s["x_um"] for s in stars], [s["y_um"] for s in stars],
                c=values, cmap=cmap, norm=norm, marker="*", s=70, edgecolors="#303C43", linewidths=.4, zorder=5)
            for s in stars:
                label = f"#{s['rank']}: {s['electrode']}" if include_electrode else str(s["rank"])
                on_right = s["x_um"] > ax.get_xlim()[0]+.75*np.diff(ax.get_xlim())[0]
                ax.annotate(label, (s["x_um"], s["y_um"]), xytext=(-4 if on_right else 4, 8),
                    ha="right" if on_right else "left", textcoords="offset points")
            return artist
        ax.text(.02, .98, "No eligible centres", transform=ax.transAxes, va="top")
        return None

    # Deterministic thinning solely for an enormous raster's display.
    inside = np.isfinite(session.times) & (session.times >= 0) & (session.times < session.duration_sec)
    ids = np.flatnonzero(inside)
    total = len(ids)
    cap = int(config["raster_max_display_spikes"])
    if total > cap:
        ids = ids[np.linspace(0, total-1, cap, dtype=int)]
    fig, ax = plt.subplots(figsize=(5, 2.5))
    ax.scatter(session.times[ids], session.channels[ids], s=.1, color="#303C43", rasterized=True)
    ax.set(xlabel="Time (s)", ylabel="Recording channel", xlim=(0, session.duration_sec))
    ax.spines[["top", "right"]].set_visible(False)
    if total > cap:
        ax.text(.01, .98, f"Display: {len(ids):,} / {total:,} spikes; analysis: all", transform=ax.transAxes, va="top")
    save(fig, "baseline_raster", displayed_spikes=len(ids), analyzed_spikes=total)

    fig, ax = plt.subplots(figsize=(5, 2.5))
    diag = result["burst_diagnostics"]
    if diag:
        time = diag["edges_sec"][:-1]
        smooth = diag["smoothed_counts"]
        count = min(len(time), len(smooth))
        ax.plot(time[:count], smooth[:count], color="#303C43", lw=.4, rasterized=True)
        ax.axhline(diag["high_threshold"], color="#e46240", lw=.6, label="High threshold")
        ax.axhline(diag["low_threshold"], color="#7f3f98", lw=.6, label="Low threshold")
        ax.legend(frameon=False)
    else:
        ax.text(.02, .95, "Insufficient spikes for burst detection", transform=ax.transAxes, va="top")
    for a, b in result["bursts"]:
        ax.axvspan(a, b, color="#ffde17", alpha=.25, linewidth=0)
    ax.set(xlabel="Time (s)", ylabel="Smoothed population spikes / bin", xlim=(0, session.duration_sec))
    ax.spines[["top", "right"]].set_visible(False)
    save(fig, "burst_overview", bursts=len(result["bursts"]))

    for field in ("out_degree", "in_degree"):
        fig, ax = spatial()
        if len(metrics):
            artist = ax.scatter(xy[:, 0], xy[:, 1], c=[r[field] for r in metrics], cmap=cmap, s=4,
            norm=Normalize(0, max(1, max(r[field] for r in metrics))), linewidths=0)
            fig.colorbar(artist, ax=ax, shrink=.65, label=field.replace("_", " ").capitalize())
        save(fig, field+"_map", electrodes=len(metrics))

    fig, ax = spatial()
    if len(metrics):
        rates = np.array([r["burst_firing_rate_hz"] for r in metrics])
        sizes = 3+12*rates/max(float(rates.max()), 1e-12)
        ax.scatter(xy[:, 0], xy[:, 1], s=sizes, color="#b3b6bd", linewidths=0, zorder=2)
    if result["edges"]:
        source = np.asarray([positions[e["source_electrode"]] for e in result["edges"]])
        target = np.asarray([positions[e["target_electrode"]] for e in result["edges"]])
        vectors = target-source
        ax.quiver(source[:, 0], source[:, 1], vectors[:, 0], vectors[:, 1], angles="xy",
            scale_units="xy", scale=1, width=.0015, headwidth=4, headlength=5,
            color="#303C43", alpha=config["arrow_alpha"], zorder=1)
    artist = draw_stars(ax)
    if artist is not None:
        fig.colorbar(artist, ax=ax, shrink=.65, label="Centre out degree")
    fig.text(.01, .045, "Node size: burst firing rate; stars: ranked centres", fontsize=6)
    save(fig, "spontaneous_network_all_edges", edges=len(result["edges"]), edge_cap=None)

    fig, ax = spatial()
    if len(xy):
        ax.scatter(xy[:, 0], xy[:, 1], s=3, color="#d1d3d8", linewidths=0)
    for centre in stars:
        unit_xy = np.asarray([positions[e] for e in centre["unit_electrodes"]])
        ax.scatter(unit_xy[:, 0], unit_xy[:, 1], s=18, facecolors="none", edgecolors="#303C43", linewidths=.6)
    artist = draw_stars(ax, include_electrode=True)
    if artist is not None:
        fig.colorbar(artist, ax=ax, shrink=.65, label="Centre out degree")
    fig.text(.01, .045, "Stars: centres; circles: centre + 3 nearest recording sites", fontsize=6)
    save(fig, "spontaneous_network_selected_sites", centres=len(stars))
    return manifest
