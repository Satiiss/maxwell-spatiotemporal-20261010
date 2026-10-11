"""Read-only, stimulus-aligned Probe spike figures; no hardware or scoring."""
from __future__ import annotations

import numpy as np
import matplotlib.pyplot as plt

from .figures import configure, save


def align_saved_events(session, trials):
    times = np.asarray([float(row['stim_time_sec']) for row in trials])
    if not times.size or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError('Probe刺激时刻为空、非有限或不递增')
    events = []
    for time in times:
        matches = [event for event in session.events
                   if abs(event['time_sec'] - time) <= .5 / session.sampling_hz]
        if len(matches) != 1:
            raise ValueError(f'刺激时刻{time:.9f}s对应{len(matches)}个H5事件，无法确认时间原点')
        events.append(matches[0])
    actual = np.asarray([event['time_sec'] for event in events])
    if np.any(actual < 0) or np.any(actual >= session.duration_sec):
        raise ValueError('H5刺激事件位于录制范围之外')
    return actual, events


def local_spikes(session, onset, window_ms):
    lo, hi = onset + np.asarray(window_ms) / 1000
    lo, hi = max(0., lo), min(session.duration_sec, hi)
    left, right = np.searchsorted(session.times, [lo, hi], side='left')
    return (session.times[left:right] - onset) * 1000, session.channels[left:right]


def pooled_psth(session, times, window_ms, bin_ms=1.):
    start, end = window_ms
    bins = round((end - start) / bin_ms)
    if bin_ms <= 0 or bins < 1 or not np.isclose(bins * bin_ms, end - start):
        raise ValueError('绘图范围必须为正数bin宽度的整数倍')
    edges = np.linspace(start, end, bins + 1)
    counts, exposure = np.zeros(bins, dtype=np.int64), np.zeros(bins, dtype=np.int64)
    for onset in times:
        absolute = onset + edges / 1000
        covered = (absolute[:-1] >= -1e-12) & (absolute[1:] <= session.duration_sec + 1e-12)
        indices = np.searchsorted(session.times, absolute, side='left')
        counts += np.where(covered, np.diff(indices), 0)
        exposure += covered
    rate = np.divide(counts, exposure * bin_ms / 1000,
                     out=np.full(bins, np.nan), where=exposure > 0)
    return edges, counts, exposure, rate


def decorate(ax, window_ms):
    start, end = window_ms
    ax.axvline(0, color='#F26B59', lw=.6, label='Stimulus')
    for lo, hi, color, label in [(0, 3, '#F8B400', '0–3 ms'),
                                (10, 50, '#A4C8E1', '10–50 ms')]:
        if max(start, lo) < min(end, hi):
            ax.axvspan(max(start, lo), min(end, hi), color=color, alpha=.18,
                       lw=0, label=label)
    ax.set(xlim=(start, end), xlabel='Time from stimulus (ms)')
    ax.grid(False)
    ax.legend(loc='upper center', bbox_to_anchor=(.5, 1.15), ncol=3,
              frameon=False, fontsize=5, handlelength=1.2)


def mark_unrecorded(ax, session, onset, window_ms, trial_id=None):
    start, end = window_ms
    actual_start, actual_end = -onset * 1000, (session.duration_sec - onset) * 1000
    for lo, hi in [(start, min(end, actual_start)), (max(start, actual_end), end)]:
        if hi > lo:
            if trial_id is None:
                ax.axvspan(lo, hi, color='#D8DEE9', alpha=.8, hatch='///', lw=0)
            else:
                ax.fill_between([lo, hi], trial_id-.4, trial_id+.4,
                                color='#D8DEE9', alpha=.8, hatch='///', lw=0)


def plot_site(session, times, trial_ids, directory, window_ms, progress):
    configure()
    windows = [tuple(window_ms), (-5., 10.)]
    for window, name in zip(windows, ['aligned_trials', 'artifact_zoom']):
        fig, ax = plt.subplots(figsize=(3.5, 2.3))
        for onset, trial_id in zip(times, trial_ids):
            x, _ = local_spikes(session, onset, window)
            ax.plot(x, np.full(len(x), trial_id), linestyle='None', marker='|',
                    markersize=2, markeredgewidth=.3, color='#303C43')
            mark_unrecorded(ax, session, onset, window, trial_id)
        decorate(ax, window)
        ax.set(ylabel='Trial', ylim=(max(trial_ids) + .6, min(trial_ids) - .6))
        ax.set_yticks(trial_ids)
        save(fig, directory / name)
        progress(name)
    channel_ids = [r['channel'] for r in session.mapping]
    for onset, trial_id in zip(times, trial_ids):
        fig, ax = plt.subplots(figsize=(3.5, 2.8))
        x, channels = local_spikes(session, onset, window_ms)
        ax.plot(x, channels, linestyle='None', marker='|', markersize=1.5,
                markeredgewidth=.25, color='#303C43')
        decorate(ax, window_ms)
        mark_unrecorded(ax, session, onset, window_ms)
        ax.set(ylabel='Recording channel', ylim=(min(channel_ids) - .5, max(channel_ids) + .5))
        save(fig, directory / f'trial_{trial_id:02d}_raster')
        progress(f'trial_{trial_id:02d}_raster')
    edges, counts, exposure, rate = pooled_psth(session, times, window_ms)
    for name, omit in [('psth', False), ('psth_after_3ms', True)]:
        plotted = rate.copy()
        if omit:
            plotted[(edges[:-1] < 3) & (edges[1:] > 0)] = np.nan
        fig, ax = plt.subplots(figsize=(3.5, 2.3))
        ax.stairs(plotted, edges, color='#303C43', lw=.6)
        decorate(ax, window_ms)
        ax.set(ylabel='Population rate (spikes/s)')
        if omit:
            ax.text(.02, .96, '0–3 ms omitted', transform=ax.transAxes,
                    va='top', fontsize=5, color='#303C43')
        save(fig, directory / name)
        progress(name)
    return [dict(start_ms=float(lo), end_ms=float(hi), spike_count=int(count),
                 observed_trials=int(n), population_rate_hz=None if not n else float(r))
            for lo, hi, count, n, r in zip(edges[:-1], edges[1:], counts, exposure, rate)]
