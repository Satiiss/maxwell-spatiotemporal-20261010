"""Edit the selection below; use fixed commands to plan, check routing, or acquire."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Edit only this section. Paths inside SELECTION are relative to this file.
CONFIG_DIR = "config"  # Set to your current culture's configuration directory.
INTER_SEQUENCE_REST_SEC = 1.0  # Seconds after the last complete pulse; None keeps the YAML value.
SESSION = "short"  # "short": 5/10 ms; "long": 50/100 ms. One invocation, one session.
TRIALS_PER_CONDITION = 50  # Every single reference repeats the initial single protocol.
REST_AFTER_SYNC_SEC = 300.0
REST_BETWEEN_DT_SEC = 0.0  # >0 automatically inserts a complete post-rest single reference.
FINAL_REST_SEC = 300.0
PRE_SPONTANEOUS_SEC = 600
POST_SPONTANEOUS_SEC = 600
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
    from python.dt_sessions import configure_dt_session
    stimulation["formal"].update(pre_spontaneous_sec=PRE_SPONTANEOUS_SEC,
        post_spontaneous_sec=POST_SPONTANEOUS_SEC)
    stimulation = configure_dt_session(stimulation, SESSION, trials=TRIALS_PER_CONDITION,
        rest_after_sync_sec=REST_AFTER_SYNC_SEC, rest_between_dt_sec=REST_BETWEEN_DT_SEC,
        final_rest_sec=FINAL_REST_SEC)
    print(f"本次仅执行 {SESSION} 时段 dt={stimulation['formal']['dt_ms']} ms；不会自动接续另一个时段")
    print(f"正式每条Sequence最后一个脉冲结束后静息 {stimulation['formal']['trial_interval_sec']} 秒，随后切换并稳定")
    run = execute(system, stimulation, mode="hardware", formal_selection=Path(__file__),
                  dry_run=not args.enable_hardware, formal_routing_check=args.routing_check)
    plan = json.loads((run / "experiment_plan.json").read_text(encoding="utf-8"))
    for unit in plan["formal_units"]:
        print(f"正式unit {unit['formal_unit']} 来源={unit['source_unit']} 电极={unit['electrodes']}")
        for metric in unit["site_metrics"]:
            print("  ", metric)
    print(f"trial={plan['formal_trial_count']}；名义时长={plan['formal_nominal_duration_sec']/60:.1f}min；结果={run}")
    if plan.get('schedule_mode')=='dt_sessions':
        print(f"主体={plan['primary_trial_count']}；漂移参照={plan['reference_trial_count']}；参照与主条件单独标记")
        print(f"每对含同步及两dt的首末时间跨度(min)：{[round(r['span_sec']/60,1) for r in plan['pair_time_ranges'].values()]}")


if __name__ == "__main__":
    main()
