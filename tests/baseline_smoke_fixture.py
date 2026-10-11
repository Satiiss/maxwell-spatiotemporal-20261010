"""Create a small explicitly synthetic H5/CFG for an offline CLI smoke check.

Not a culture model or proof of scientific validity. No hardware APIs are used.
"""
from pathlib import Path
import csv
import hashlib
import h5py
import numpy as np


def create_fixture(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    mapping, spikes = [], []
    starts = np.arange(2, 120, 2, dtype=float)
    rng = np.random.default_rng(12345)
    for unit in range(6):
        for site, (dx, dy) in enumerate([(0, 0), (17.5, 0), (0, 17.5), (17.5, 17.5)]):
            ch, electrode = unit*4+site, 100+unit*4+site
            mapping.append((ch, electrode, (unit % 3)*150+dx, (unit//3)*150+dy))
            for index, start in enumerate(starts):
                # Eight off-peak bursts create singleton histogram background bins.
                latency = .005+site*.012+unit*.001
                if index < 8:
                    latency += (index+1)*.002*(site+1)
                for offset in [0, .001, .002, .003, .004]:
                    time = start+latency+offset
                    spikes.append((50000+round(time*20000), ch, -float(60+10*site+rng.normal())))
    mapping = np.array(mapping, dtype=[("channel", "i4"), ("electrode", "i4"), ("x", "f4"), ("y", "f4")])
    spikes = np.array(spikes, dtype=[("frameno", "i8"), ("channel", "i4"), ("amplitude", "f4")])
    # Intentionally store unsorted spikes to check the public reader's aligned sort.
    rng.shuffle(spikes)
    with h5py.File(output/"synthetic.raw.h5", "w") as handle:
        handle.attrs["synthetic"] = True
        seg = handle.create_group("data_store/data0000")
        seg.create_dataset("well_id", data=[0])
        seg.create_dataset("settings/sampling", data=[20000.])
        seg.create_dataset("settings/mapping", data=mapping)
        seg.create_dataset("groups/routed/frame_nos", data=[50000, 12049999])
        seg.create_dataset("spikes", data=spikes)
    cfg = "".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in mapping)
    (output/"synthetic.cfg").write_text(cfg, encoding="utf-8")
    with (output/"reviewed_bursts.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["start_sec", "end_sec"])
        writer.writerows((start, start+.15) for start in starts)
    (output/"input_hashes.txt").write_text("\n".join(
        f"{name} {hashlib.sha256((output/name).read_bytes()).hexdigest()}"
        for name in ["synthetic.raw.h5", "synthetic.cfg"]), encoding="utf-8")
    return output


if __name__ == "__main__":
    create_fixture(Path(__file__).resolve().parents[1]/"data/baseline_smoke_fixture")
