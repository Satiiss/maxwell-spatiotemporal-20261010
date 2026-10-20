"""Generate a candidate CFG in an isolated SDK Array; never download or stimulate."""
from __future__ import annotations

from pathlib import Path
from numbers import Integral
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


def validate_stimulation_request(source_cfg, electrodes, max_attempts=4):
    """Validate explicit physical sites; no SDK access or experimental defaults."""
    source = Path(source_cfg).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("来源CFG必须是文件")
    original = parse_cfg(source)
    sites = list(electrodes)
    if (not 1 <= len(sites) <= 32 or any(isinstance(e, bool) or not isinstance(e, Integral)
            or not 0 <= e < 26400 for e in sites) or len(set(sites)) != len(sites)):
        raise ValueError("刺激电极必须为1–32个不同的有效整数ID")
    sites = [int(e) for e in sites]
    if not set(sites).issubset({r['electrode'] for r in original}):
        raise ValueError("所选刺激电极必须属于原CFG；不会换成邻近位点")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 16:
        raise ValueError("max_attempts必须为1–16整数")
    return source, original, sites


def _joint_software_pool(array, mapping, sites):
    """Match the formal runner's clear/connect/query path, on a scratch Array only."""
    for row in mapping:
        value = array.query_stimulation_at_electrode(row['electrode'])
        if value is None:
            raise RuntimeError("SDK查询返回None，不能解释为空连接")
        if str(value).strip():
            require_ok(array.disconnect_electrode_from_stimulation(row['electrode']), "移除软件Array旧刺激连接")
    # Query AFTER connecting the whole pool: a later connection may change earlier assignments.
    for electrode in sites:
        require_ok(array.connect_electrode_to_stimulation(electrode), f"软件连接电极{electrode}")
    pool, unavailable = {}, []
    for electrode in sites:
        value = array.query_stimulation_at_electrode(electrode)
        if value is None:
            raise RuntimeError("SDK查询返回None，不能解释为空连接")
        text = str(value).strip()
        if not text:
            unavailable.append(electrode)
        elif not text.isdigit() or not 0 <= int(text) < 32:
            raise RuntimeError(f"电极{electrode}返回无效刺激单元：{value!r}")
        else:
            pool[str(electrode)] = int(text)
    conflicts = [dict(stim_unit=unit, electrodes=[int(e) for e, u in pool.items() if u == unit])
                 for unit in sorted(set(pool.values())) if list(pool.values()).count(unit) > 1]
    return dict(pool=pool, unavailable_electrodes=unavailable, conflicts=conflicts,
                independent=not unavailable and not conflicts and len(pool) == len(sites))


def prepare_stimulation_candidate(source_cfg, electrodes, output, mx, *, max_attempts=4, emit=print):
    """Try a joint independent pool without changing physical sites or chip state.

    Only isolated SDK Array methods are used. No initialize/activate/download/offset,
    StimulationUnit, DAC, Sequence, recording, configuration activation or Probe handoff.
    A passing result is a SOFTWARE candidate, never evidence of hardware delivery.
    """
    import time
    from datetime import datetime, timezone
    import logging
    import threading

    source, original, sites = validate_stimulation_request(source_cfg, electrodes, max_attempts)
    output = Path(output).expanduser().resolve()
    # Never allow a caller to target a source file or overwrite an existing directory.
    output.mkdir(parents=True, exist_ok=False)
    source_hash = sha256(source)
    report = dict(status='preparing', source_cfg=str(source), source_cfg_sha256=source_hash,
                  requested_electrodes=sites, original_recording_electrodes=[r['electrode'] for r in original],
                  max_attempts=max_attempts, attempts=[], candidate_cfg=None,
                  source_cfg_unchanged=True, hardware_downloaded=False, hardware_tested=False,
                  stimulation_sent=False, configuration_activated=False, formal_started=False,
                  probe_evidence_reused=False, started_utc=datetime.now(timezone.utc).isoformat())
    report_path = output / 'routing_preparation.json'
    atomic_json(report_path, report)
    started = time.monotonic()

    def inspect_cfg(path, label):
        array = None
        try:
            array = mx.Array('formal_check_' + uuid.uuid4().hex)
            require_ok(array.load_config(str(path)), "加载独立软件CFG")
            before = parse_cfg(path)
            check = _joint_software_pool(array, before, sites)
            # Connecting stimulation must not silently change the readout mapping.
            rows = [dict(channel=int(r.channel), electrode=int(r.electrode), x=float(r.x), y=float(r.y))
                    for r in array.get_config().mappings]
            check['recording_mapping_unchanged_by_connect'] = mapping_comparison(before, rows)['exact_recording_mapping']
            check['independent'] &= check['recording_mapping_unchanged_by_connect']
            emit(f"{label}：{'联合独立连接通过' if check['independent'] else '未通过'}；"
                 f"分配={check['pool']}；冲突={check['conflicts']}；不可路由={check['unavailable_electrodes']}", flush=True)
            return check
        finally:
            if array is not None:
                array.close()

    try:
        emit(f"仅服务器软件计算：{len(original)}个记录电极、{len(sites)}个指定刺激电极；不下载、不刺激", flush=True)
        report['original_check'] = inspect_cfg(source, '原CFG')
        if report['original_check']['independent']:
            report.update(status='original_cfg_already_independent', selected_cfg=str(source),
                          baseline_mapping_reusable=True, same_recording_electrodes=True,
                          joint_check=report['original_check'])
            emit("原CFG已满足软件独立连接，不生成替代CFG；仍未验证实际硬件", flush=True)
            return report
        for index in range(max_attempts):
            array = None
            attempt = dict(attempt=index + 1, status='preparing')
            report['attempts'].append(attempt)
            candidate_path = output / f'attempt_{index+1:02d}.cfg'
            # The SDK offers priority/order, not a public arbitrary stim-unit assignment API.
            # Try bounded, deterministic input orders; no electrodes are replaced or dropped.
            order = [r['electrode'] for r in original]
            shift = (index // 2 * max(1, len(order) // max_attempts)) % len(order)
            order = order[shift:] + order[:shift]
            targets = list(sites)
            if index % 2:
                order.reverse()
                targets.reverse()
            attempt.update(recording_order=order, stimulation_priority_order=targets,
                           candidate_cfg=str(candidate_path))
            emit(f"软件路由尝试 {index+1}/{max_attempts}：保留全部物理电极，提高指定刺激位点优先级", flush=True)
            try:
                array = mx.Array('formal_route_' + uuid.uuid4().hex)
                require_ok(array.reset(), "重置独立软件Array")
                require_ok(array.clear_selected_electrodes(), "清除独立软件选择")
                require_ok(array.select_electrodes(order), "选择原记录电极")
                require_ok(array.select_stimulation_electrodes(targets), "提高指定刺激位点优先级")
                done = threading.Event()
                def beat():
                    while not done.wait(10):
                        logging.info("软件路由尝试%d/%d仍在计算；未下载或刺激", index+1, max_attempts)
                heartbeat = threading.Thread(target=beat, daemon=True)
                heartbeat.start()
                try:
                    require_ok(array.route(), "计算候选软件路由")
                finally:
                    done.set()
                    heartbeat.join()
                require_ok(array.save_config(str(candidate_path)), "保存候选软件CFG")
            finally:
                if array is not None:
                    array.close()
            candidate = parse_cfg(candidate_path)
            comparison = mapping_comparison(original, candidate)
            attempt['mapping_comparison'] = comparison
            emit("映射核对：缺失%d，新增%d，换通道%d，坐标变化%d" % (
                 len(comparison['missing_electrodes']), len(comparison['added_electrodes']),
                 len(comparison['changed_channel_electrodes']), len(comparison['changed_coordinate_electrodes'])), flush=True)
            if (comparison['missing_electrodes'] or comparison['added_electrodes']
                    or comparison['changed_coordinate_electrodes']):
                attempt['status'] = 'rejected_recording_electrodes_changed'
                atomic_json(report_path, report)
                continue
            # Fresh scratch Array + serialized CFG verifies the later reload/connect path.
            attempt['joint_check'] = inspect_cfg(candidate_path, f"尝试{index+1}")
            attempt['status'] = 'software_passed' if attempt['joint_check']['independent'] else 'rejected_joint_pool'
            atomic_json(report_path, report)
            if attempt['joint_check']['independent']:
                accepted = output / 'candidate_formal.cfg'
                with accepted.open('xb') as handle:
                    handle.write(candidate_path.read_bytes())
                report.update(status='candidate_for_hardware_routing_check', candidate_cfg=str(accepted),
                              candidate_cfg_sha256=sha256(accepted), joint_check=attempt['joint_check'],
                              mapping_comparison=comparison, same_recording_electrodes=True,
                              baseline_mapping_reusable=comparison['exact_recording_mapping'],
                              fresh_probe_required=True, automatic_handoff_allowed=False)
                emit("候选CFG软件检查通过；尚未下载或验证刺激。不能将旧Probe分数改写成新CFG下的成绩。", flush=True)
                return report
        report.update(status='no_solution_found', same_recording_electrodes=None,
                      baseline_mapping_reusable=False)
        emit("有限次软件尝试未找到满足条件的CFG；这不证明硬件上不存在其他解，不自动换电极或放宽冲突检查。", flush=True)
        return report
    except BaseException as exc:
        report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        if report['attempts'] and report['attempts'][-1]['status'] == 'preparing':
            report['attempts'][-1].update(status='failed', error=str(exc))
        raise
    finally:
        try:
            unchanged = sha256(source) == source_hash
        except OSError:
            unchanged = False
        report.update(source_cfg_unchanged=unchanged, elapsed_sec=time.monotonic()-started,
                      finished_utc=datetime.now(timezone.utc).isoformat())
        if not unchanged:
            report.update(status='source_changed', candidate_cfg=None, error='来源CFG在准备期间发生变化')
        write_csv(output / 'joint_pool.csv', [dict(electrode=int(e), stim_unit=u)
                  for e, u in report.get('joint_check', {}).get('pool', {}).items()],
                  fields=['electrode', 'stim_unit'])
        atomic_json(report_path, report)
        if not unchanged:
            raise RuntimeError("来源CFG发生变化，候选结果不可使用")
