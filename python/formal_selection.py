"""Independent, editable formal selections with optional read-only Probe evidence."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import runpy

import yaml

from analysis.utils.io import parse_cfg
from .utils.persistence import sha256


def load_formal_selection(path, system, stimulation, hardware=False):
    path = Path(path).expanduser().resolve()
    selection = (runpy.run_path(str(path))["SELECTION"] if path.suffix == ".py"
                 else json.loads(path.read_text(encoding="utf-8")))
    units, reference = validate_formal_selection(selection, system, stimulation, path.parent, hardware)
    reference.update(selection_file=str(path), selection_sha256=sha256(path))
    return units, reference


def validate_formal_selection(selection, system, stimulation, base_dir, hardware=False):
    base = Path(base_dir).resolve()
    culture = str(selection.get("culture_id") or "")
    if not culture or culture != str(system["culture"]["id"] or ""):
        raise ValueError("正式选点文件的culture_id必须与当前培养物一致")
    cfg = Path(system["electrode_map"]["cfg_path"])
    cfg_hash = sha256(cfg)
    mapping = {int(r["electrode"]): r for r in parse_cfg(cfg)}
    minimum = selection.get("minimum_success_trials", 6)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 10:
        raise ValueError("minimum_success_trials必须为1–10整数")
    chosen = selection.get("units")
    if not isinstance(chosen, list) or len(chosen) != 3:
        raise ValueError("正式选点必须为3个unit，每个2个不同电极")
    used, seen_units, source_cache, output = set(), set(), {}, []
    for index, entry in enumerate(chosen, 1):
        sites = entry.get("electrodes")
        if (not isinstance(sites, list) or len(sites) != 2 or any(
                isinstance(e, bool) or not isinstance(e, int) for e in sites)
                or len(set(sites)) != 2 or used.intersection(sites)):
            raise ValueError("正式选点必须有6个不同的整数电极ID")
        if any(e not in mapping for e in sites):
            raise ValueError("正式电极不属于当前CFG")
        source_path = entry.get("source_run")
        label = str(entry.get("source_unit") or entry.get("name") or f"manual_{index:02d}")
        exceptions = entry.get("exceptions", {})
        if not isinstance(exceptions, dict) or any(str(k) not in {str(e) for e in sites} for k in exceptions):
            raise ValueError("exceptions只能逐电极填写已选择位点的理由")
        metrics = []
        if source_path:
            source = Path(source_path).expanduser()
            source = (source if source.is_absolute() else base / source).resolve()
            identity = (str(source), label)
            if source not in source_cache:
                source_cache[source] = _read_probe_evidence(source, system, stimulation, cfg_hash, hardware)
            evidence = source_cache[source]
            allowed = evidence["members"].get(label)
            if allowed is None or not set(sites).issubset(allowed):
                raise ValueError("正式电极不属于所注明的源Probe unit")
            for electrode in sites:
                row = evidence["metrics"].get((label, electrode))
                if row is None or row.get("status") != "complete":
                    raise ValueError("引用的Probe位点没有完整实测记录")
                try:
                    total, successes = int(row["all_trials"]), int(row["success_trials"])
                    fraction, response = float(row["success_fraction"]), float(row["response_10_50ms_mean"])
                except (ValueError, TypeError) as exc:
                    raise ValueError("Probe次数或响应字段无效") from exc
                if (total != 10 or not 0 <= successes <= total or not math.isfinite(response)
                        or response < 0 or not math.isclose(fraction, successes / total)):
                    raise ValueError("Probe必须有完整10次且次数/比例一致；缺失数据不能标成实测")
                reason = exceptions.get(electrode, exceptions.get(str(electrode), ""))
                if not isinstance(reason, str):
                    raise ValueError("人工例外理由必须为文本")
                passed = successes >= minimum
                if not passed and not reason.strip():
                    raise ValueError(f"电极{electrode}仅{successes}/10，低于参考门槛；如人工选用须逐电极填写exceptions理由")
                saved_eligible = row.get("eligible", "").strip().lower()
                if saved_eligible not in ("", "true", "false"):
                    raise ValueError("源Probe eligible字段无效")
                metrics.append(dict(electrode=electrode, success_trials=successes, all_trials=total,
                    success_fraction=fraction, response_10_50ms_mean=response,
                    source_eligible=(saved_eligible == "true") if saved_eligible else None,
                    meets_reference_policy=passed,
                    manual_exception_reason=reason.strip() or None,
                    threshold_rule=evidence["reference"]["probe_policy"]["threshold_rule"]))
        else:
            identity = (None, label)
            reason = entry.get("selection_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("无Probe来源的人工选点必须填写selection_reason；不会伪造Probe合格记录")
            metrics = [dict(electrode=e, success_trials=None, all_trials=None,
                meets_reference_policy=None, selection_reason=reason.strip(), evidence="manual_unscored") for e in sites]
        if identity in seen_units:
            raise ValueError("不能把同一源unit重复拆成多个正式unit")
        seen_units.add(identity)
        used.update(sites)
        output.append(dict(formal_unit=index, source_unit=label, electrodes=list(sites),
                           source_probe_run=str(source) if source_path else None, site_metrics=metrics))
    reference = dict(selection_mode="independent_formal_selection", culture_id=culture,
        cfg_sha256=cfg_hash, minimum_success_trials=minimum,
        sources=[v["reference"] for v in source_cache.values()],
        selected_units=output, source_files_modified=False)
    return output, reference


def _read_probe_evidence(source, system, stimulation, cfg_hash, hardware):
    result_file = source / "experiment_result.json"
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if result.get("status") not in ("probe_complete", "probe_review_required"):
        raise ValueError("Probe证据必须为已收尾的Probe；不要求该run整体合格")
    if hardware and result.get("synthetic") is not False:
        raise ValueError("真实正式实验不能引用模拟Probe证据")
    system_file, stim_file = source / "config_snapshot/system.yaml", source / "config_snapshot/stimulation.yaml"
    saved = yaml.safe_load(system_file.read_text(encoding="utf-8"))
    saved_stim = yaml.safe_load(stim_file.read_text(encoding="utf-8"))
    if str(saved["culture"]["id"]) != str(system["culture"]["id"]):
        raise ValueError("源Probe培养物不一致")
    if any(saved["maxwell"][k] != system["maxwell"][k] for k in
           ("device", "well", "event_threshold", "amplifier_gain")):
        raise ValueError("源Probe设备/well/检测阈值/gain不一致")
    if saved_stim["protocol"] != stimulation["protocol"]:
        raise ValueError("正式刺激波形与引用Probe不一致")
    inputs_file = source / "source_inputs.json"
    inputs = json.loads(inputs_file.read_text(encoding="utf-8"))
    if inputs["cfg_sha256"] != cfg_hash:
        raise ValueError("源Probe CFG与正式CFG不一致")
    summary_file, members_file = source / "pre_experiment/probe_summary.csv", source / "pre_experiment/candidate_sites.csv"
    members, metrics = {}, {}
    with members_file.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["unit_id"] in members:
                raise ValueError("源Probe候选unit重复")
            members[row["unit_id"]] = {int(e) for e in row["electrodes"].split(";") if e}
    with summary_file.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = row["unit_id"], int(row["electrode"])
            if key in metrics:
                raise ValueError("源Probe汇总存在重复电极")
            metrics[key] = row
    files = [result_file, system_file, stim_file, inputs_file, summary_file, members_file]
    return dict(members=members, metrics=metrics, reference=dict(source_probe_run=str(source),
        status=result["status"], synthetic=result.get("synthetic"),
        source_probe_trial_count=result.get("probe_trial_count"), probe_policy=saved_stim["probe"],
        file_sha256={str(p): sha256(p) for p in files}))
