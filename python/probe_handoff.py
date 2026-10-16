"""Validate a separately reviewed Probe run before starting formal acquisition."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import yaml

from .site_selection import build_local_units
from .utils.persistence import sha256
from analysis.utils.io import parse_cfg
from analysis.utils.probe import minimum_probe_successes


def load_probe_handoff(source, system, stimulation, hardware=False):
    source = Path(source).expanduser().resolve()
    result = json.loads((source / "experiment_result.json").read_text(encoding="utf-8"))
    if result.get("status") != "probe_complete" or result.get("formal_started") is not False:
        raise ValueError("正式实验需要已完成、尚未开始正式刺激的Probe结果目录")
    if hardware and result.get("synthetic") is not False:
        raise ValueError("禁止用模拟Probe结果启动真实正式实验")
    source_system = yaml.safe_load((source / "config_snapshot/system.yaml").read_text(encoding="utf-8"))
    source_stim = yaml.safe_load((source / "config_snapshot/stimulation.yaml").read_text(encoding="utf-8"))
    minimum = minimum_probe_successes(stimulation["probe"])
    if minimum_probe_successes(source_stim["probe"]) != minimum:
        raise ValueError("当前最少有效trial数与源Probe不一致")
    if str(source_system["culture"]["id"]) != str(system["culture"]["id"]):
        raise ValueError("当前培养物编号与Probe培养物不一致")
    if any(source_system["maxwell"][k] != system["maxwell"][k] for k in ("device", "well")):
        raise ValueError("当前设备类型或well与Probe不一致")
    if any(source_system["maxwell"][k] != system["maxwell"][k]
           for k in ("event_threshold", "amplifier_gain")):
        raise ValueError("当前spike检测阈值或gain与Probe不一致")
    if source_stim["probe"].get("order", "sequential") != stimulation["probe"].get("order", "sequential"):
        raise ValueError("当前Probe交替/逐位点顺序与源结果不一致")
    if source_stim["probe"].get("enforce_artifact_qc", False):
        raise ValueError("源Probe采用旧的伪迹硬门槛，请按最新响应定义重新评估后再进行正式实验")
    for key in ("threshold_rule", "response_window_ms", "baseline_window_ms", "trials_per_electrode",
                "frequency_hz", "success_fraction_threshold"):
        if source_stim["probe"][key] != stimulation["probe"][key]:
            raise ValueError(f"当前Probe定义与源结果不一致: {key}")
    if stimulation['probe']['threshold_rule']=='paired_pre_stim_ratio':
        for key,default in [('paired_baseline_window_ms',[-50.,-10.]),('response_to_baseline_ratio',1.5)]:
            if source_stim['probe'].get(key,default)!=stimulation['probe'].get(key,default):
                raise ValueError(f'当前Probe定义与源结果不一致: {key}')
    for key in ("amplitude_mv", "pulse_width_us", "inter_phase_interval_us", "polarity"):
        if source_stim["protocol"][key] != stimulation["protocol"][key]:
            raise ValueError(f"当前刺激波形与Probe不一致: {key}")
    inputs = json.loads((source / "source_inputs.json").read_text(encoding="utf-8"))
    cfg = Path(system["electrode_map"]["cfg_path"])
    if sha256(cfg) != inputs["cfg_sha256"]:
        raise ValueError("当前CFG与Probe使用的CFG不同，停止正式实验")
    mapping = parse_cfg(cfg)
    manual = system["manual_inputs"]
    original_units = build_local_units(mapping, source_system["manual_inputs"]["candidate_centers"],
        manual["minimum_center_distance_pitches"] * manual["electrode_pitch_um"],
        additional=bool(source_system["manual_inputs"].get("additional_probe")),
        ranks=source_system["manual_inputs"].get("candidate_ranks"))
    allowed = {u["unit_id"]: set(u["electrodes"]) for u in original_units}
    with (source / "pre_experiment/probe_summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    metrics = {}
    for row in rows:
        key = (row["unit_id"], int(row["electrode"]))
        if key in metrics:
            raise ValueError("Probe汇总存在重复位点")
        metrics[key] = row
    selected_file = source / "pre_experiment/selected_formal_sites.yaml"
    selected = yaml.safe_load(selected_file.read_text(encoding="utf-8"))
    if not isinstance(selected, list) or len(selected) != 3:
        raise ValueError("审阅后的正式选点必须为3个unit，每个2个单电极")
    units, used_units, used_sites = [], set(), set()
    for index, selection in enumerate(selected, 1):
        unit_id = selection["source_unit"]
        sites = [int(e) for e in selection["electrodes"]]
        if unit_id in used_units or unit_id not in allowed or len(sites) != 2 or len(set(sites)) != 2:
            raise ValueError("正式选点包含重复或未知unit，或每unit未选两个不同位点")
        valid_metrics = []
        for electrode in sites:
            if electrode not in allowed[unit_id] or electrode in used_sites:
                raise ValueError("正式电极不属于原Probe unit，或多个unit共享电极")
            row = metrics.get((unit_id, electrode))
            if (row is None or row["eligible"].lower() != "true" or row.get("status") != "complete"
                    or int(row["all_trials"]) != 10 or int(row["success_trials"]) < minimum):
                raise ValueError(f"{unit_id}的电极{electrode}不满足完整10次Probe且至少{minimum}次有效")
            if not minimum <= int(row["success_trials"]) <= 10 or not math.isclose(
                    float(row["success_fraction"]), int(row["success_trials"]) / 10):
                raise ValueError("Probe成功次数与响应概率不一致")
            valid_metrics.append(dict(electrode=electrode, success_fraction=float(row["success_fraction"]),
                                      response_10_50ms_mean=float(row["response_10_50ms_mean"])))
            used_sites.add(electrode)
        used_units.add(unit_id)
        units.append(dict(source_unit=unit_id, formal_unit=index, electrodes=sites, site_metrics=valid_metrics))
    reference = dict(source_probe_run=str(source), source_probe_trial_count=result["probe_trial_count"],
                     cfg_sha256=inputs["cfg_sha256"], synthetic=result["synthetic"],
                     selected_sites_sha256=sha256(selected_file),
                     probe_summary_sha256=sha256(source / "pre_experiment/probe_summary.csv"))
    return units, reference
