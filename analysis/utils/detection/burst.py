"""Burst detection copied from interactive_pipeline2 and exposed as parameters."""
from __future__ import annotations

import numpy as np


def _density_fallback(spikes, min_duration_s, min_spikes, merge_gap_s):
    intervals, stop_idx = [], 0
    for start_idx, start_s in enumerate(spikes):
        stop_idx = max(stop_idx, start_idx)
        while stop_idx < spikes.size and spikes[stop_idx] <= start_s + min_duration_s:
            stop_idx += 1
        if stop_idx - start_idx >= min_spikes:
            intervals.append((float(start_s), float(spikes[stop_idx - 1])))
    return merge_intervals(intervals, merge_gap_s)


def merge_intervals(intervals, gap_s):
    if not intervals:
        return []
    merged = [tuple(intervals[0])]
    for a, b in sorted(intervals)[1:]:
        pa, pb = merged[-1]
        if a - pb <= gap_s:
            merged[-1] = (pa, max(pb, b))
        else:
            merged.append((a, b))
    return merged


def detect_burst_intervals(spike_lookup, *, bin_ms=1.0, smooth_ms=50.0,
                           threshold_z=6.0, min_duration_ms=30.0,
                           merge_gap_ms=30.0, min_spikes=5):
    arrays = [np.asarray(v, float) for v in spike_lookup.values() if len(v)]
    if not arrays:
        return [], {}
    spikes = np.sort(np.concatenate(arrays))
    if spikes.size < 3:
        return [], {}
    start, stop = float(spikes.min()), float(spikes.max())
    bin_s = max(0.001, float(bin_ms) / 1000.0)
    edges = np.arange(start, stop + bin_s, bin_s)
    if edges.size < 3:
        return [], {}
    counts, _ = np.histogram(spikes, bins=edges)
    smooth_bins = max(1, int(round(float(smooth_ms) / float(bin_ms))))
    rate = np.convolve(counts.astype(float), np.ones(smooth_bins) / smooth_bins, mode="same") if smooth_bins > 1 else counts.astype(float)
    quiet_cutoff = float(np.percentile(rate, 70))
    quiet = rate[rate <= quiet_cutoff]
    if quiet.size < max(3, min(10, rate.size)):
        quiet = rate
    baseline = float(np.median(quiet))
    mad = float(np.median(np.abs(quiet - baseline)))
    spread = max(1.4826 * mad, float(np.std(quiet)), float(np.sqrt(max(baseline, 0.0))) * 0.5)
    positive = rate[rate > 0]
    if spread > 1e-9:
        high_th = baseline + float(threshold_z) * spread
    elif positive.size:
        high_th = float(np.percentile(positive, min(99.0, max(50.0, 50.0 + threshold_z * 10.0))))
    else:
        return [], {}
    high_th = max(1.0, high_th)
    low_th = min(high_th, max(0.25, baseline + .35 * (high_th - baseline), high_th * .25))
    high_active, low_active = rate >= high_th, rate >= low_th
    min_bins = max(1, int(np.ceil(min_duration_ms / bin_ms)))
    intervals, i = [], 0
    while i < high_active.size:
        if not high_active[i]:
            i += 1; continue
        left = i
        while left > 0 and low_active[left - 1]: left -= 1
        right = i + 1
        while right < low_active.size and low_active[right]: right += 1
        i = right
        if right - left >= min_bins:
            a, b = float(edges[left]), float(edges[min(right, edges.size - 1)])
            if np.count_nonzero((spikes >= a) & (spikes <= b)) >= int(min_spikes):
                intervals.append((a, b))
    if not intervals:
        intervals = _density_fallback(spikes, max(bin_s, min_duration_ms / 1000), int(min_spikes), max(0, merge_gap_ms / 1000))
    intervals = merge_intervals(intervals, max(0, merge_gap_ms / 1000))
    diagnostics = {"edges_sec": edges, "counts": counts, "smoothed_counts": rate,
                   "baseline": baseline, "high_threshold": high_th, "low_threshold": low_th}
    return intervals, diagnostics
