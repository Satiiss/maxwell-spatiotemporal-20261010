"""Read-only H5/CFG analysis for manually reviewed spontaneous-network centres.

Never imports maxlab, configures hardware, starts Probe or overwrites system.yaml.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import time
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.utils.io import read_session, read_burst_table
from analysis.utils.spontaneous import analyze_spontaneous_network, select_network_candidates
from analysis.utils.network_plots import render_network_figures
from python.utils.persistence import atomic_json, write_csv, sha256, utc_now

PAIR_FIELDS = {"pair_bin_ms", "pair_peak_ratio_threshold", "pair_min_shared_bursts", "pair_min_abs_delay_ms"}
EDGE_FIELDS = ["source_channel", "target_channel", "source_electrode", "target_electrode", "shared_bursts",
               "delay_peak_ms", "signed_cb_minus_ca_ms", "delay_peak_ratio", "delay_peak_count"]
BURST_FIELDS = ["burst_id", "start_sec", "end_sec", "duration_ms", "spike_count"]
CANDIDATE_FIELDS = ["rank", "electrode", "channel", "x_um", "y_um", "out_degree", "in_degree",
                    "firing_rate_hz", "mean_abs_amplitude", "unit_electrodes"]


def validate_config(config):
    for group, keys in {
        "burst": ("bin_ms", "smooth_ms", "threshold_z", "min_duration_ms", "min_spikes"),
        "network": ("electrode_bin_ms", "electrode_peak_ratio_threshold", "pair_bin_ms", "pair_peak_ratio_threshold"),
        "candidates": ("selection_count", "star_count", "minimum_center_distance_um", "grid_neighborhood_radius_um"),
        "plot": ("dpi", "raster_max_display_spikes"),
    }.items():
        for key in keys:
            value = config[group][key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"参数{group}.{key}必须是有限正数")
    for group, key in [("burst", "merge_gap_ms"), ("network", "pair_min_shared_bursts"),
                       ("network", "pair_min_abs_delay_ms"), ("candidates", "minimum_outdegree")]:
        value = config[group][key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"参数{group}.{key}必须是有限非负数")
    for group, key in [("burst", "min_spikes"), ("network", "pair_min_shared_bursts"),
                       ("candidates", "selection_count"), ("candidates", "star_count"),
                       ("candidates", "minimum_outdegree"), ("plot", "raster_max_display_spikes")]:
        if int(config[group][key]) != config[group][key]:
            raise ValueError(f"参数{group}.{key}必须为整数")
    if config["burst"]["bin_ms"] < 1 or min(config["network"]["electrode_bin_ms"], config["network"]["pair_bin_ms"]) < .1:
        raise ValueError("现有burst检测bin至少1ms；延时直方图bin至少0.1ms")
    if config["candidates"]["star_count"] != 6 or config["candidates"]["selection_count"] != 10:
        raise ValueError("本方案记录至多10个候选并标记前6个中心")
    if config["candidates"]["minimum_center_distance_um"] < 52.5 or config["candidates"]["grid_neighborhood_radius_um"] < 17.5:
        raise ValueError("本方案中心间距必须严格>52.5um，保留3x3邻域排斥")
    if config["candidates"]["manual_review_below_peak_outdegree"] != 10 or config["candidates"]["minimum_outdegree"] < 1:
        raise ValueError("最大出度<10时必须人工复核；无合格连接的位点不自动补成候选")
    if config["plot"]["arrow_alpha"] != .2 or config["plot"]["dpi"] != 600:
        raise ValueError("Markdown要求箭头alpha=0.2和600dpi")
    if set(config["plot"]["figure_formats"]) != {"png", "pdf", "svg"}:
        raise ValueError("本实验图形同时保存PNG/PDF/SVG")
    if config["burst"]["smooth_ms"] > 60000:
        raise ValueError("平滑窗口过大，请检查ms单位")


class Progress:
    def __init__(self, logger):
        self.logger = logger
        self.stage = 0
        self.last = 0.0
        self.last_percent = -1

    def emit(self, stage, name, done=0, total=1, force=False):
        fraction = min(1, done/max(total, 1))
        percent = int(fraction*100)
        now = time.monotonic()
        if stage != self.stage:
            self.last_percent = -1
            force = True
        if force or percent == 100 or percent >= self.last_percent+10 or now-self.last >= 10:
            self.stage, self.last, self.last_percent = stage, now, percent
            overall = (stage-1+fraction)/6
            def bar(value):
                filled = int(value*20)
                return "["+"#"*filled+"-"*(20-filled)+"]"
            self.logger.info("总进度%s %5.1f%% | 阶段%d/6 %s %s %d/%d", bar(overall), overall*100,
                             stage, name, bar(fraction), done, total)


def file_reference(path, hash_content=False):
    info = path.stat()
    reference = dict(path=str(path), size_bytes=info.st_size, mtime_ns=info.st_mtime_ns)
    if hash_content:
        reference["sha256"] = sha256(path)
    return reference


def run(args):
    h5, cfg = Path(args.h5).expanduser().resolve(), Path(args.cfg).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()
    for path in (h5, cfg, config_path):
        if not path.is_file():
            raise ValueError(f"文件不存在：{path}")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    validate_config(config)
    burst_path = Path(args.bursts_csv).expanduser().resolve() if args.bursts_csv else None
    inputs = dict(h5=file_reference(h5), cfg=file_reference(cfg, True), well=args.well,
                  manual_bursts=None if burst_path is None else file_reference(burst_path, True))
    previous_parameters, previous_history, revision = None, [], 1
    if args.previous_run:
        previous = Path(args.previous_run).expanduser().resolve()
        if not previous.is_relative_to((ROOT/"data").resolve()):
            raise ValueError("previous-run必须是本项目data目录内的分析结果")
        old_inputs = json.loads((previous/"source_inputs.json").read_text(encoding="utf-8"))
        if old_inputs != inputs:
            raise ValueError("复核必须使用相同H5、CFG、well和人工burst表，禁止换数据后沿用旧参数历史")
        previous_parameters = yaml.safe_load((previous/"parameters.yaml").read_text(encoding="utf-8"))
        old_network = previous_parameters["network"]
        for group in config:
            if group == "network":
                if {k: v for k, v in config[group].items() if k not in PAIR_FIELDS} != {
                        k: v for k, v in old_network.items() if k not in PAIR_FIELDS}:
                    raise ValueError("参数复核仅开放四个pair参数；stable-electrode参数只读")
            elif config[group] != previous_parameters[group]:
                raise ValueError(f"参数复核仅开放四个pair参数；{group}参数只读")
        previous_history = json.loads((previous/"network/network_parameter_history.json").read_text(encoding="utf-8"))
        revision = int(previous_history[-1]["revision"])+1
    if args.keep_low_outdegree and (not args.previous_run or not args.review_reason.strip()):
        raise ValueError("保留低出度网络须指定--previous-run和--review-reason；不会自动批准")

    # Resolve before creating: a symlink to another experiment cannot receive outputs.
    data_root = (ROOT/"data").resolve()
    if not data_root.is_relative_to(ROOT):
        raise ValueError("本项目data目录指向项目外部；拒绝写入其他目录")
    stamp = datetime.now()
    run_dir = data_root/(stamp.strftime("%Y%m%d_%H%M%S")+"_data")
    run_dir.mkdir(parents=True, exist_ok=False)
    output = run_dir/"pre_experiment"
    network_dir = output/"network"
    network_dir.mkdir(parents=True)
    logger = logging.getLogger("baseline_analysis")
    logger.setLevel(logging.INFO)
    handlers = [logging.StreamHandler(), logging.FileHandler(run_dir/"log.txt", encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
    progress = Progress(logger)
    state = dict(updated_at=utc_now(), phase="LOAD_SPONT", h5=str(h5), cfg=str(cfg),
                 parameter_revision=revision, requires_manual_review=True)
    def save_state(phase, **values):
        state.update(phase=phase, updated_at=utc_now(), **values)
        atomic_json(output/"workflow_state.json", state)
    try:
        atomic_json(output/"source_inputs.json", inputs)
        shutil.copy2(cfg, run_dir/stamp.strftime("%Hh%Mm%Ss.cfg"))
        (output/"parameters.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
        save_state("LOAD_SPONT")
        logger.info("只读输入H5：%s | CFG：%s | 本次结果：%s", h5, cfg, output)
        progress.emit(1, "读取spikes与映射", force=True)
        session = read_session(h5, cfg, args.well)
        if not session.mapping or any(not np.isfinite([row["x"], row["y"]]).all() for row in session.mapping):
            raise ValueError("记录映射必须非空且坐标有限")
        if file_reference(h5) != inputs["h5"] or file_reference(cfg, True) != inputs["cfg"]:
            raise ValueError("读取期间输入H5或CFG发生变化；请先确认录制文件已关闭")
        progress.emit(1, "映射核对通过", 1, 1)
        logger.info("记录电极%d；时长%.2fs；采样率%.1fHz；spikes=%d", len(session.mapping),
                    session.duration_sec, session.sampling_hz, len(session.times))
        if abs(session.duration_sec-600) > 1:
            logger.warning("记录时长不是约600s，请核对基线；发放率仍按实际时长计算")
        if session.unmapped_spike_count:
            logger.warning("排除%d条无有效记录映射的spike", session.unmapped_spike_count)
        if session.amplitudes is None:
            logger.warning("H5没有spike amplitude字段；候选幅值并列项按0处理并标明缺失")
        reviewed_bursts = None if burst_path is None else read_burst_table(burst_path)
        save_state("ANALYZE_NETWORK")
        progress.emit(2, "burst检测", force=True)
        def network_progress(phase, done, total, **details):
            if phase == "burst":
                progress.emit(2, f"burst完成：{details['burst_count']}个", done, total)
            else:
                progress.emit(3, "完整电极对延时统计", done, total)
        result = analyze_spontaneous_network(session, config["burst"], config["network"],
                                             network_progress, reviewed_bursts)
        progress.emit(4, "出入度与候选计算", force=True)
        candidates = select_network_candidates(result["metrics"], config["candidates"])
        maximum = max((m["out_degree"] for m in result["metrics"]), default=0)
        accepted = args.keep_low_outdegree and maximum < 10
        phase = ("REVIEW_NETWORK_PARAMETERS" if maximum < 10 and not accepted
                 else "INSUFFICIENT_CANDIDATES" if len(candidates) < 5 else "REVIEW_CANDIDATES")
        stats = dict(recorded_electrodes=len(session.mapping), duration_sec=session.duration_sec,
            sampling_hz=session.sampling_hz, valid_spikes=int(sum(m["spike_count"] for m in result["metrics"])),
            excluded_unmapped_spikes=session.unmapped_spike_count,
            excluded_outside_recording_spikes=result["excluded_outside_recording_spikes"],
            burst_count=len(result["bursts"]), burst_source="reviewed_csv" if burst_path else "existing_detector",
            burst_occupancy_fraction=sum(b-a for a, b in result["bursts"])/session.duration_sec,
            stable_electrodes=sum(bool(m["stable_activation"]) for m in result["metrics"]),
            qualified_edges=len(result["edges"]), maximum_outdegree=maximum,
            candidate_count=len(candidates), proposed_centres=[r["electrode"] for r in candidates[:6]],
            phase=phase, parameter_revision=revision, requires_manual_review=True)
        progress.emit(4, "出入度与候选完成", 1, 1)
        for candidate in candidates:
            logger.info("候选#%d 电极%d 通道%d 出度%d 入度%d rate=%.4fHz | unit=%s",
                candidate["rank"], candidate["electrode"], candidate["channel"], candidate["out_degree"],
                candidate["in_degree"], candidate["firing_rate_hz"], candidate["unit_electrodes"])
        progress.emit(5, "保存表格与参数历史", force=True)
        write_csv(network_dir/"stable_electrode_metrics.csv", result["metrics"])
        write_csv(network_dir/"stable_delay_edges.csv", result["edges"], EDGE_FIELDS)
        write_csv(output/"baseline_bursts.csv", result["burst_table"], BURST_FIELDS)
        write_csv(network_dir/"candidate_sites.csv", [{k: json.dumps(r[k]) if k == "unit_electrodes" else r[k]
                   for k in CANDIDATE_FIELDS} for r in candidates], CANDIDATE_FIELDS)
        np.savez_compressed(network_dir/"first_activation.npz", channels=result["channels"],
            first_activation_ms=result["first_activation"], bursts_sec=np.asarray(result["bursts"]).reshape(-1, 2))
        atomic_json(output/"baseline_statistics.json", stats)
        history = previous_history+[dict(revision=revision, time=utc_now(), before_parameters=previous_parameters,
            after_parameters=config, burst_count=stats["burst_count"], stable_electrodes=stats["stable_electrodes"],
            edge_count=stats["qualified_edges"], maximum_outdegree=maximum,
            decision="keep_current" if accepted else "parameters_recomputed" if revision > 1 else "pending_review",
            reason=args.review_reason if accepted else "", phase=phase)]
        atomic_json(network_dir/"network_parameter_history.json", history)
        write_csv(network_dir/"network_parameter_history.csv", [{**r,
            "before_parameters": json.dumps(r["before_parameters"], ensure_ascii=False),
            "after_parameters": json.dumps(r["after_parameters"], ensure_ascii=False)} for r in history])
        proposed = dict(status="DRAFT_REQUIRES_MANUAL_REVIEW", network_review_phase=phase,
            baseline_h5=str(h5), cfg_path=str(cfg), burst_csv=str(output/"baseline_bursts.csv"),
            candidate_centers=stats["proposed_centres"], sufficient_centres=len(candidates) >= 5,
            units=[dict(center=r["electrode"], electrodes=r["unit_electrodes"]) for r in candidates[:6]])
        (output/"candidate_centers_proposed.yaml").write_text(
            yaml.safe_dump(proposed, sort_keys=False, allow_unicode=True), encoding="utf-8")
        progress.emit(5, "表格保存完成", 1, 1)
        plot_config = dict(config["plot"], star_count=config["candidates"]["star_count"])
        progress.emit(6, "生成6张图（PNG/PDF/SVG）", force=True)
        manifest = render_network_figures(session, result, candidates, network_dir, plot_config,
            revision, lambda n, total, name: progress.emit(6, name, n, total))
        atomic_json(network_dir/"figure_manifest.json", manifest)
        save_state(phase, candidate_centers=stats["proposed_centres"], maximum_outdegree=maximum,
                   low_outdegree_manually_accepted=bool(accepted), output=str(output))
        summary = (f"分析完成\nburst数: {stats['burst_count']}\n稳定电极数: {stats['stable_electrodes']}\n"
                   f"合格连接数: {stats['qualified_edges']}\n最大出度: {maximum}\n"
                   f"候选中心数: {len(candidates)}（建议使用前5–6个，须人工复核）\n"
                   f"状态: {phase}\n结果目录: {output}\n")
        (output/"summary.txt").write_text(summary, encoding="utf-8")
        logger.info("\n%s", summary)
        if stats["burst_count"] <= config["network"]["pair_min_shared_bursts"]:
            logger.warning("burst数量不足以满足严格共享burst门槛；本次不能据此自动选择Probe中心")
        if stats["burst_occupancy_fraction"] > .5:
            logger.warning("burst区间覆盖超过一半录制时间，请人工查看burst_overview和raster")
        if phase == "REVIEW_NETWORK_PARAMETERS":
            logger.warning("最大出度<10：待人工参数复核；未降低阈值或启动Probe")
        if len(candidates) < 5:
            logger.warning("空间排斥和unit不重叠条件下不足5个中心，请人工复核；未补入不合格位点")
        return output
    except BaseException as error:
        save_state("STOPPED" if isinstance(error, KeyboardInterrupt) else "FAILED", error=str(error))
        raise
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def parser():
    command = argparse.ArgumentParser(description="自发基线burst、完整连接出入度Map及候选中心；无硬件操作")
    command.add_argument("--h5", required=True)
    command.add_argument("--cfg", required=True)
    command.add_argument("--well", type=int, default=0, help="Python从0计数；界面Well 1对应0")
    command.add_argument("--config", default=str(ROOT/"config/baseline_analysis.yaml"))
    command.add_argument("--bursts-csv", help="可选人工复核的start_sec,end_sec；相对H5首帧")
    command.add_argument("--previous-run", help="本项目旧结果目录；继承参数历史，仅允许修改四个pair参数")
    command.add_argument("--keep-low-outdegree", action="store_true", help="人工选择保持低出度参数；须提供旧结果和原因")
    command.add_argument("--review-reason", default="")
    return command


if __name__ == "__main__":
    try:
        run(parser().parse_args())
    except KeyboardInterrupt:
        print("分析已停止；已保存部分结果及状态。")
        raise SystemExit(130)
    except Exception as error:
        print(f"分析失败：{error}")
        raise SystemExit(1)
