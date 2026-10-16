"""Prepare a separate additional-unit configuration; never import the device SDK."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import load_configuration, validate
from python.additional_probe import validate_additional_probe
from python.utils.persistence import atomic_json, sha256


def prepare(source_run, review, minimum=6, output=None):
    source = Path(source_run).resolve()
    review = Path(review).resolve()
    plan_file = review / "candidate_units.json" if review.is_dir() else review
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    system, stimulation = load_configuration(source / "config_snapshot")
    system, stimulation = copy.deepcopy(system), copy.deepcopy(stimulation)
    inputs = json.loads((source / "source_inputs.json").read_text(encoding="utf-8"))
    # Use the archived CFG, whose exact bytes were used in the source acquisition.
    cfg = Path(inputs["cfg"]).resolve()
    if sha256(cfg) != inputs["cfg_sha256"]:
        raise ValueError("原Probe归档CFG哈希不一致")
    system["electrode_map"]["cfg_path"] = str(cfg)
    manual = system["manual_inputs"]
    proposed = plan["units"]
    manual.update(additional_probe=True, additional_probe_from=str(source),
                  candidate_centers=[u["center"] for u in proposed],
                  candidate_ranks=[u["rank"] for u in proposed], network_review_record=str(plan_file))
    system["data"]["root"] = str(ROOT / "data")
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
    for name, content in (("system", system), ("stimulation", stimulation)):
        (destination / f"{name}.yaml").write_text(
            yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8")
    atomic_json(destination / "additional_probe_plan.json", dict(
        source_probe_run=str(source), review_file=str(plan_file), cfg_sha256=inputs["cfg_sha256"],
        culture=system["culture"], units=units, min_success_trials=minimum,
        expected_probe_trial_count=len(units) * 4 * 10, stimulation_sent=False,
        formal_started=False, source_files_unchanged=True))
    return destination


def main():
    parser = argparse.ArgumentParser(description="仅生成补测配置；不连接设备、不刺激")
    parser.add_argument("--source-run", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--min-success-trials", type=int, default=6)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = prepare(args.source_run, args.review, args.min_success_trials, args.output)
    plan = json.loads((output / "additional_probe_plan.json").read_text(encoding="utf-8"))
    for u in plan["units"]:
        print(f"{u['unit_id']} 中心={u['center']} 四电极={u['electrodes']}")
    print(f"培养物={plan['culture']}；计划{plan['expected_probe_trial_count']}次；完整10次中至少{plan['min_success_trials']}次有效")
    print(f"新配置：{output}")
    print(f'下一步仅预演：python -B main.py --config-dir "{output}" --headless --probe-only --dry-run')


if __name__ == "__main__":
    main()
