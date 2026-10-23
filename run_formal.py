"""Edit the selection below; use fixed commands to plan, check routing, or acquire."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Edit only this section. Paths inside SELECTION are relative to this file.
CONFIG_DIR = "config"  # Set to your current culture's configuration directory.
INTER_SEQUENCE_REST_SEC = 1.0  # Seconds after the last complete pulse; None keeps the YAML value.
SCHEDULE_MODE = 'pair_batches'  # 'legacy' keeps the previous dt-stage schedule.
DT_MS = [1, 5, 10, 50, 100]
REFERENCE_TRIALS = 10  # Per site, per batch checkpoint; pre/mid/post.
SCHEDULE_SEED = 42
BATCH_REST_SEC = 300.0
SELECTION = {
    "culture_id": "",  # Must match CONFIG_DIR/system.yaml.
    "recording_mode": "spikes_events",
    "minimum_success_trials": 6,  # Reference policy; does not rewrite Probe scores.
    "units": [
        # Exactly three entries, two electrode IDs each. Example structure:
        # {"source_run": "data/YOUR_PROBE_RUN", "source_unit": "candidate_XX",
        #  "electrodes": [ID_A, ID_B], "exceptions": {ID_B: "Reason if below reference policy"}},
        # Without Probe evidence: {"name": "manual_A", "electrodes": [ID_A, ID_B],
        #                         "selection_reason": "Manual selection basis"},
    ],
}
# End editable section. No default experimental electrodes are selected.


def main():
    import argparse
    import json
    import logging
    from main import load_configuration, execute
    parser = argparse.ArgumentParser(description="正式实验独立入口；默认只预演，不接触设备")
    parser.add_argument("--enable-hardware", action="store_true", help="显式允许连接设备；未指定时仅预演")
    parser.add_argument("--routing-check", action="store_true", help="只检查六电极联合路由，不发刺激、不采集H5")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    print("仅预演，不连接设备" if not args.enable_hardware else
          "仅执行零刺激联合路由检查" if args.routing_check else "执行真实正式采集")
    directory = Path(CONFIG_DIR).expanduser()
    system, stimulation = load_configuration(directory if directory.is_absolute() else ROOT / directory)
    if INTER_SEQUENCE_REST_SEC is not None:
        stimulation["formal"]["trial_interval_sec"] = INTER_SEQUENCE_REST_SEC
    stimulation["formal"]["temporal_interval_mode"] = "after_second"
    if SCHEDULE_MODE=='pair_batches':
        from python.pair_batches import configure_pair_batches
        stimulation=configure_pair_batches(stimulation,dt_ms=DT_MS,reference_trials=REFERENCE_TRIALS,
            seed=SCHEDULE_SEED,rest_between_batches_sec=BATCH_REST_SEC)
    elif SCHEDULE_MODE!='legacy':
        raise ValueError('SCHEDULE_MODE只能为pair_batches或legacy')
    print(f"正式每条Sequence最后一个脉冲结束后静息 {stimulation['formal']['trial_interval_sec']} 秒，随后切换并稳定")
    run = execute(system, stimulation, mode="hardware", formal_selection=Path(__file__),
                  dry_run=not args.enable_hardware, formal_routing_check=args.routing_check)
    plan = json.loads((run / "experiment_plan.json").read_text(encoding="utf-8"))
    for unit in plan["formal_units"]:
        print(f"正式unit {unit['formal_unit']} 来源={unit['source_unit']} 电极={unit['electrodes']}")
        for metric in unit["site_metrics"]:
            print("  ", metric)
    print(f"trial={plan['formal_trial_count']}；名义时长={plan['formal_nominal_duration_sec']/60:.1f}min；结果={run}")
    if plan.get('schedule_mode')=='pair_batches':
        print(f"主体={plan['primary_trial_count']}；漂移参照={plan['reference_trial_count']}；参照与主条件单独标记")
        print(f"每对首末时间跨度(min)：{[round(r['span_sec']/60,1) for r in plan['pair_time_ranges'].values()]}")


if __name__ == "__main__":
    main()
