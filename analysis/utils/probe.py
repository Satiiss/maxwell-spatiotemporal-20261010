"""Window counts, non-burst background and deterministic site ranking."""
from __future__ import annotations

import math
import numpy as np


def count_windows(times, starts, width_sec):
    times = np.sort(np.asarray(times, float))
    starts = np.asarray(starts, float)
    return np.searchsorted(times, starts + width_sec, side="left") - np.searchsorted(times, starts, side="left")


def baseline_distribution(times, duration_sec, bursts, window_ms=40):
    width = float(window_ms) / 1000
    if width <= 0 or duration_sec < width:
        raise ValueError("基线窗口或基线时长无效")
    starts = np.arange(0, duration_sec - width + 1e-9, width)
    mask = np.ones(starts.size, bool)
    for a, b in bursts:
        mask &= ~((starts < b) & (starts + width > a))
    starts = starts[mask]
    if len(starts) < 20:
        raise ValueError("去除burst后的背景窗口不足20个，不能判定probe")
    counts = count_windows(times, starts, width)
    return dict(window_ms=window_ms, windows=int(len(counts)), mean=float(counts.mean()),
                variance=float(counts.var(ddof=1)), sd=float(counts.std(ddof=1)),
                background_rate_hz=float(counts.mean() / width), counts=counts, starts=starts)


def response_threshold(baseline, rule):
    if rule == "mean_plus_3sd":
        return baseline["mean"] + 3 * baseline["sd"]
    if rule == "mean_plus_3variance":
        return baseline["mean"] + 3 * baseline["variance"]
    raise ValueError("请在probe.threshold_rule中明确三倍标准差或三倍方差；禁止隐式替换")


def validate_paired_config(cfg):
    before = np.asarray(cfg.get('paired_baseline_window_ms', [-50., -10.]), dtype=float)
    after = np.asarray(cfg['response_window_ms'], dtype=float)
    ratio = float(cfg.get('response_to_baseline_ratio', 1.5))
    if before.shape!=(2,) or after.shape!=(2,) or not np.array_equal(before,[-50.,-10.]) or not np.array_equal(after,[10.,50.]):
        raise ValueError('paired_pre_stim_ratio要求前窗[-50,-10]ms、后窗[10,50]ms')
    if not np.isfinite(ratio) or ratio<=0:
        raise ValueError('response_to_baseline_ratio必须为有限正数')


def minimum_probe_successes(cfg):
    """Explicit inclusive trial count, or the legacy strict-fraction rule."""
    trials = cfg.get("trials_per_electrode", 10)
    if isinstance(trials, bool) or not isinstance(trials, (int, np.integer)) or trials < 1:
        raise ValueError("trials_per_electrode必须为正整数")
    minimum = cfg.get("min_success_trials")
    if minimum is not None:
        if isinstance(minimum, bool) or not isinstance(minimum, (int, np.integer)) or not 1 <= minimum <= trials:
            raise ValueError("min_success_trials必须为1至总trial数之间的整数")
        return int(minimum)
    threshold = float(cfg["success_fraction_threshold"])
    if not math.isfinite(threshold) or not 0 <= threshold < 1:
        raise ValueError("success_fraction_threshold必须在[0,1)内")
    return math.floor(threshold * trials) + 1


def evaluate_probe(session, stim_times, baseline, cfg):
    minimum = minimum_probe_successes(cfg)
    stim_times = np.asarray(stim_times, float)
    lo, hi = map(float, cfg["response_window_ms"])
    paired = cfg['threshold_rule'] == 'paired_pre_stim_ratio'
    if not paired and not np.isclose(hi - lo, baseline["window_ms"]):
        raise ValueError("背景与响应窗口宽度不一致，必须重算同宽基线分布")
    if not len(stim_times) or stim_times.min() < 0 or stim_times.max() + hi / 1000 > session.duration_sec:
        raise ValueError("刺激窗口超出H5录制范围")
    artifacts = count_windows(session.times, stim_times, 0.003)
    generic_artifacts = count_windows(session.times, stim_times, 0.002)
    response = count_windows(session.times, stim_times + lo / 1000, (hi - lo) / 1000)
    if paired:
        validate_paired_config(cfg)
        before_lo, before_hi = map(float, cfg.get('paired_baseline_window_ms', [-50., -10.]))
        ratio = float(cfg.get('response_to_baseline_ratio', 1.5))
        if not np.isfinite([before_lo, before_hi, ratio]).all() or ratio <= 0 or not before_lo < before_hi <= 0:
            raise ValueError('刺激前窗口或响应倍数无效')
        if np.any(stim_times + before_lo / 1000 < 0):
            raise ValueError('刺激前背景窗口超出H5录制范围，不能将未录制部分视为零')
        before = count_windows(session.times, stim_times + before_lo / 1000, (before_hi-before_lo)/1000)
        thresholds = ratio * before * (hi-lo)/(before_hi-before_lo)
        threshold = None  # This rule has one threshold per trial, not one global threshold.
    else:
        threshold = response_threshold(baseline, cfg["threshold_rule"])
        thresholds = np.full(len(stim_times), threshold)
    success = response > thresholds
    artifact_valid = artifacts > int(cfg["artifact_min_spikes"])
    trials = [dict(trial=i + 1, stim_time_sec=float(t), artifact_0_3ms=int(a),
                   artifact_0_2ms=int(g), response_10_50ms=int(r), response_threshold=float(th),
                   response_success=bool(s), artifact_valid=bool(v),
                   success=bool(s and (v or not cfg["enforce_artifact_qc"])))
              for i, (t, a, g, r, th, s, v) in enumerate(zip(stim_times, artifacts, generic_artifacts, response, thresholds, success, artifact_valid))]
    if paired:
        for row, count in zip(trials, before):
            row.update(baseline_minus50_minus10ms=int(count),
                       baseline_rate_hz=float(count/((before_hi-before_lo)/1000)),
                       response_rate_hz=float(row['response_10_50ms']/((hi-lo)/1000)),
                       paired_baseline_window_ms=[before_lo,before_hi], response_to_baseline_ratio=ratio)
    successful = sum(row["success"] for row in trials)
    fraction = successful / len(trials)
    site_success = (len(trials) == cfg.get("trials_per_electrode", 10) and successful >= minimum
                    if cfg.get("min_success_trials") is not None
                    else fraction > cfg["success_fraction_threshold"])
    artifact_file_valid = float(artifacts.mean()) > cfg["artifact_min_spikes"]
    summary = dict(success_trials=successful, all_trials=len(trials), success_fraction=float(fraction),
                   eligible=bool(site_success and
                                 (artifact_file_valid or not cfg["enforce_artifact_qc"])),
                   min_success_trials=minimum,
                   artifact_0_3ms_mean=float(artifacts.mean()), artifact_0_3ms_total=int(artifacts.sum()),
                   artifact_0_2ms_mean=float(generic_artifacts.mean()),
                   generic_delivery_valid=bool(generic_artifacts.mean() >= 200),
                   response_10_50ms_mean=float(response.mean()), response_10_50ms_total=int(response.sum()),
                   artifact_file_valid=artifact_file_valid, response_threshold=threshold,
                   threshold_rule=cfg["threshold_rule"], response_window_ms=[lo, hi])
    if paired:
        summary.update(paired_baseline_window_ms=[before_lo,before_hi], response_to_baseline_ratio=ratio,
                       trial_specific_threshold=True, response_threshold_mean=float(thresholds.mean()),
                       baseline_minus50_minus10ms_mean=float(before.mean()))
    return trials, summary


def rank_units(site_summaries, unit_count=3, per_unit=2):
    groups = {}
    for row in site_summaries:
        groups.setdefault(row["unit_id"], []).append(row)
    candidates = []
    for unit_id, rows in groups.items():
        valid = sorted([r for r in rows if r["eligible"]],
                       key=lambda r: (-r["success_fraction"], -r["response_10_50ms_mean"], r["electrode"]))
        if len(valid) >= per_unit:
            candidates.append(dict(source_unit=unit_id, valid_electrode_count=len(valid),
                                   electrodes=[r["electrode"] for r in valid[:per_unit]],
                                   site_metrics=valid[:per_unit]))
    candidates.sort(key=lambda u: (-u["valid_electrode_count"],
                                  -sum(r["success_fraction"] for r in u["site_metrics"]), u["source_unit"]))
    if len(candidates) < unit_count:
        raise ValueError(f"仅有{len(candidates)}个合格unit，需要至少{unit_count}个且每个至少{per_unit}个可用单电极；停止正式刺激")
    for i, unit in enumerate(candidates[:unit_count], 1):
        unit["formal_unit"] = i
    return candidates[:unit_count]
