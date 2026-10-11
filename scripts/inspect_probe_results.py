"""Read saved Probe metrics/spikes for diagnosis; never load MaxLab or write inputs."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.utils.io import read_session
from analysis.utils.probe import count_windows
from scripts.inspect_recording_settings import read_settings


def read_trials(row):
    path = row.get("trial_metrics_path") or str(Path(row["h5"]).parent / "trial_metrics.csv")
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def inspect_run(run, electrodes):
    run = Path(run).resolve()
    pre = run / "pre_experiment"
    system = yaml.safe_load((run / "config_snapshot/system.yaml").read_text(encoding="utf-8"))
    probe = yaml.safe_load((run / "config_snapshot/stimulation.yaml").read_text(encoding="utf-8"))["probe"]
    stats = json.loads((pre / "baseline_statistics.json").read_text(encoding="utf-8"))
    with (pre / "probe_summary.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    completed = [r for r in rows if r.get("status") == "complete" and r.get("h5")]
    print("只读检查：读取保存的metrics、spikes和小型settings，不读取raw电压、不连接设备")
    print("Probe配置：检测阈值=%s，尾部等待=%ss，顺序=%s" % (
        system["maxwell"]["event_threshold"], probe.get("recording_tail_sec", "未保存"),
        probe.get("order", "sequential")))
    print("背景：mean=%.4f，variance=%.4f，windows=%s，burst_count=%s；判据=%s" % (
        stats["mean"], stats["variance"], stats["windows"], stats.get("burst_count"), stats["threshold_rule"]))
    last_zero = []
    for row in completed:
        trials = read_trials(row)
        if trials and int(trials[-1]["response_10_50ms"]) == 0:
            last_zero.append(int(row["electrode"]))
    print(f"末次10–50ms计数为零的位点：{len(last_zero)}/{len(completed)}；电极={last_zero}")
    print("基线H5实际spike_threshold:", read_settings(stats["h5"], system["maxwell"]["well"])["spike_threshold"])
    for electrode in electrodes:
        row = next((r for r in rows if int(r["electrode"]) == electrode), None)
        if row is None or not row.get("h5"):
            print(f"\n电极{electrode}：无已保存H5，跳过")
            continue
        session = read_session(row["h5"], well=system["maxwell"]["well"])
        trials = read_trials(row)
        if not trials:
            print(f"\n电极{electrode}：无已保存trial，跳过")
            continue
        times = [float(t["stim_time_sec"]) for t in trials]
        before = count_windows(session.times, [t - .050 for t in times], .040).tolist()
        after = count_windows(session.times, [t + .010 for t in times], .040).tolist()
        stored = [int(t["response_10_50ms"]) for t in trials]
        last_spike = ((float(session.times[-1]) - times[-1]) * 1000) if session.times.size else None
        actual = read_settings(row["h5"], system["maxwell"]["well"])["spike_threshold"]
        print(f"\n电极{electrode}：实际检测阈值={actual}；响应阈值={row['response_threshold']}")
        print("刺激前−50至−10ms:", before)
        if min(times) < .050:
            print("注意：首次刺激前窗口未完整包含在录制范围内，该次前窗口计数不能直接比较")
        print("刺激后10至50ms:", after, "；与保存指标一致:", after == stored)
        print("最后刺激至raw结束: %.1fms；最后spike相对最后刺激: %sms" % (
            (session.duration_sec - times[-1]) * 1000,
            "无spikes" if last_spike is None else f"{last_spike:.1f}"))
    print("\n最后spike时间只用于排查尾部；晚于50ms不保证检测无遗漏，计数为零也不单独证明文件缺失。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读查看Probe逐次响应与末次记录，不进行刺激")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--electrodes", type=int, nargs="+", default=[23209, 23205, 24985, 13075, 13953, 19271])
    args = parser.parse_args()
    inspect_run(args.run, args.electrodes)
