"""Read-only preflight for additional units; reject reused electrodes before SDK setup."""
from pathlib import Path
import csv
import json
import yaml

from analysis.utils.io import parse_cfg
from .site_selection import build_local_units
from .utils.persistence import sha256


def validate_additional_probe(system, stimulation, hardware=False):
    manual = system["manual_inputs"]
    if not manual.get("additional_probe"):
        return None
    source_path = manual.get("additional_probe_from")
    if not source_path:
        raise ValueError("补测必须提供additional_probe_from原Probe run目录")
    source = Path(source_path).resolve()
    result = json.loads((source / "experiment_result.json").read_text(encoding="utf-8"))
    if result.get("status") not in ("probe_complete", "probe_review_required") or result.get("formal_started") is not False:
        raise ValueError("补测来源必须为已收尾、未进行正式刺激的Probe")
    if hardware and result.get("synthetic") is not False:
        raise ValueError("真实补测禁止引用模拟Probe")
    old = yaml.safe_load((source / "config_snapshot/system.yaml").read_text(encoding="utf-8"))
    old_stim = yaml.safe_load((source / "config_snapshot/stimulation.yaml").read_text(encoding="utf-8"))
    if str(old["culture"]["id"]) != str(system["culture"]["id"]):
        raise ValueError("补测培养物与原Probe不一致")
    if any(old["maxwell"][key] != system["maxwell"][key]
           for key in ("device", "well", "event_threshold", "amplifier_gain")):
        raise ValueError("补测设备/well/检测阈值/gain与原Probe不一致")
    before, current = old["manual_inputs"].get("baseline_h5"), manual.get("baseline_h5")
    if not before or not current or Path(before).resolve() != Path(current).resolve():
        raise ValueError("补测必须引用原Probe的同一基线H5")
    inputs = json.loads((source / "source_inputs.json").read_text(encoding="utf-8"))
    cfg = Path(system["electrode_map"]["cfg_path"])
    if sha256(cfg) != inputs["cfg_sha256"]:
        raise ValueError("补测CFG与原Probe不一致")
    if old_stim["protocol"] != stimulation["protocol"]:
        raise ValueError("补测波形与原Probe不一致")
    defaults = {"order": "sequential", "paired_baseline_window_ms": [-50., -10.],
                "response_to_baseline_ratio": 1.5}
    for key in ("threshold_rule", "response_window_ms", "trials_per_electrode", "frequency_hz", *defaults):
        if old_stim["probe"].get(key, defaults.get(key)) != stimulation["probe"].get(key, defaults.get(key)):
            raise ValueError(f"补测Probe定义与原Probe不一致：{key}")
    ranks = manual.get("candidate_ranks")
    if ranks is None:
        raise ValueError("补测必须显式填写candidate_ranks，保留原候选编号")
    units = build_local_units(parse_cfg(cfg), manual["candidate_centers"],
        manual["minimum_center_distance_pitches"] * manual["electrode_pitch_um"],
        additional=True, ranks=ranks)
    used_sites, used_ids = set(), set()
    visited = set()
    while True:
        if source in visited or len(visited) >= 20:
            raise ValueError("补测来源链循环或过长")
        visited.add(source)
        lineage = json.loads((source / "source_inputs.json").read_text(encoding="utf-8"))
        if lineage["cfg_sha256"] != inputs["cfg_sha256"]:
            raise ValueError("补测来源链CFG不一致")
        with (source / "pre_experiment/candidate_sites.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                used_ids.add(row["unit_id"])
                used_sites.update(int(e) for e in row["electrodes"].split(";") if e)
        saved = yaml.safe_load((source / "config_snapshot/system.yaml").read_text(encoding="utf-8"))
        previous = saved["manual_inputs"].get("additional_probe_from") if saved["manual_inputs"].get("additional_probe") else None
        if not previous:
            break
        source = Path(previous).resolve()
    overlap = used_sites.intersection(e for unit in units for e in unit["electrodes"])
    if overlap or used_ids.intersection(u["unit_id"] for u in units):
        raise ValueError(f"补测重复旧候选编号或电极：{sorted(overlap)}；拒绝重复刺激")
    return units
