from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from datetime import datetime
import copy
import logging
import shutil
import time
import yaml
import numpy as np

from analysis.utils.io import read_session, read_burst_table
from analysis.utils.detection.burst import detect_burst_intervals
from analysis.utils.probe import baseline_distribution, evaluate_probe, rank_units
from .site_selection import build_local_units
from .schedule import Condition, formal_conditions, round_robin, waveform_commands, next_onset_interval, pulse_duration_sec
from .utils.persistence import atomic_json, write_csv, sha256, utc_now
from .utils.time_log import ExternalTimeLog, SegmentStimLog
from .utils.progress import ExperimentStopped
from .figures import unit_map, raster, summary_table


class ExperimentRunner:
    def __init__(self, system, stimulation, run_dir, backend, reporter, probe_only=False):
        self.system, self.stimulation = copy.deepcopy(system), copy.deepcopy(stimulation)
        self.probe_only = probe_only
        self.system["experiment"]["execution_scope"] = "probe_only" if probe_only else "full"
        self.run_dir, self.backend, self.reporter = Path(run_dir), backend, reporter
        self.current_log = None
        self.completed_segments = []
        self.state_file = self.run_dir / "pre_experiment" / "workflow_state.json"
        self.event_id = 1
        self.trial_id = 1
        self.completed_trials = 0
        self.audit = ExternalTimeLog(self.run_dir, time.time())
        self.audit.add("experiment_start")

    def state(self, stage, **details):
        state = dict(stage=stage, updated_utc=utc_now(), run_dir=str(self.run_dir),
                    execution_scope="probe_only" if self.probe_only else "full",
                    completed_segments=self.completed_segments, completed_trials=self.completed_trials,
                    selected_formal_units=getattr(self, "formal_units", []))
        state.update(details)
        atomic_json(self.state_file, state)
        display = dict(details)
        if "phase" in display:
            display["record_phase"] = display.pop("phase")
        self.reporter.emit(stage, **display)

    def snapshot(self):
        directory = self.run_dir / "config_snapshot"
        directory.mkdir(exist_ok=True)
        for name, cfg in (("system", self.system), ("stimulation", self.stimulation)):
            (directory / f"{name}.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        pre = self.run_dir / "pre_experiment"
        pre.mkdir(exist_ok=True)
        (pre / "parameters.yaml").write_text(yaml.safe_dump(dict(system=self.system, stimulation=self.stimulation),
                                                               allow_unicode=True, sort_keys=False), encoding="utf-8")

    def initialize(self):
        cfg = self.run_dir / (datetime.now().strftime("%Hh%Mm%Ss") + ".cfg")
        source = self.system["electrode_map"]["cfg_path"]
        if self.backend.realtime:
            shutil.copy2(source, cfg)
        self.backend.initialize(cfg)
        self.cfg = cfg
        if not self.backend.realtime:
            self.system["manual_inputs"]["candidate_centers"] = self.backend.centers
            if self.stimulation["probe"]["threshold_rule"] is None:
                self.stimulation["probe"]["threshold_rule"] = "mean_plus_3sd"
                logging.warning("模拟模式暂用mean_plus_3sd，仅用于运行软件；真实配置仍需明确阈值公式")
        centers = self.system["manual_inputs"]["candidate_centers"]
        manual = self.system["manual_inputs"]
        self.units = build_local_units(self.backend.mapping, centers,
                                       manual["minimum_center_distance_pitches"] * manual["electrode_pitch_um"])
        network_review = manual.get("network_review_record")
        if network_review:
            shutil.copy2(network_review, self.run_dir / "pre_experiment" / Path(network_review).name)
        write_csv(self.run_dir / "pre_experiment" / "candidate_sites.csv", [dict(
            unit_id=u["unit_id"], center=u["center"], electrodes=";".join(map(str, u["electrodes"])),
            source="manual_review") for u in self.units])
        unit_map(self.backend.mapping, self.units, self.run_dir / "pre_experiment" / "candidate_units")
        self.reporter.emit("候选unit已保存", 1, 1, mapping=self.backend.mapping, candidate_units=self.units)
        self.snapshot()
        atomic_json(self.run_dir / "source_inputs.json", dict(cfg=str(cfg), cfg_sha256=sha256(cfg),
                    supplied_baseline=manual.get("baseline_h5"), manual_centers=centers,
                    synthetic=not self.backend.realtime, analysis_pipeline_deferred=True))

    def start_segment(self, directory, block, phase, expected_trials=0):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        basename = f"{self.system['experiment']['recording_name_prefix']}_{block}_{phase}"
        origin = self.backend.start_segment(directory, basename)
        self.current_log = SegmentStimLog(directory, block, phase, basename, origin)
        self.audit.add("record_start", epoch_sec=origin, block=block, phase=phase, segment_name=basename)
        (directory / "phase_meta.yaml").write_text(yaml.safe_dump(dict(block=block, phase=phase,
                    expected_trials=expected_trials, basename=basename, origin_epoch=origin,
                    synthetic=not self.backend.realtime)), encoding="utf-8")
        self.state("RECORD_SEGMENT", block=block, phase=phase, directory=str(directory))
        return self.current_log

    def finish_segment(self, expected_trials=0):
        log = self.current_log
        path = self.backend.finish_segment()
        self.audit.add("record_end", block=log.block, phase=log.phase, segment_name=log.segment_name)
        session = read_session(path, self.cfg, self.system["maxwell"]["well"])
        logging.info("H5已关闭并读取: %s | 时长 %.3fs，映射通道%d，未映射spike%d",
                     path, session.duration_sec, len(session.mapping), session.unmapped_spike_count)
        if expected_trials or log.rows:
            rows = log.finalize(session, expected_trials)
        else:
            rows = []
        self.completed_segments.append(dict(path=str(path), phase=log.phase, closed=True, verified=True))
        self.current_log = None
        self.state("SEGMENT_CLOSED", h5=str(path))
        return session, rows

    def spontaneous(self, directory, block, phase, duration):
        self.backend.disconnect_active() if self.backend.realtime else self.backend.switch(())
        self.start_segment(directory, block, phase)
        self.reporter.wait(duration, "自发录制", realtime=self.backend.realtime, directory=str(directory))
        if not self.backend.realtime:
            self.backend.advance(duration)
        session, _ = self.finish_segment()
        # A sparse/early-stopped recording is not a completed baseline.
        tolerance = max(.1, 2 / session.sampling_hz)
        if session.duration_sec + tolerance < duration:
            raise ValueError(f"自发H5时长{session.duration_sec:.3f}s少于要求{duration}s")
        self.reporter.emit("自发文件已关闭", duration, duration, h5=str(session.path))
        return session

    def prepare_baseline(self):
        manual = self.system["manual_inputs"]
        baseline_path = manual.get("baseline_h5") if self.backend.realtime else None
        duration = manual["baseline_duration_sec"]
        if not self.backend.realtime and self.system["mock"].get("baseline_duration_override_sec"):
            duration = self.system["mock"]["baseline_duration_override_sec"]
        if baseline_path:
            self.state("LOAD_MANUAL_BASELINE", h5=str(baseline_path))
            session = read_session(baseline_path, self.cfg, self.system["maxwell"]["well"])
            if session.duration_sec < duration - .1:
                raise ValueError("导入的手动基线不足600s；请提供完整记录")
            atomic_json(self.run_dir / "pre_experiment" / "baseline_reference.json", dict(
                path=str(Path(baseline_path).resolve()), sha256=sha256(baseline_path), duration_sec=session.duration_sec))
        else:
            session = self.spontaneous(self.run_dir / "pre_experiment" / "01_spontaneous", "manual_baseline",
                                       "01_pre_spont", duration)
        self.state("ANALYZE_PROBE_BACKGROUND", h5=str(session.path))
        if manual.get("baseline_bursts_csv"):
            bursts = read_burst_table(manual["baseline_bursts_csv"])
        else:
            lookup = {int(c): session.times[session.channels == c] for c in np.unique(session.channels)}
            bursts, _ = detect_burst_intervals(lookup, **self.system["burst_detection"])
        background = baseline_distribution(session.times, session.duration_sec, bursts,
                                            self.stimulation["probe"]["baseline_window_ms"])
        self.background = {k: v for k, v in background.items() if k not in ("counts", "starts")}
        pre = self.run_dir / "pre_experiment"
        write_csv(pre / "baseline_bursts.csv", [dict(start_sec=a, end_sec=b) for a, b in bursts], ["start_sec", "end_sec"])
        write_csv(pre / "baseline_background_windows.csv", [dict(start_sec=float(a), count=int(c))
                  for a, c in zip(background["starts"], background["counts"])])
        atomic_json(pre / "baseline_statistics.json", dict(**self.background, burst_count=len(bursts),
                    threshold_rule=self.stimulation["probe"]["threshold_rule"], h5=str(session.path),
                    probe_window_ms=self.stimulation["probe"]["response_window_ms"]))
        logging.info("非burst背景: %s", self.background)

    def one_trial(self, condition, round_index, interval_sec, current, total, stage, deadline=None):
        self.reporter.check()
        protocol = self.stimulation["protocol"]
        commands = waveform_commands(condition, protocol, self.backend.lsb_mv, self.event_id)
        sequence = self.backend.prepare_sequence(condition, commands, self.event_id, protocol)
        self.backend.switch(condition.electrodes)
        if self.backend.realtime and deadline is not None:
            if time.monotonic() > deadline + .01:
                logging.warning("trial %d 准备耗时导致调度延迟 %.3fs；真实时间以H5事件为准", self.trial_id, time.monotonic() - deadline)
            self.reporter.wait_until(deadline)
        onset_monotonic = time.monotonic()
        epoch, mock_times = self.backend.stimulate(sequence)
        del sequence
        ids = [self.event_id, self.event_id + 1] if condition.kind == "temporal" else [self.event_id]
        estimate = epoch - self.current_log.origin + protocol["sequence_initial_delay_samples"] * 50e-6
        onsets = mock_times or ([estimate, estimate + condition.dt_ms / 1000] if condition.kind == "temporal" else [estimate])
        row = dict(trial_id=self.trial_id, stage=stage, round=round_index, condition=condition.name,
                   kind=condition.kind, electrodes=list(condition.electrodes), labels=list(condition.labels),
                   relation=condition.relation, dt_ms=condition.dt_ms, event_ids=ids,
                   send_epoch_sec=epoch, estimated_onsets_sec=onsets,
                   amplitude_mv=protocol["amplitude_mv"], pulse_width_us=protocol["pulse_width_us"],
                   polarity=protocol["polarity"], routing_mode=self.system["routing"]["mode"],
                   interval_sec=interval_sec, response_anchor="second" if condition.kind == "temporal" else "first")
        self.current_log.add_trial(row)
        self.audit.add("stim_send", epoch_sec=epoch, block=self.current_log.block, phase=self.current_log.phase,
                       segment_name=self.current_log.segment_name, trial_id=self.trial_id,
                       electrodes=";".join(map(str, condition.electrodes)), event_ids=";".join(map(str, ids)), note=condition.name)
        self.trial_id += 1
        self.event_id += len(ids)
        self.completed_trials += 1
        self.reporter.emit(stage, current, total, global_trial=self.completed_trials,
                           global_total=getattr(self, "global_total", None), round=round_index,
                           electrodes=list(condition.electrodes), dt_ms=condition.dt_ms,
                           amplitude_mv=protocol["amplitude_mv"], pulse_width_us=protocol["pulse_width_us"],
                           condition=condition.name, artifact_count="待文件关闭后按H5事件计算",
                           response_count="待文件关闭后按H5事件计算", h5=self.current_log.segment_name,
                           map_path=str(self.run_dir / "pre_experiment" / "candidate_units.png"))
        if not self.backend.realtime:
            self.backend.advance(interval_sec)
        return onset_monotonic + interval_sec

    def probes(self):
        pre = self.run_dir / "pre_experiment"
        cfg = self.stimulation["probe"]
        rounds = cfg["trials_per_electrode"]
        site_summaries = []
        self.global_total = len(self.units) * 4 * rounds
        if not self.probe_only:
            formal_rounds = self.stimulation["formal"]["trials_per_condition"]
            if not self.backend.realtime and self.system["mock"].get("trials_per_condition_override"):
                formal_rounds = self.system["mock"]["trials_per_condition_override"]
            self.global_total += (12 + 12 * len(self.stimulation["formal"]["dt_ms"])) * formal_rounds
        self.state("RUN_PROBE")
        for unit in self.units:
            pool = self.backend.route(unit["electrodes"])
            atomic_json(pre / "02_probe" / unit["unit_id"] / "routing.json", pool)
            for electrode in unit["electrodes"]:
                attempt = 0
                while True:
                    attempt += 1
                    condition = Condition(f"{unit['unit_id']}_ele{electrode}", "probe", (electrode,), (str(electrode),))
                    directory = pre / "02_probe" / unit["unit_id"] / f"ele_{electrode}_attempt_{attempt:02d}"
                    self.start_segment(directory, unit["unit_id"] + f"_ele{electrode}", "02_stim", rounds)
                    action = None
                    deadline = None
                    for trial in range(1, rounds + 1):
                        action = self.reporter.consume_probe_action()
                        if action:
                            break
                        deadline = self.one_trial(condition, trial, 1 / cfg["frequency_hz"], trial, rounds,
                                                  "单电极Probe", deadline)
                    actual_count = len(self.current_log.rows)
                    self.reporter.wait(.1, "Probe尾部录制", realtime=self.backend.realtime)
                    if not self.backend.realtime:
                        self.backend.advance(.1)
                    session, manifest = self.finish_segment(actual_count)
                    times = [r["onsets_sec"][0] for r in manifest]
                    raster(session, times, directory / "raster")
                    if actual_count:
                        trials, summary = evaluate_probe(session, times, self.background, cfg)
                    else:
                        trials, summary = [], dict(success_trials=0, all_trials=0, success_fraction=0.0,
                            eligible=False, artifact_0_3ms_mean=0.0, artifact_0_3ms_total=0,
                            artifact_0_2ms_mean=0.0, response_10_50ms_mean=0.0, response_10_50ms_total=0)
                    summary.update(unit_id=unit["unit_id"], electrode=electrode, center=unit["center"],
                                   h5=str(session.path), attempt=attempt, status=action or "complete")
                    summary["eligible"] = bool(summary["eligible"] and actual_count == rounds and not action)
                    write_csv(directory / "trial_metrics.csv", trials,
                              list(trials[0]) if trials else ["trial", "success"])
                    atomic_json(directory / "probe_metrics.json", summary)
                    self.reporter.emit("Probe位点评估", 1, 1, **summary)
                    if action == "retry":
                        continue
                    site_summaries.append(summary)
                    write_csv(pre / "probe_summary.csv", site_summaries)
                    break
                self.backend.disconnect_active() if self.backend.realtime else self.backend.switch(())
                self.reporter.wait(cfg["inter_electrode_pause_sec"], "Probe位点间等待", realtime=self.backend.realtime)
        summary_table(site_summaries, pre / "probe_summary")
        self.formal_units = rank_units(site_summaries, cfg["formal_unit_count"], cfg["formal_electrodes_per_unit"])
        (pre / "selected_formal_sites.yaml").write_text(yaml.safe_dump(self.formal_units, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.state("PROBE_PASSED", selected_formal_units=self.formal_units)
        return self.formal_units

    def formal(self):
        cfg = self.stimulation["formal"]
        stages = formal_conditions(self.formal_units, cfg["dt_ms"])
        rounds = cfg["trials_per_condition"]
        if not self.backend.realtime and self.system["mock"].get("trials_per_condition_override"):
            rounds = self.system["mock"]["trials_per_condition_override"]
        total = sum(len(cs) * rounds for _, cs in stages)
        self.global_total = self.completed_trials + total
        block = "spatiotemporal"
        directory = self.run_dir / "raw_data" / block
        directory.mkdir(parents=True, exist_ok=True)
        write_csv(directory / "condition_manifest.csv", [dict(stage=stage, **asdict(c), planned_trials=rounds)
                  for stage, cs in stages for c in cs])
        (directory / "block_meta.yaml").write_text(yaml.safe_dump(dict(block=block, units=self.formal_units,
                    stages=[s for s, _ in stages], rounds=rounds, formal_trials=total,
                    raw_recording=True, analysis_pipeline_deferred=True), allow_unicode=True), encoding="utf-8")
        self.backend.route([e for unit in self.formal_units for e in unit["electrodes"]])
        self.spontaneous(directory / "01_pre_spont", block, "01_pre_spont", cfg["pre_spontaneous_sec"])
        self.start_segment(directory / "02_stim", block, "02_stim", total)
        count = 0
        for stage, conditions in stages:
            deadline = None
            for round_index, condition in round_robin(conditions, rounds):
                interval = next_onset_interval(condition, cfg, pulse_duration_sec(self.stimulation["protocol"]))
                count += 1
                deadline = self.one_trial(condition, round_index, interval, count, total, stage, deadline)
            # Ensure the final pulse, especially dt=1s, and response window complete before disconnect.
            if self.backend.realtime:
                self.reporter.wait_until(deadline)
            self.backend.disconnect_active() if self.backend.realtime else self.backend.switch(())
            rest = cfg["rest_after_last_stage_sec"] if stage == stages[-1][0] else cfg["rest_between_stages_sec"]
            self.reporter.wait(rest, "阶段间休息并继续录制", realtime=self.backend.realtime, stage_completed=stage)
            if not self.backend.realtime:
                self.backend.advance(rest)
        self.reporter.wait(cfg["recording_tail_sec"], "刺激段尾部录制", realtime=self.backend.realtime)
        if not self.backend.realtime:
            self.backend.advance(cfg["recording_tail_sec"])
        self.finish_segment(total)
        self.spontaneous(directory / "03_post_spont", block, "03_post_spont", cfg["post_spontaneous_sec"])
        self.result = dict(status="complete", synthetic=not self.backend.realtime,
                    formal_trial_count=total, probe_trial_count=len(self.units) * 4 * self.stimulation["probe"]["trials_per_electrode"],
                    selected_formal_units=self.formal_units, analysis_pipeline_deferred=True,
                    completed_utc=utc_now(), raw_data=str(directory))

    def run(self):
        self.state("INITIALIZE")
        try:
            self.initialize()
            self.prepare_baseline()
            self.probes()
            if self.probe_only:
                self.result = dict(status="probe_complete", execution_scope="probe_only",
                    synthetic=not self.backend.realtime, formal_started=False, formal_trial_count=0,
                    probe_trial_count=self.completed_trials, selected_formal_units=self.formal_units,
                    analysis_pipeline_deferred=True, completed_utc=utc_now())
            else:
                self.formal()
        except BaseException as exc:
            logging.exception("实验未完成，停止刺激并保存状态")
            if self.current_log:
                self.current_log.save_incomplete(str(exc))
            # Atomic error writes must work even when the stop event is set.
            atomic_json(self.state_file, dict(stage="STOPPED" if isinstance(exc, (ExperimentStopped, KeyboardInterrupt)) else "FAILED",
                        last_completed_segments=self.completed_segments, last_status=self.reporter.last_status,
                        error=str(exc), updated_utc=utc_now(), automatic_stimulation_stopped=True))
            raise
        finally:
            try:
                self.backend.shutdown()
                if self.current_log:
                    # Save recoverable times for a stopped/failed segment after cleanup closed its H5.
                    log = self.current_log
                    files = list(log.phase_dir.glob(log.segment_name + "*.raw.h5"))
                    if len(files) == 1:
                        try:
                            session = read_session(files[0], self.cfg, self.system["maxwell"]["well"])
                            if log.rows:
                                log.finalize(session, len(log.rows), allow_estimated=True)
                            atomic_json(log.phase_dir / "incomplete_segment.json", dict(
                                status="stopped_or_failed", closed=True, verified=False,
                                completed_commands=len(log.rows), h5=str(files[0]), generated_utc=utc_now()))
                        except Exception:
                            logging.exception("异常段已保留；恢复刺激时间表失败")
            except Exception as exc:
                atomic_json(self.run_dir / "cleanup_failure.json", dict(error=str(exc), time=utc_now()))
                raise
            finally:
                self.audit.add("experiment_end")
                self.audit.close()
        self.state("PROBE_COMPLETE" if self.probe_only else "COMPLETE")
        atomic_json(self.run_dir / "experiment_result.json", self.result)
