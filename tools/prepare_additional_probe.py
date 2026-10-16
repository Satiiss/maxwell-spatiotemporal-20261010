"""Prepare a separate additional-unit configuration; never import the device SDK."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import load_configuration, validate
from analysis.utils.io import parse_cfg
from python.additional_probe import read_probe_history, validate_additional_probe
from python.site_selection import build_local_units
from python.utils.persistence import atomic_json, sha256


def prepare(source_run, review=None, minimum=None, output=None, *, centers=None):
    if (review is None) == (centers is None):
        raise ValueError("必须且只能提供review或centers之一")
    source = Path(source_run).resolve()
    plan_file = None
    if review is not None:
        review = Path(review).resolve()
        plan_file = review / "candidate_units.json" if review.is_dir() else review
    system, stimulation = load_configuration(source / "config_snapshot")
    system, stimulation = copy.deepcopy(system), copy.deepcopy(stimulation)
    inputs = json.loads((source / "source_inputs.json").read_text(encoding="utf-8"))
    # Use the archived CFG, whose exact bytes were used in the source acquisition.
    cfg = Path(inputs["cfg"]).resolve()
    if sha256(cfg) != inputs["cfg_sha256"]:
        raise ValueError("原Probe归档CFG哈希不一致")
    system["electrode_map"]["cfg_path"] = str(cfg)
    manual = system["manual_inputs"]
    if plan_file is not None:
        proposed = json.loads(plan_file.read_text(encoding="utf-8"))["units"]
    else:
        centers = list(centers)
        _, used_ids = read_probe_history(source, inputs["cfg_sha256"])
        if not used_ids or any(re.fullmatch(r"candidate_[0-9]+", value) is None for value in used_ids):
            raise ValueError("无法识别原Probe候选编号，不能自动分配新编号")
        first = max(int(value.removeprefix("candidate_")) for value in used_ids) + 1
        proposed = build_local_units(parse_cfg(cfg), centers,
            manual["minimum_center_distance_pitches"] * manual["electrode_pitch_um"],
            additional=True, ranks=list(range(first, first + len(centers))))
    manual.update(additional_probe=True, additional_probe_from=str(source),
                  candidate_centers=[u["center"] for u in proposed],
                  candidate_ranks=[u["rank"] for u in proposed],
                  network_review_record=str(plan_file) if plan_file else None)
    system["data"]["root"] = str(ROOT / "data")
    if minimum is None:
        minimum = stimulation["probe"].get("min_success_trials")
        if minimum is None:
            minimum = 6
    stimulation["probe"].update(min_success_trials=minimum, enforce_artifact_qc=False)
    probe, protocol = stimulation["probe"], stimulation["protocol"]
    if (probe.get("order", "sequential") != "sequential" or probe["trials_per_electrode"] != 10
            or probe["frequency_hz"] != 2 or probe["threshold_rule"] != "paired_pre_stim_ratio"
            or probe.get("response_to_baseline_ratio", 1.5) != 1.5
            or protocol["amplitude_mv"] != 400 or protocol["pulse_width_us"] != 300
            or protocol["polarity"] != "negative_first"):
        raise ValueError("来源Probe不是已确认的逐位点10次/2Hz/400mV/300us/配对1.5倍协议")
    units = validate_additional_probe(system, stimulation)
    if any(u["unit_id"] != p["unit_id"] or u["electrodes"] != p["electrodes"]
           for u, p in zip(units, proposed)):
        raise ValueError("候选草稿与当前CFG最近邻重算结果不一致")
    validate(system, stimulation, probe_only=True)
    destination = (ROOT / "config" / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_additional_probe")
                   if output is None else Path(output)).resolve()
    if not destination.is_relative_to((ROOT / "config").resolve()) or destination == (ROOT / "config").resolve():
        raise ValueError("新配置必须放在本项目config的独立子目录")
    destination.mkdir(parents=True, exist_ok=False)
    plan_path = destination / "additional_probe_plan.json"
    if plan_file is None:
        manual["network_review_record"] = str(plan_path)
    for name, content in (("system", system), ("stimulation", stimulation)):
        (destination / f"{name}.yaml").write_text(
            yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8")
    atomic_json(plan_path, dict(
        source_probe_run=str(source), review_file=str(plan_file) if plan_file else None,
        selection_source="review_file" if plan_file else "manual_centers",
        cfg_sha256=inputs["cfg_sha256"],
        culture=system["culture"], units=units, min_success_trials=minimum,
        expected_probe_trial_count=len(units) * 4 * 10, stimulation_sent=False,
        formal_started=False, source_files_unchanged=True))
    return destination


def main():
    parser = argparse.ArgumentParser(description="仅生成补测配置；不连接设备、不刺激")
    parser.add_argument("--source-run", required=True, type=Path, help="同一培养物和CFG下最近一次已结束的实际Probe；不能填dry-run目录")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--review", type=Path, help="已有候选JSON或所在目录；保留其编号")
    selection.add_argument("--centers", nargs="+", type=int, help="仅填中心电极ID；自动匹配邻居并分配新unit编号")
    parser.add_argument("--min-success-trials", type=int, help="缺省继承来源的显式门槛；旧来源未设置时用6")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = prepare(args.source_run, args.review, args.min_success_trials, args.output, centers=args.centers)
    plan = json.loads((output / "additional_probe_plan.json").read_text(encoding="utf-8"))
    for u in plan["units"]:
        print(f"{u['unit_id']} 中心={u['center']} 四电极={u['electrodes']} 距离µm={u['distances_um'][1:]}")
    print(f"培养物={plan['culture']}；计划{plan['expected_probe_trial_count']}次；完整10次中至少{plan['min_success_trials']}次有效")
    print(f"新配置：{output}")
    print(f'下一步仅预演：python -B main.py --config-dir "{output}" --headless --probe-only --dry-run')


if __name__ == "__main__":
    main()
