"""Pure spontaneous-network metrics; no hardware access or experiment-runner imports.

First-activation and histogram peak-ratio definitions follow the existing
spont_burst_outdegree_handoff/spont_analysis/core.py. All qualified edges are kept.
"""
from __future__ import annotations

from itertools import combinations
from math import hypot
import numpy as np

from .detection.burst import detect_burst_intervals


def histogram_peak_ratio(values, bin_ms, signed=False):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 3:
        return 0.0, None, 0
    if not np.isfinite(bin_ms) or bin_ms < 0.1:
        raise ValueError("直方图bin必须至少为0.1ms")
    width = float(bin_ms)
    if signed:
        limit = max(10.0, float(np.max(np.abs(values))) + width)
        edges = np.arange(-limit, limit + width * 1.01, width)
    else:
        limit = max(10.0, float(np.max(values)) + width)
        edges = np.arange(0, limit + width * 1.1, width)
    counts, edges = np.histogram(values, bins=edges)
    peak_index = int(np.argmax(counts))
    nonzero = counts[counts > 0]
    background = max(1.0, float(np.median(nonzero)))
    center = float((edges[peak_index] + edges[peak_index + 1]) / 2)
    return float(counts[peak_index] / background), center, int(counts[peak_index])


def first_activation_matrix(spike_lookup, channels, bursts):
    """Burst first-spike latencies in ms; NaN denotes absence in a burst.

    Retains the historical spontaneous-map detector's inclusive burst endpoints.
    The Probe response windows remain left-closed/right-open elsewhere.
    """
    first = np.full((len(channels), len(bursts)), np.nan)
    if not bursts:
        return first
    starts, stops = np.asarray(bursts, dtype=float).T
    for index, channel in enumerate(channels):
        times = np.asarray(spike_lookup.get(channel, []), dtype=float)
        lo = np.searchsorted(times, starts, side="left")
        hi = np.searchsorted(times, stops, side="right")
        participated = hi > lo
        first[index, participated] = (times[lo[participated]] - starts[participated]) * 1000
    return first


def directed_delay_edges(first, channels, electrode_by_channel, config, progress=None):
    """Orient the histogram peak: positive L_b-L_a means a -> b.

    No display edge cap and no silent relaxation of any threshold.
    Stable individual activation is a tag, not an additional pair filter.
    """
    eligible = np.flatnonzero(np.isfinite(first).sum(axis=1) > config["pair_min_shared_bursts"])
    total = len(eligible) * (len(eligible) - 1) // 2
    edges = []
    if progress:
        progress(0, max(total, 1))
    for completed, (ia, ib) in enumerate(combinations(eligible, 2), 1):
        a, b = first[ia], first[ib]
        shared_mask = np.isfinite(a) & np.isfinite(b)
        shared = int(shared_mask.sum())
        if shared > config["pair_min_shared_bursts"]:
            ratio, delay, peak_count = histogram_peak_ratio(
                b[shared_mask] - a[shared_mask], config["pair_bin_ms"], signed=True)
            if (delay is not None and ratio >= config["pair_peak_ratio_threshold"]
                    and abs(delay) > config["pair_min_abs_delay_ms"]):
                ca, cb = channels[ia], channels[ib]
                source, target = (ca, cb) if delay > 0 else (cb, ca)
                edges.append(dict(source_channel=int(source), target_channel=int(target),
                    source_electrode=int(electrode_by_channel[source]),
                    target_electrode=int(electrode_by_channel[target]),
                    shared_bursts=shared, delay_peak_ms=abs(delay), signed_cb_minus_ca_ms=delay,
                    delay_peak_ratio=ratio, delay_peak_count=peak_count))
        if progress and (completed % 2000 == 0 or completed == total):
            progress(completed, max(total, 1))
    if not total and progress:
        progress(1, 1)
    return edges


def analyze_spontaneous_network(session, burst_config, network_config, progress=None, burst_intervals=None):
    if not np.isfinite(session.duration_sec) or session.duration_sec <= 0:
        raise ValueError("需要H5记录帧给出的真实录制时长")
    times, channels = np.asarray(session.times), np.asarray(session.channels)
    if len(times) != len(channels) or (session.amplitudes is not None and len(session.amplitudes) != len(times)):
        raise ValueError("spike时间、通道及幅值必须逐项对齐")
    if np.any(np.diff(times) < 0):
        raise ValueError("spike时间必须已排序")
    in_recording = np.isfinite(times) & (times >= 0) & (times < session.duration_sec)
    times, channels = times[in_recording], channels[in_recording]
    amplitudes = None if session.amplitudes is None else np.asarray(session.amplitudes)[in_recording]
    # Group once, preserving the reader's temporal order; avoid N_channels*N_spikes scans.
    order = np.argsort(channels, kind="stable")
    grouped_channels = channels[order]
    unique, starts = np.unique(grouped_channels, return_index=True)
    pieces = np.split(order, starts[1:]) if len(unique) else []
    indices = {int(c): p for c, p in zip(unique, pieces)}
    lookup = {c: times[p] for c, p in indices.items()}
    bursts, diagnostics = detect_burst_intervals(lookup, **burst_config)
    if burst_intervals is not None:
        bursts = sorted((float(a), float(b)) for a, b in burst_intervals)
        if any(not np.isfinite([a, b]).all() or a < 0 or b <= a or b > session.duration_sec for a, b in bursts):
            raise ValueError("人工burst必须在完整录制范围内且end > start")
        if any(a <= previous_b for (_, previous_b), (a, _) in zip(bursts, bursts[1:])):
            raise ValueError("人工burst不能重叠或共享闭区间端点")
    bursts = [(max(0.0, a), min(session.duration_sec, b)) for a, b in bursts if b > a]
    rows = sorted(session.mapping, key=lambda row: row["channel"])
    channel_ids = np.array([row["channel"] for row in rows], dtype=int)
    if len(set(channel_ids)) != len(rows) or len({r["electrode"] for r in rows}) != len(rows):
        raise ValueError("H5映射含重复电极或通道")
    first = first_activation_matrix(lookup, channel_ids, bursts)
    electrode_by_channel = {r["channel"]: r["electrode"] for r in rows}
    if progress:
        progress("burst", 1, 1, burst_count=len(bursts))
    edges = directed_delay_edges(first, channel_ids, electrode_by_channel, network_config,
        None if progress is None else lambda n, total: progress("pairs", n, total))
    outgoing, incoming = {}, {}
    for edge in edges:
        outgoing[edge["source_electrode"]] = outgoing.get(edge["source_electrode"], 0) + 1
        incoming[edge["target_electrode"]] = incoming.get(edge["target_electrode"], 0) + 1
    metrics = []
    burst_starts = np.array([a for a, _ in bursts])
    burst_stops = np.array([b for _, b in bursts])
    for i, row in enumerate(rows):
        channel, electrode = row["channel"], row["electrode"]
        positions = indices.get(channel, np.array([], dtype=int))
        ch_times = lookup.get(channel, np.array([]))
        ratio, peak, peak_count = histogram_peak_ratio(first[i], network_config["electrode_bin_ms"])
        participating = int(np.isfinite(first[i]).sum())
        burst_spikes = int((np.searchsorted(ch_times, burst_stops, side="right") -
                            np.searchsorted(ch_times, burst_starts, side="left")).sum())
        amp = None
        if amplitudes is not None and len(positions):
            finite = np.abs(amplitudes[positions])
            finite = finite[np.isfinite(finite)]
            if finite.size:
                amp = float(finite.mean())
        metrics.append(dict(channel=int(channel), electrode=int(electrode), x_um=float(row["x"]),
            y_um=float(row["y"]), spike_count=len(positions),
            firing_rate_hz=len(positions) / session.duration_sec,
            mean_abs_amplitude=amp, burst_spike_count=burst_spikes,
            burst_firing_rate_hz=burst_spikes / session.duration_sec,
            bursts_participated=participating, burst_participation=participating / max(len(bursts), 1),
            first_activation_peak_ms=peak, first_activation_peak_count=peak_count,
            first_activation_peak_ratio=ratio,
            stable_activation=ratio >= network_config["electrode_peak_ratio_threshold"],
            out_degree=outgoing.get(electrode, 0), in_degree=incoming.get(electrode, 0)))
    metrics.sort(key=lambda r: (-r["out_degree"], -r["in_degree"], -r["firing_rate_hz"],
                               -(r["mean_abs_amplitude"] or 0), r["electrode"]))
    burst_table = [dict(burst_id=i + 1, start_sec=a, end_sec=b, duration_ms=(b-a)*1000,
        spike_count=int(np.searchsorted(times, b, side="right") - np.searchsorted(times, a, side="left")))
        for i, (a, b) in enumerate(bursts)]
    return dict(metrics=metrics, edges=edges, bursts=bursts, burst_table=burst_table,
                burst_diagnostics=diagnostics, first_activation=first, channels=channel_ids,
                excluded_outside_recording_spikes=int((~in_recording).sum()))


def select_network_candidates(metrics, config):
    """Suggest centres with 3x3 exclusion and disjoint exact nearest-3 units.

    No silent substitution of centres or nearest neighbours. Actual stimulation
    routing eligibility remains a hardware check in the subsequent Probe stage.
    """
    selected, occupied = [], set()
    sites = {r["electrode"]: r for r in metrics}
    radius = float(config["grid_neighborhood_radius_um"])
    ranked = sorted(metrics, key=lambda r: (-r["out_degree"], -r["in_degree"], -r["firing_rate_hz"],
                                           -(r["mean_abs_amplitude"] or 0), r["electrode"]))
    for row in ranked:
        if row["out_degree"] < config["minimum_outdegree"]:
            continue
        def distance(other):
            return hypot(row["x_um"] - other["x_um"], row["y_um"] - other["y_um"])
        if any(distance(previous) <= config["minimum_center_distance_um"] or
               (abs(row["x_um"] - previous["x_um"]) <= 2 * radius and
                abs(row["y_um"] - previous["y_um"]) <= 2 * radius) for previous in selected):
            continue
        nearest = sorted((r for r in sites.values() if r["electrode"] != row["electrode"]),
                         key=lambda r: (distance(r), r["electrode"]))[:3]
        if len(nearest) != 3:
            continue
        unit = [row["electrode"], *(r["electrode"] for r in nearest)]
        if occupied.intersection(unit):
            continue
        occupied.update(unit)
        selected.append(dict(row, rank=len(selected)+1, unit_electrodes=unit))
        if len(selected) == config["selection_count"]:
            break
    return selected
