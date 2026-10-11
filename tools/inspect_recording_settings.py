"""Read only small H5 acquisition settings; never import MaxLab or stimulate."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

import h5py
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.utils.io import select_segment


def read_settings(path, well):
    values = {}
    with h5py.File(path, "r") as handle:
        settings = select_segment(handle, well)["settings"]
        for key in ("sampling", "gain", "hpf", "lsb", "spike_threshold"):
            if key not in settings:
                values[key] = "未保存"
                continue
            data = np.asarray(settings[key][()]).reshape(-1)
            unique = np.unique(data)
            if unique.size <= 8:
                values[key] = str(unique.tolist())
            elif data.dtype.kind in "biuf":
                values[key] = f"{data.size}项，范围[{data.min():g}, {data.max():g}]，不视为单一阈值"
            else:
                values[key] = f"{data.size}项，未展开"
    return values


def inspect_run(run):
    run = Path(run).resolve()
    system = yaml.safe_load((run / "config_snapshot/system.yaml").read_text(encoding="utf-8"))
    pre = run / "pre_experiment"
    baseline = json.loads((pre / "baseline_statistics.json").read_text(encoding="utf-8"))["h5"]
    with (pre / "probe_summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        row = next((r for r in csv.DictReader(handle) if r.get("h5")), None)
    if row is None:
        raise ValueError("此run还没有已完成的Probe H5")
    print("只读检查：不读取raw电压或spikes，不连接设备")
    print("基线H5:", baseline)
    print("Probe H5:", row["h5"])
    print("Probe配置的event_threshold:", system["maxwell"]["event_threshold"])
    a = read_settings(baseline, system["maxwell"]["well"])
    b = read_settings(row["h5"], system["maxwell"]["well"])
    for key in a:
        print(f"{key}: 基线={a[key]} | Probe={b[key]}")
    print("缺失或非单一spike_threshold不能用于确认两份记录的在线检测设置一致。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读比较已有基线与Probe的H5录制设置")
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    inspect_run(args.run)
