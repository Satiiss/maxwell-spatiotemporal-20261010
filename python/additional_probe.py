"""Read-only provenance checks for new units and explicit whole-roster Probe repeats."""
from pathlib import Path
import csv
import json
import re
import yaml

from analysis.utils.io import parse_cfg
from .site_selection import build_local_units
from .utils.persistence import sha256


def _history_runs(source, cfg_sha256):
    source = Path(source).resolve()
    visited = set()
    while True:
        if source in visited or len(visited) >= 20:
            raise ValueError("补测来源链循环或过长")
        visited.add(source)
        lineage = json.loads((source / "source_inputs.json").read_text(encoding="utf-8"))
        if lineage["cfg_sha256"] != cfg_sha256:
            raise ValueError("补测来源链CFG不一致")
        saved = yaml.safe_load((source / "config_snapshot/system.yaml").read_text(encoding="utf-8"))
        yield source, saved
        manual = saved["manual_inputs"]
        previous = (manual.get("repeat_probe_from") if manual.get("repeat_probe") else
                    manual.get("additional_probe_from") if manual.get("additional_probe") else None)
        if not previous:
            break
        source = Path(previous).resolve()


def read_probe_history(source, cfg_sha256):
    """Collect planned sites and unit IDs across additional/repeat source chains."""
    used_sites, used_ids = set(), set()
    for run, _ in _history_runs(source, cfg_sha256):
        with (run / "pre_experiment/candidate_sites.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                used_ids.add(row["unit_id"])
                used_sites.update(int(e) for e in row["electrodes"].split(";") if e)
    return used_sites, used_ids


def read_probe_roster(source, cfg_sha256):
    """Merge original four-site units; repeated identities must keep identical members."""
    units = {}
    for run, _ in _history_runs(source, cfg_sha256):
        with (run / "pre_experiment/candidate_sites.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                name = row["unit_id"]
                sites = [int(e) for e in row["electrodes"].split(";") if e]
                if not re.fullmatch(r"candidate_[0-9]+", name) or len(sites) != 4 or len(set(sites)) != 4:
                    raise ValueError("整轮重测需要原始完整四电极候选清单")
                rank = int(name.removeprefix("candidate_"))
                unit = dict(unit_id=name, rank=rank, center=int(row.get("center") or sites[0]), electrodes=sites)
                if rank < 1 or (name in units and units[name] != unit):
                    raise ValueError("来源链同编号unit的原始候选不同，拒绝混用")
                units[name] = unit
    return sorted(units.values(), key=lambda u: u["rank"])


def validate_additional_probe(system, stimulation, hardware=False):
    if not system["manual_inputs"].get("additional_probe"):
        return None
    return _validate_source(system, stimulation, hardware, repeat=False)


def validate_repeat_probe(system, stimulation, hardware=False):
    if not system["manual_inputs"].get("repeat_probe"):
        return None
    return _validate_source(system, stimulation, hardware, repeat=True)


def _validate_source(system, stimulation, hardware, repeat):
    manual = system["manual_inputs"]
    if manual.get("additional_probe") and manual.get("repeat_probe"):
        raise ValueError("新增补测和整轮重测不能同时启用")
    key = "repeat_probe_from" if repeat else "additional_probe_from"
    source_path = manual.get(key)
    if not source_path:
        raise ValueError(f"必须提供{key}原Probe run目录")
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
    if repeat:
        roster = read_probe_roster(source, inputs["cfg_sha256"])
        actual = [{k: u[k] for k in ("unit_id", "rank", "center", "electrodes")} for u in units]
        if sorted(actual, key=lambda u: u["rank"]) != roster:
            raise ValueError("整轮重测必须覆盖来源链全部候选，且原编号/中心/四电极与当前CFG一致")
        return units
    used_sites, used_ids = read_probe_history(source, inputs["cfg_sha256"])
    overlap = used_sites.intersection(e for unit in units for e in unit["electrodes"])
    if overlap or used_ids.intersection(u["unit_id"] for u in units):
        raise ValueError(f"补测重复旧候选编号或电极：{sorted(overlap)}；拒绝重复刺激")
    return units
