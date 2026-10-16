"""This experiment's optional A/B/C/D Probe and stimulation-free pool check."""
from __future__ import annotations

import logging

from analysis.utils.probe import evaluate_probe
from .figures import raster
from .maxwell_setup import ElectrodeUnroutable, StimPoolConflict
from .schedule import Condition
from .utils.persistence import atomic_json, write_csv


def check_probe_pools(runner):
    reports = []
    runner.state("CHECK_PROBE_POOLS_NO_STIMULATION")
    for index, unit in enumerate(runner.units, 1):
        report = dict(unit_id=unit["unit_id"], center=unit["center"],
                      electrodes=unit["electrodes"], stimulation_sent=False)
        try:
            pool = runner.backend.route(unit["electrodes"])
        except (ElectrodeUnroutable, StimPoolConflict) as exc:
            report.update(can_interleave=False, status="routing_unavailable", error=str(exc), pool={})
            logging.warning("交替Probe路由检查 %s 不通过: %s", unit["unit_id"], exc)
        else:
            report.update(can_interleave=True, status="passed", error="", pool=pool)
        reports.append(report)
        runner.backend.disconnect_active() if runner.backend.realtime else runner.backend.switch(())
        runner.reporter.emit("交替Probe路由检查（不刺激）", index, len(runner.units), **report)
        pre = runner.run_dir / "pre_experiment"
        atomic_json(pre / "probe_routing_summary.json", reports)
        write_csv(pre / "probe_routing_summary.csv", reports)
    return reports


def not_tested(unit, reason):
    return [dict(unit_id=unit["unit_id"], electrode=e, center=unit["center"],
                 stimulation_unit=None, routing_mode="unit_pool_fast", h5=None, attempt=0,
                 status="not_tested", route_error=reason, all_trials=0, success_trials=0,
                 success_fraction=0.0, eligible=False, artifact_0_3ms_mean=None,
                 artifact_0_3ms_total=None, artifact_0_2ms_mean=None,
                 response_10_50ms_mean=None, response_10_50ms_total=None)
            for e in unit["electrodes"]]


def run_interleaved_probe(runner):
    cfg = runner.stimulation["probe"]
    pre = runner.run_dir / "pre_experiment"
    rounds = int(cfg["trials_per_electrode"])
    reports = check_probe_pools(runner)
    available = sum(r["can_interleave"] for r in reports)
    summaries = []
    runner.global_total = available * 4 * rounds
    if available < cfg["formal_unit_count"]:
        # Do not stimulate a partial batch that cannot meet the unit-count requirement.
        reason = f"仅{available}个unit支持四位点独立交替；先人工排查路由，不发送Probe"
        logging.warning(reason)
        runner.global_total = 0
        for unit, report in zip(runner.units, reports):
            summaries.extend(not_tested(unit, report["error"] or reason))
        write_csv(pre / "probe_summary.csv", summaries)
        return summaries
    runner.state("RUN_INTERLEAVED_PROBE", global_frequency_hz=cfg["frequency_hz"],
                 per_electrode_interval_sec=4 / cfg["frequency_hz"])
    for unit, report in zip(runner.units, reports):
        if not report["can_interleave"]:
            summaries.extend(not_tested(unit, report["error"]))
            write_csv(pre / "probe_summary.csv", summaries)
            continue
        # Confirm the pool again before opening this unit's recording.
        try:
            pool = runner.backend.route(unit["electrodes"])
        except (ElectrodeUnroutable, StimPoolConflict) as exc:
            summaries.extend(not_tested(unit, str(exc)))
            runner.global_total -= 4 * rounds
            write_csv(pre / "probe_summary.csv", summaries)
            continue
        directory = pre / "02_probe" / unit["unit_id"] / "interleaved_attempt_01"
        runner.start_segment(directory, unit["unit_id"] + "_interleaved", "02_stim", 4 * rounds)
        runner.wait_probe_baseline()
        atomic_json(directory / "routing.json", dict(mode="unit_pool_fast", pool=pool,
            order=unit["electrodes"], global_frequency_hz=cfg["frequency_hz"],
            per_electrode_interval_sec=4 / cfg["frequency_hz"]))
        deadline = None
        count = 0
        for round_index in range(1, rounds + 1):
            for electrode in unit["electrodes"]:
                count += 1
                condition = Condition(f"{unit['unit_id']}_ele{electrode}", "probe", (electrode,), (str(electrode),))
                deadline = runner.one_trial(condition, round_index, 1 / cfg["frequency_hz"],
                    count, 4 * rounds, "unit内交替Probe", deadline)
        tail = float(cfg["recording_tail_sec"])
        runner.reporter.wait(tail, "Probe尾部录制（保持采集开启）", realtime=runner.backend.realtime)
        if not runner.backend.realtime:
            runner.backend.advance(tail)
        session, manifest = runner.finish_segment(4 * rounds)
        raster(session, [r["onsets_sec"][0] for r in manifest], directory / "raster")
        all_metrics = []
        for electrode in unit["electrodes"]:
            rows = [r for r in manifest if r["electrodes"] == [electrode]]
            times = [r["onsets_sec"][0] for r in rows]
            trials, summary = evaluate_probe(session, times, runner.background, cfg)
            metrics_dir = directory / f"ele_{electrode}_metrics"
            metrics_path = metrics_dir / "trial_metrics.csv"
            summary.update(unit_id=unit["unit_id"], electrode=electrode, center=unit["center"],
                stimulation_unit=pool[electrode], routing_mode="unit_pool_fast", h5=str(session.path),
                trial_metrics_path=str(metrics_path), attempt=1, status="complete", recording_tail_sec=tail,
                global_frequency_hz=cfg["frequency_hz"], per_electrode_interval_sec=4 / cfg["frequency_hz"])
            summary["eligible"] = bool(summary["eligible"] and len(rows) == rounds)
            write_csv(metrics_path, trials)
            atomic_json(metrics_dir / "probe_metrics.json", summary)
            for trial, source in zip(trials, rows):
                all_metrics.append(dict(trial, electrode=electrode, round=source["round"],
                                        global_trial_id=source["trial_id"]))
            summaries.append(summary)
            write_csv(pre / "probe_summary.csv", summaries)
            runner.reporter.emit("Probe位点评估", 1, 1, **summary)
        write_csv(directory / "trial_metrics.csv", sorted(all_metrics, key=lambda r: r["stim_time_sec"]))
        runner.backend.disconnect_active() if runner.backend.realtime else runner.backend.switch(())
        runner.reporter.wait(cfg["inter_electrode_pause_sec"], "Probe unit间等待", realtime=runner.backend.realtime)
    return summaries
