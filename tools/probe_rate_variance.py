"""Offline literal rate-variance comparison; never changes acquisition or official Probe results."""
from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import yaml

from python.utils.persistence import atomic_json, sha256, utc_now, write_csv
from tools.inspect_probe_results import read_trials


def rate_statistics(counts, width_sec):
    counts = np.asarray(counts, dtype=float)
    if (counts.ndim != 1 or counts.size < 20 or not np.isfinite(counts).all()
            or np.any(counts < 0) or np.any(counts != np.floor(counts))
            or not np.isfinite(width_sec) or width_sec <= 0):
        raise ValueError("需要至少20个有效背景窗口计数和正窗口宽度")
    rates = counts / width_sec
    mean, variance = float(rates.mean()), float(rates.var(ddof=1))
    threshold = mean + 3 * variance
    return dict(window_sec=width_sec, windows=int(counts.size),
                aggregation="sum_over_all_recording_electrodes", mean_rate_hz=mean,
                variance_rate_hz2=variance, sd_rate_hz=float(rates.std(ddof=1)),
                response_threshold_hz=threshold, equivalent_count_threshold=threshold * width_sec,
                threshold_rule="literal_mean_plus_3variance_rate_hz",
                interpretation="按请求数值相加Hz均值与Hz平方方差；不是量纲一致的3sigma判据")


def review_rate_variance(run, output=None):
    started = utc_now()
    run = Path(run).resolve()
    pre = run / "pre_experiment"
    source_files = [pre / "baseline_statistics.json", pre / "baseline_background_windows.csv",
                    pre / "probe_summary.csv", run / "config_snapshot/stimulation.yaml"]
    hashes = {str(p): sha256(p) for p in source_files}
    stats = json.loads(source_files[0].read_text(encoding="utf-8"))
    with source_files[1].open(encoding="utf-8-sig", newline="") as f:
        counts = np.array([float(r["count"]) for r in csv.DictReader(f)])
    with source_files[2].open(encoding="utf-8-sig", newline="") as f:
        sources = list(csv.DictReader(f))
    stimulation = yaml.safe_load(source_files[3].read_text(encoding="utf-8"))
    probe = stimulation["probe"]
    width = float(stats["window_ms"]) / 1000
    lo, hi = map(float, probe["response_window_ms"])
    if not np.isclose(hi - lo, stats["window_ms"]):
        raise ValueError("响应窗口与背景窗口宽度不同，不能直接比较")
    background = rate_statistics(counts, width)
    if (len(counts) != int(stats["windows"]) or
            not np.isclose(counts.mean(), stats["mean"]) or
            not np.isclose(counts.var(ddof=1), stats["variance"])):
        raise ValueError("保存的背景窗口与基线统计不一致，停止离线比较")
    trials, summaries, unit_sites = [], [], {}
    expected = int(probe["trials_per_electrode"])
    fraction_threshold = float(probe["success_fraction_threshold"])
    for row in sources:
        unit, electrode = row["unit_id"], int(row["electrode"])
        unit_sites.setdefault(unit, [])
        summary = dict(unit_id=unit, electrode=electrode, source_status=row.get("status", ""),
                       original_success_trials=int(row["success_trials"]), all_trials=0,
                       success_trials_rate=0, success_fraction_rate=0.0, eligible_rate=False,
                       response_rate_mean_hz=None, response_rate_max_hz=None,
                       response_threshold_hz=background["response_threshold_hz"])
        if row.get("status") == "complete" and row.get("h5"):
            saved = read_trials(row)
            metrics_path = Path(row.get("trial_metrics_path") or str(Path(row["h5"]).parent / "trial_metrics.csv"))
            hashes[str(metrics_path)] = sha256(metrics_path)
            if len(saved) != int(row["all_trials"]) or not saved:
                raise ValueError(f"电极{electrode}的已保存trial数量与汇总不一致")
            response = np.array([float(t["response_10_50ms"]) for t in saved])
            if not np.isfinite(response).all() or np.any(response < 0) or np.any(response != np.floor(response)):
                raise ValueError(f"电极{electrode}的响应计数无效")
            if not np.isclose(response.mean(), float(row["response_10_50ms_mean"])):
                raise ValueError(f"电极{electrode}的逐次计数与保存均值不一致")
            rates = response / width
            success = rates > background["response_threshold_hz"]
            successful = int(success.sum())
            fraction = successful / len(saved)
            eligible = len(saved) == expected and fraction > fraction_threshold
            summary.update(all_trials=len(saved), success_trials_rate=successful,
                           success_fraction_rate=float(fraction), eligible_rate=bool(eligible),
                           response_rate_mean_hz=float(rates.mean()), response_rate_max_hz=float(rates.max()))
            if eligible:
                unit_sites[unit].append(electrode)
            for t, count, rate, passed in zip(saved, response, rates, success):
                trials.append(dict(unit_id=unit, electrode=electrode, trial=int(t["trial"]),
                                   stim_time_sec=float(t["stim_time_sec"]), response_count=int(count),
                                   response_rate_hz=float(rate), response_threshold_hz=background["response_threshold_hz"],
                                   response_success_rate=bool(passed)))
        summaries.append(summary)
    unit_rows = [dict(unit_id=u, eligible_site_count=len(sites), electrodes=";".join(map(str, sites)))
                 for u, sites in unit_sites.items()]
    if any(sha256(p) != digest for p, digest in hashes.items()):
        raise ValueError("检查期间源文件发生变化，停止保存")
    output = Path(output) if output else run.parent / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_rate_variance_review")
    output.mkdir(parents=True, exist_ok=False)
    config = dict(source_run=str(run), purpose="offline_rate_variance_review", window_sec=width,
                  aggregation=background["aggregation"], threshold_rule=background["threshold_rule"],
                  expected_trials=expected, success_fraction_threshold=fraction_threshold,
                  original_probe_config=probe, source_file_sha256=hashes)
    (output / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    atomic_json(output / "baseline_rate_statistics.json", background)
    write_csv(output / "probe_rate_summary.csv", summaries)
    write_csv(output / "probe_rate_trial_metrics.csv", trials)
    write_csv(output / "unit_rate_review.csv", unit_rows)
    result = dict(status="offline_rate_variance_review", source_run=str(run), output_dir=str(output.resolve()),
                  started_utc=started, completed_utc=utc_now(), hardware_contacted=False, stimulation_sent=False,
                  source_files_unchanged=True, evaluated_trials=len(trials),
                  eligible_sites_rate=sum(r["eligible_rate"] for r in summaries),
                  units_with_at_least_two_eligible_sites=sum(len(sites) >= 2 for sites in unit_sites.values()))
    atomic_json(output / "analysis_result.json", result)
    print("仅离线发放率方差对照：沿用原非burst窗口和全部记录电极，不改变原Probe结果、不进行刺激")
    print("背景均值=%.4fHz；方差=%.4fHz²；标准差=%.4fHz" % (
        background["mean_rate_hz"], background["variance_rate_hz2"], background["sd_rate_hz"]))
    print("字面均值+3倍方差阈值=%.4f；40ms等效计数阈值=%.4f" % (
        background["response_threshold_hz"], background["equivalent_count_threshold"]))
    print("电极 原规则成功次数 发放率规则成功/总次数 发放率规则合格 状态")
    for row in summaries:
        print(row["electrode"], row["original_success_trials"],
              f"{row['success_trials_rate']}/{row['all_trials']}", row["eligible_rate"], row["source_status"])
    print("有效位点=%d；至少2个有效位点的unit=%d" % (
        result["eligible_sites_rate"], result["units_with_at_least_two_eligible_sites"]))
    print("此目录是独立离线对照，不能直接作为--formal-from输入。结果目录：", output.resolve())
    return result, background, summaries
