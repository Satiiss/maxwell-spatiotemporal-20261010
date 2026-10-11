"""Generate a candidate CFG in an isolated SDK Array; never download or stimulate."""
from __future__ import annotations

from pathlib import Path
import uuid

import numpy as np

from analysis.utils.io import parse_cfg
from .maxwell_setup import require_ok
from .site_selection import build_local_units
from .utils.persistence import atomic_json, sha256, write_csv


def mapping_comparison(original, candidate):
    old = {r["electrode"]: r for r in original}
    new = {r["electrode"]: r for r in candidate}
    missing, added = sorted(old.keys() - new.keys()), sorted(new.keys() - old.keys())
    common = old.keys() & new.keys()
    channels = sorted(e for e in common if old[e]["channel"] != new[e]["channel"])
    coordinates = sorted(e for e in common if not np.allclose(
        [old[e]["x"], old[e]["y"]], [new[e]["x"], new[e]["y"]], rtol=0, atol=.1))
    return dict(original_count=len(old), candidate_count=len(new), missing_electrodes=missing,
                added_electrodes=added, changed_channel_electrodes=channels,
                changed_coordinate_electrodes=coordinates,
                exact_recording_mapping=not (missing or added or channels or coordinates))


def prepare_candidate(system, output, mx, emit=print):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    source = Path(system["electrode_map"]["cfg_path"]).resolve()
    source_hash = sha256(source)
    original = parse_cfg(source)
    manual = system["manual_inputs"]
    units = build_local_units(original, manual["candidate_centers"],
                              manual["minimum_center_distance_pitches"] * manual["electrode_pitch_um"])
    targets = [e for unit in units for e in unit["electrodes"]]
    cfg = output / "candidate_probe.cfg"
    report = dict(source_cfg=str(source), source_cfg_sha256=source_hash,
                  candidate_cfg=str(cfg.resolve()), candidate_units=units, units=[],
                  stimulation_sent=False, hardware_downloaded=False, hardware_tested=False,
                  status="preparing", source_cfg_unchanged=True)
    array = None
    try:
        # Array mutations below are server-side software operations only (official SDK).
        # A unique token prevents touching any existing GUI/experiment Array object.
        array = mx.Array("probe_candidate_" + uuid.uuid4().hex)
        require_ok(array.reset(), "重置独立软件Array")
        require_ok(array.clear_selected_electrodes(), "清除独立Array选择")
        require_ok(array.select_electrodes([r["electrode"] for r in original]), "选择原记录电极")
        require_ok(array.select_stimulation_electrodes(targets), "提高原24个位点路由优先级")
        emit(f"软件路由运行中：记录电极{len(original)}个，原候选位点{len(targets)}个；不下载到芯片", flush=True)
        require_ok(array.route(), "生成候选路由")
        require_ok(array.save_config(str(cfg)), "另存候选CFG")
        candidate = parse_cfg(cfg)
        report["candidate_cfg_sha256"] = sha256(cfg)
        report["mapping_comparison"] = comparison = mapping_comparison(original, candidate)
        emit("映射变化：缺失%d，新增%d，换通道%d，坐标变化%d" % (
            len(comparison["missing_electrodes"]), len(comparison["added_electrodes"]),
            len(comparison["changed_channel_electrodes"]), len(comparison["changed_coordinate_electrodes"])), flush=True)
        mapped = {r["electrode"] for r in candidate}
        # Check the same original nearest neighbours, never replace any candidate.
        try:
            rebuilt = build_local_units(candidate, manual["candidate_centers"],
                manual["minimum_center_distance_pitches"] * manual["electrode_pitch_um"])
            same_units = [u["electrodes"] for u in units] == [u["electrodes"] for u in rebuilt]
        except ValueError:
            same_units = False
        report["nearest_neighbour_units_unchanged"] = same_units
        for unit in units:
            require_ok(array.load_config(str(cfg)), "加载候选软件CFG")
            pool, errors = {}, []
            for electrode in unit["electrodes"]:
                if electrode not in mapped:
                    errors.append(f"电极{electrode}未保留在新记录映射中")
                    continue
                require_ok(array.connect_electrode_to_stimulation(electrode), f"软件连接电极{electrode}")
                value = str(array.query_stimulation_at_electrode(electrode)).strip()
                if not value:
                    errors.append(f"电极{electrode}未分配刺激单元")
                    continue
                try:
                    stimulus = int(value)
                except ValueError as exc:
                    raise RuntimeError(f"软件查询电极{electrode}返回异常: {value!r}") from exc
                if not 0 <= stimulus < 32:
                    raise RuntimeError(f"电极{electrode}返回越界刺激单元{stimulus}")
                shared = [e for e, s in pool.items() if s == stimulus]
                if shared:
                    errors.append(f"电极{shared[0]}与{electrode}共用刺激单元{stimulus}")
                pool[electrode] = stimulus
            row = dict(unit_id=unit["unit_id"], electrodes=unit["electrodes"], pool=pool,
                       can_interleave=not errors and len(pool) == 4, error="；".join(errors))
            report["units"].append(row)
            emit(f"{unit['unit_id']}: {'软件路由通过' if row['can_interleave'] else row['error']}", flush=True)
            atomic_json(output / "routing_preparation.json", report)
        available = sum(r["can_interleave"] for r in report["units"])
        report["software_routable_unit_count"] = available
        report["baseline_mapping_reusable"] = comparison["exact_recording_mapping"] and same_units
        report["status"] = ("routing_unavailable" if available < 3 else
                            "mapping_review_required" if not report["baseline_mapping_reusable"] else
                            "candidate_for_hardware_routing_check")
        write_csv(output / "routing_preparation.csv", report["units"])
        emit(f"软件可交替unit：{available}/{len(units)}；记录映射完全一致：{comparison['exact_recording_mapping']}", flush=True)
        emit(f"原邻居unit不变：{same_units}；状态：{report['status']}", flush=True)
        emit(f"原CFG及芯片当前路由不变。候选CFG尚未下载、未验证硬件、未进行Probe。结果目录：{output.resolve()}", flush=True)
        return report
    except BaseException as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        try:
            if array is not None:
                array.close()
        finally:
            report["source_cfg_unchanged"] = sha256(source) == source_hash
            atomic_json(output / "routing_preparation.json", report)
