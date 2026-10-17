"""Prepare a fresh whole-roster Probe repeat; edit settings below, no SDK calls."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main import load_configuration, validate
from python.additional_probe import read_probe_roster, validate_repeat_probe
from python.utils.persistence import atomic_json, sha256

# Edit only these settings; the candidate IDs and original four-site units are read automatically.
SOURCE_RUN = ""  # Most recent completed Probe in this culture/CFG lineage.
OUTPUT_DIR = ""  # Empty creates a new timestamped directory under config/.
CULTURE_DIV = None  # Set the actual DIV for the new acquisition, or keep the source value.
MIN_SUCCESS_TRIALS = 6


def prepare_repeat(source_run, output=None, minimum=6, culture_div=None):
    source = Path(source_run).expanduser().resolve()
    system, stimulation = load_configuration(source / "config_snapshot")
    inputs = json.loads((source / "source_inputs.json").read_text(encoding="utf-8"))
    cfg = Path(inputs["cfg"]).resolve()
    if sha256(cfg) != inputs["cfg_sha256"]:
        raise ValueError("原Probe归档CFG哈希不一致")
    roster = read_probe_roster(source, inputs["cfg_sha256"])
    system["electrode_map"]["cfg_path"] = str(cfg)
    system["data"]["root"] = str(ROOT / "data")
    system["experiment"]["date"] = datetime.now().date().isoformat()
    if culture_div is not None:
        if isinstance(culture_div, bool) or not isinstance(culture_div, int) or culture_div < 0:
            raise ValueError("DIV必须为非负整数")
        system["culture"]["div"] = culture_div
    manual = system["manual_inputs"]
    manual.update(repeat_probe=True, repeat_probe_from=str(source), additional_probe=False,
                  additional_probe_from=None, candidate_centers=[u["center"] for u in roster],
                  candidate_ranks=[u["rank"] for u in roster], network_review_record=None)
    probe, protocol = stimulation["probe"], stimulation["protocol"]
    probe.update(min_success_trials=minimum, enforce_artifact_qc=False)
    if (probe.get("order", "sequential") != "sequential" or probe["trials_per_electrode"] != 10
            or probe["frequency_hz"] != 2 or probe["threshold_rule"] != "paired_pre_stim_ratio"
            or probe.get("response_to_baseline_ratio", 1.5) != 1.5
            or protocol["amplitude_mv"] != 400 or protocol["pulse_width_us"] != 300
            or protocol["polarity"] != "negative_first"):
        raise ValueError("来源不符合本轮逐位点10次/2Hz/400mV/300us/配对1.5倍协议")
    units = validate_repeat_probe(system, stimulation)
    validate(system, stimulation, probe_only=True)
    destination = (ROOT / "config" / (datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_probe_repeat")
                   if output is None else Path(output)).resolve()
    if not destination.is_relative_to((ROOT / "config").resolve()) or destination == (ROOT / "config").resolve():
        raise ValueError("新配置必须放在本项目config的独立子目录")
    destination.mkdir(parents=True, exist_ok=False)
    plan_file = destination / "repeat_probe_plan.json"
    manual["network_review_record"] = str(plan_file)
    for name, value in (("system", system), ("stimulation", stimulation)):
        (destination / f"{name}.yaml").write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    atomic_json(plan_file, dict(source_probe_run=str(source), cfg_sha256=inputs["cfg_sha256"],
        culture=system["culture"], units=units, expected_probe_trial_count=len(units)*4*10,
        min_success_trials=minimum, repeat_all_candidates=True, stimulation_sent=False,
        source_files_unchanged=True, formal_started=False))
    return destination


def main():
    if not SOURCE_RUN:
        raise ValueError("请先在本文件顶部填写SOURCE_RUN")
    source = Path(SOURCE_RUN).expanduser()
    source = source if source.is_absolute() else ROOT / source
    output = Path(OUTPUT_DIR).expanduser() if OUTPUT_DIR else None
    if output is not None and not output.is_absolute():
        output = ROOT / output
    config = prepare_repeat(source, output, MIN_SUCCESS_TRIALS, CULTURE_DIV)
    plan = json.loads((config / "repeat_probe_plan.json").read_text(encoding="utf-8"))
    for u in plan["units"]:
        print(f"{u['unit_id']} 中心={u['center']} 四电极={u['electrodes']}")
    print(f"培养物={plan['culture']}；{len(plan['units'])}个unit；计划{plan['expected_probe_trial_count']}次；至少{MIN_SUCCESS_TRIALS}/10有效")
    print(f"新配置：{config}；原记录保留，未连接设备、未发送刺激")
    print(f'预演：python -B main.py --config-dir "{config}" --headless --probe-only --dry-run')


if __name__ == "__main__":
    main()
