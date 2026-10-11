"""One entry point for GUI, dry-run, mock run and explicit hardware execution."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import logging
import math
import os
import platform
from pathlib import Path
import subprocess
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parent


def load_configuration(directory):
    import yaml
    directory = Path(directory)
    with (directory / "system.yaml").open(encoding="utf-8") as handle:
        system = yaml.safe_load(handle)
    with (directory / "stimulation.yaml").open(encoding="utf-8") as handle:
        stimulation = yaml.safe_load(handle)
    root = Path(system["data"]["root"])
    system["data"]["root"] = str(root if root.is_absolute() else ROOT / root)
    # User-entered relative paths are relative to the configuration directory.
    paths = [(system["electrode_map"], "cfg_path"), (system["manual_inputs"], "baseline_h5"),
             (system["manual_inputs"], "baseline_bursts_csv"), (system["manual_inputs"], "network_review_record"),
             (system["maxwell"], "api_parent"), (system["maxwell"], "maxlab_root")]
    for mapping, key in paths:
        value = mapping.get(key)
        if value:
            p = Path(value).expanduser()
            mapping[key] = str(p if p.is_absolute() else directory.resolve() / p)
    return system, stimulation


def check_environment(hardware=False, api_parent=None):
    if api_parent:
        sys.path.insert(0, str(api_parent))
    modules = {"numpy": "numpy", "h5py": "h5py", "PyYAML": "yaml", "matplotlib": "matplotlib"}
    if hardware:
        modules["maxlab"] = "maxlab"
    missing = []
    for package, module in modules.items():
        try:
            imported = importlib.import_module(module)
            logging.info("依赖 %s: %s", package, getattr(imported, "__version__", "OK"))
        except ImportError as exc:
            missing.append(f"{package} ({exc})")
    if missing:
        raise RuntimeError("缺失依赖: " + "; ".join(missing) +
                           "; 请执行 pip install -r requirements.txt；maxlab请安装官方SDK并配置api_parent")
    logging.info("环境检查: OK")


def create_run(system):
    root = Path(system["data"]["root"])
    root.mkdir(parents=True, exist_ok=True)
    path = root / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_data")
    # Do not silently reuse or suffix a timestamp directory.
    path.mkdir(exist_ok=False)
    return path


def validate(system, stimulation, hardware=False, probe_only=False):
    from python.schedule import Condition, waveform_commands, next_onset_interval, pulse_duration_sec
    p = stimulation["protocol"]
    if stimulation["probe"].get("order", "sequential") not in ("sequential", "unit_interleaved"):
        raise ValueError("probe.order只能为sequential或unit_interleaved")
    if stimulation["probe"].get("order") == "unit_interleaved" and not math.isclose(
            float(stimulation["probe"]["frequency_hz"]), 2.0):
        raise ValueError("已确认的交替Probe要求全局2Hz，即每0.5秒刺激下一位点")
    tail = float(stimulation["probe"].get("recording_tail_sec", 1.0))
    if probe_only and (not math.isfinite(tail) or tail < .5):
        raise ValueError("probe.recording_tail_sec必须至少0.5秒；停止录制前需要保留spike处理时间")
    if (not 1 <= stimulation["probe"]["trials_per_electrode"] or
            (not probe_only and not 1 <= stimulation["formal"]["trials_per_condition"])):
        raise ValueError("trial数必须为正整数")
    if not probe_only and stimulation["formal"]["temporal_interval_mode"] not in ("after_second", "onset_to_onset"):
        raise ValueError("时序间隔模式必须显式设置")
    if not probe_only and stimulation["formal"]["condition_order"] != "cyclic":
        raise ValueError("本方案要求循环交叉条件刺激")
    if system["routing"]["mode"] != "pool_fast" or not system["routing"]["require_unique_stim_units"]:
        raise ValueError("交叉条件必须快切，且禁止共用stim unit")
    if float(system["routing"]["settle_wait_ms"]) < 0:
        raise ValueError("settle wait不能为负")
    if not system["maxwell"]["raw_recording"]:
        raise ValueError("本次方案需要保留原始波形排查伪迹及第一脉冲污染，请启用raw_recording")
    if p["sequence_initial_delay_samples"] < 0:
        raise ValueError("初始序列延迟不能为负")
    waveform_commands(Condition("validation_probe", "probe", (1,), ("a",)), p, 2.98, 1)
    for dt in ([] if probe_only else stimulation["formal"]["dt_ms"]):
        if dt <= 0:
            raise ValueError("dt必须为正")
        c = Condition("validation", "temporal", (1, 2), ("a", "b"), float(dt))
        waveform_commands(c, p, 2.98, 1)
        next_onset_interval(c, stimulation["formal"], pulse_duration_sec(p))
    if hardware:
        if platform.system() != "Linux":
            raise RuntimeError("MaxLab真实执行路径面向Linux设备电脑；Windows可模拟、预览或配置")
        cfg = system["electrode_map"].get("cfg_path")
        if not cfg or not Path(cfg).is_file():
            raise ValueError("请填写实际electrode_map.cfg_path；当前允许留空生成代码")
        if not system["culture"].get("id") or system["culture"].get("div") is None:
            raise ValueError("真实实验前须填写culture.id与culture.div")
        if len(system["manual_inputs"]["candidate_centers"]) not in (5, 6):
            raise ValueError("真实实验前须填写5–6个手动审阅的候选中心电极")
        if stimulation["probe"]["threshold_rule"] not in ("mean_plus_3sd", "mean_plus_3variance"):
            raise ValueError("probe.threshold_rule尚未明确：请选择三倍标准差或三倍方差公式")
        for key in ("baseline_h5", "baseline_bursts_csv", "network_review_record"):
            value = system["manual_inputs"].get(key)
            if value and not Path(value).is_file():
                raise ValueError(f"{key}文件不存在")


def build_cpp(system):
    sdk = system["maxwell"].get("maxlab_root") or os.getenv("MAXLAB_ROOT")
    if not sdk:
        raise ValueError("编译C++读取器需要maxwell.maxlab_root或MAXLAB_ROOT")
    build_dir = ROOT / "cpp" / "build"
    subprocess.run(["cmake", "-S", str(ROOT / "cpp"), "-B", str(build_dir), f"-DMAXLAB_ROOT={sdk}"], check=True)
    subprocess.run(["cmake", "--build", str(build_dir)], check=True)
    executable = build_dir / ("spike_monitor.exe" if os.name == "nt" else "spike_monitor")
    if not executable.is_file():
        raise RuntimeError("C++构建后未找到读取器")
    system["maxwell"]["monitor_executable"] = str(executable)


def make_plan(system, stimulation, probe_only=False):
    if probe_only:
        centers = system["manual_inputs"]["candidate_centers"]
        return dict(execution_scope="probe_only", stages=[], formal_trial_count=0,
                    probe_order=stimulation["probe"].get("order", "sequential"),
                    formal_nominal_duration_sec=0,
                    raw_uncompressed_bytes_at_1024_channels_20khz=0,
                    probe_trial_count=(len(centers) if centers else 6) * 4 * stimulation["probe"]["trials_per_electrode"],
                    pending_inputs=["cfg_path", "culture", "candidate_centers", "probe.threshold_rule"],
                    full_analysis_deferred=True)
    from python.schedule import formal_conditions, round_robin, Condition, next_onset_interval, pulse_duration_sec
    units = [dict(electrodes=[10 * i, 10 * i + 1]) for i in range(1, 4)]
    stages = formal_conditions(units, stimulation["formal"]["dt_ms"])
    formal = stimulation["formal"]
    rounds = formal["trials_per_condition"]
    pulse_duration = pulse_duration_sec(stimulation["protocol"])
    stage_rows = [dict(stage=stage, conditions=len(conditions), trials=len(conditions) * rounds,
                       nominal_duration_sec=sum(next_onset_interval(c, formal, pulse_duration) for c in conditions) * rounds)
                  for stage, conditions in stages]
    duration = sum(r["nominal_duration_sec"] for r in stage_rows) + formal["pre_spontaneous_sec"] + formal["post_spontaneous_sec"]
    duration += (len(stage_rows) - 1) * formal["rest_between_stages_sec"] + formal["rest_after_last_stage_sec"]
    return dict(execution_scope="full", stages=stage_rows, formal_trial_count=sum(r["trials"] for r in stage_rows),
                formal_nominal_duration_sec=duration, temporal_interval_mode=formal["temporal_interval_mode"],
                raw_uncompressed_bytes_at_1024_channels_20khz=duration * 1024 * 20000 * 2,
                pending_inputs=["cfg_path", "culture", "candidate_centers", "probe.threshold_rule"],
                full_analysis_deferred=True)


def execute(system, stimulation, mode="mock", reporter=None, dry_run=False, quick=False, compile_cpp=False,
            probe_only=True, formal_from=None, routing_check=False, probe_order=None):
    from python.utils.progress import Reporter
    from python.utils.persistence import atomic_json
    import yaml
    import copy
    system, stimulation = copy.deepcopy(system), copy.deepcopy(stimulation)
    if routing_check and formal_from is not None:
        raise ValueError("路由检查不能与正式实验同时运行")
    if probe_order is not None:
        stimulation["probe"]["order"] = probe_order
    stimulation["probe"].setdefault("order", "sequential")
    stimulation["probe"].setdefault("recording_tail_sec", 1.0)
    if formal_from is not None:
        probe_only = False
    elif not probe_only:
        raise ValueError("Probe和正式实验必须分开启动；正式实验请提供--formal-from Probe结果目录")
    if stimulation["probe"].get("enforce_artifact_qc"):
        logging.warning("按最新Probe要求，伪迹仅作诊断；本次覆盖旧enforce_artifact_qc为false并保存快照")
    stimulation["probe"]["enforce_artifact_qc"] = False
    system["experiment"]["execution_scope"] = ("probe_routing_check" if routing_check else
                                                ("probe_only" if probe_only else "formal_only"))
    handoff = None
    if formal_from is not None:
        from python.probe_handoff import load_probe_handoff
        handoff = load_probe_handoff(formal_from, system, stimulation, hardware=mode == "hardware")
    reporter = reporter or Reporter()
    run_dir = create_run(system)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(run_dir / "log.txt", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    try:
        check_environment(hardware=mode == "hardware" and not dry_run, api_parent=system["maxwell"].get("api_parent"))
        validate(system, stimulation, hardware=mode == "hardware" and not dry_run, probe_only=probe_only)
        plan = make_plan(system, stimulation, probe_only=probe_only)
        if formal_from is not None:
            plan.update(execution_scope="formal_only", probe_trial_count=0, source_probe_run=str(Path(formal_from).resolve()))
        if routing_check:
            plan.update(execution_scope="probe_routing_check", probe_trial_count=0, stimulation_sent=False)
        atomic_json(run_dir / "experiment_plan.json", plan)
        if routing_check:
            logging.info("run=%s | 仅检查原候选unit的四位点独立路由；不发送刺激、不采集H5", run_dir)
        elif probe_only:
            logging.info("run=%s | 仅运行Probe，完成后停止；不会进入正式实验", run_dir)
        else:
            logging.info("run=%s | 正式trial=%d | 名义时长=%.1fmin | raw未压缩估计=%.1fGB",
                         run_dir, plan["formal_trial_count"], plan["formal_nominal_duration_sec"] / 60,
                         plan["raw_uncompressed_bytes_at_1024_channels_20khz"] / 1e9)
        if dry_run:
            snapshot = run_dir / "config_snapshot"
            snapshot.mkdir()
            for name, cfg in (("system", system), ("stimulation", stimulation)):
                (snapshot / f"{name}.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
            atomic_json(run_dir / "dry_run.json", dict(status="plan_only", hardware_contacted=False, **plan))
            return run_dir
        if mode == "hardware":
            if compile_cpp or system["maxwell"]["monitor_enabled"]:
                build_cpp(system)
            from python.maxwell_setup import MaxLabBackend
            backend = MaxLabBackend(system, reporter)
        else:
            if quick:
                system["mock"]["trials_per_condition_override"] = 2
                system["mock"]["baseline_duration_override_sec"] = 40
            from python.mock_backend import MockBackend
            backend = MockBackend(system, reporter)
        from python.experiment_runner import ExperimentRunner
        runner = ExperimentRunner(system, stimulation, run_dir, backend, reporter,
                                  probe_only=probe_only, formal_handoff=handoff, routing_check=routing_check)
        runner.run()
        return run_dir
    except BaseException as exc:
        logging.exception("运行失败或中止；run=%s", run_dir)
        atomic_json(run_dir / "failure.json", dict(error=str(exc), error_type=type(exc).__name__, run_dir=str(run_dir)))
        raise
    finally:
        file_handler.close()
        logger.removeHandler(file_handler)


def main():
    parser = argparse.ArgumentParser(description="MaxWell 单电极/同步/时序刺激实验")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--mode", choices=["mock", "hardware"], default="mock")
    parser.add_argument("--enable-hardware", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quick", action="store_true", help="只缩短模拟数据；不改变真实协议")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--build-cpp", action="store_true")
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument("--probe-only", action="store_true", help="仅执行Probe（默认）；完成后停止，待人工审阅")
    stage.add_argument("--formal-from", type=Path, help="独立启动正式实验，读取已审阅的Probe结果目录，不重复Probe")
    stage.add_argument("--probe-routing-check", action="store_true", help="只检查候选unit能否四位点独立路由，不发送刺激")
    parser.add_argument("--probe-order", choices=["sequential", "unit_interleaved"],
                        help="Probe顺序；交替模式为A/B/C/D每0.5秒换一位点，各位点每2秒一次")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        system, stimulation = load_configuration(args.config_dir)
        if args.probe_order is not None:
            stimulation["probe"]["order"] = args.probe_order
        wants_gui = args.gui or (system["gui"]["enabled"] and not args.headless and not args.dry_run)
        if wants_gui and (args.probe_routing_check or stimulation["probe"].get("order") == "unit_interleaved"):
            raise ValueError("交替Probe和路由检查请使用--headless终端模式")
        if args.formal_from and (args.gui or (system["gui"]["enabled"] and not args.headless and not args.dry_run)):
            raise ValueError("正式实验请使用--headless --formal-from；GUI仅运行Probe")
        if args.gui or (system["gui"]["enabled"] and not args.headless and not args.dry_run):
            from python.gui import launch
            return launch(system, stimulation, args)
        if args.mode == "hardware" and not args.enable_hardware and not args.dry_run:
            raise ValueError("真实执行请显式使用 --mode hardware --enable-hardware")
        output = execute(system, stimulation, args.mode, dry_run=args.dry_run, quick=args.quick,
                         compile_cpp=args.build_cpp, probe_only=args.formal_from is None, formal_from=args.formal_from,
                         routing_check=args.probe_routing_check, probe_order=args.probe_order)
        print(f"运行结果目录: {output.resolve()}")
        return 0
    except KeyboardInterrupt:
        logging.error("用户中断")
        return 130
    except Exception:
        logging.exception("启动/执行失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
