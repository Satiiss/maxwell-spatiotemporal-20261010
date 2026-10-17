"""Edit centers here; preview or acquire with the same fixed commands every round."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Edit this section. CONFIG_DIR holds this culture's CFG, baseline and acquisition settings.
CONFIG_DIR = "config"
CENTER_ELECTRODES = []  # Electrode IDs, in the order you want to test them.
CULTURE_DIV = None  # None keeps system.yaml; set this to the actual DIV when needed.
MIN_SUCCESS_TRIALS = 6  # Inclusive count out of 10 complete trials.
# End editable section. No old Probe run, candidate review file or mode flag is needed.


def configure_probe(system, stimulation, centers, *, minimum=6, culture_div=None):
    """Build this round's config in memory; never edit the source YAML or old results."""
    import copy
    from datetime import date

    system, stimulation = copy.deepcopy(system), copy.deepcopy(stimulation)
    manual = system["manual_inputs"]
    # Old workflow options must never affect a fresh center-list acquisition.
    for key in ("additional_probe", "additional_probe_from", "repeat_probe", "repeat_probe_from",
                "candidate_ranks", "network_review_record", "baseline_bursts_csv"):
        manual.pop(key, None)
    manual["candidate_centers"] = list(centers)
    if culture_div is not None:
        if isinstance(culture_div, bool) or not isinstance(culture_div, int) or culture_div < 0:
            raise ValueError("DIV必须为非负整数")
        system["culture"]["div"] = culture_div
    system["experiment"]["date"] = date.today().isoformat()
    stimulation["probe"].update(
        order="sequential", trials_per_electrode=10, frequency_hz=2.,
        threshold_rule="paired_pre_stim_ratio", paired_baseline_window_ms=[-50., -10.],
        response_window_ms=[10., 50.], response_to_baseline_ratio=1.5,
        min_success_trials=minimum, enforce_artifact_qc=False, artifact_window_ms=[0., 3.],
        recording_tail_sec=1.)
    stimulation["protocol"].update(amplitude_mv=400., pulse_width_us=300,
        inter_phase_interval_us=0, polarity="negative_first")
    return system, stimulation


def preflight(system, stimulation):
    """Read current inputs and choose physical neighbors without importing the SDK."""
    from main import make_plan, validate
    from python.acquisition_settings import validate_baseline_acquisition

    culture = system["culture"]
    if not culture.get("id") or culture.get("div") is None:
        raise ValueError("请在当前配置填写培养物编号和DIV")
    cfg = system["electrode_map"].get("cfg_path")
    if not cfg or not Path(cfg).is_file():
        raise ValueError("请在当前配置填写现用CFG路径")
    if not system["manual_inputs"]["candidate_centers"]:
        raise ValueError("请在run_probe.py顶部填写CENTER_ELECTRODES")
    validate(system, stimulation, probe_only=True)
    plan = make_plan(system, stimulation, probe_only=True)
    validate_baseline_acquisition(system)
    return plan


def main():
    import argparse
    import json
    import logging
    from main import execute, load_configuration

    parser = argparse.ArgumentParser(description="中心列表Probe；默认只预演，不连接设备")
    parser.add_argument("--enable-hardware", action="store_true", help="开始真实Probe刺激")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    directory = Path(CONFIG_DIR).expanduser()
    system, stimulation = load_configuration(directory if directory.is_absolute() else ROOT / directory)
    system, stimulation = configure_probe(system, stimulation, CENTER_ELECTRODES,
        minimum=MIN_SUCCESS_TRIALS, culture_div=CULTURE_DIV)
    plan = preflight(system, stimulation)
    print("开始真实Probe刺激" if args.enable_hardware else "仅预演，不连接设备、不发送刺激", flush=True)
    print(f"培养物={system['culture']}；CFG={system['electrode_map']['cfg_path']}", flush=True)
    for unit in plan["candidate_units"]:
        print(f"{unit['unit_id']} 中心={unit['center']} 四电极={unit['electrodes']} "
              f"邻居距离µm={[round(d, 1) for d in unit['distances_um'][1:]]}", flush=True)
    print(f"{len(plan['candidate_units'])}个unit，{len(plan['candidate_units'])*4}个位点，"
          f"计划{plan['probe_trial_count']}次；完整10次中至少{plan['min_success_trials']}次有效", flush=True)
    run = execute(system, stimulation, mode="hardware", probe_only=True, dry_run=not args.enable_hardware)
    result = json.loads((run / "experiment_plan.json").read_text(encoding="utf-8"))
    print(f"本轮计划={result['probe_trial_count']}次；结果目录: {run}")
    print(f"候选图: {run / 'pre_experiment/candidate_units.png'}")


if __name__ == "__main__":
    main()
