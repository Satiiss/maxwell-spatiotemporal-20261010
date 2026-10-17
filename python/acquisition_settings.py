"""Read-only acquisition checks before any hardware Probe initialization."""
from __future__ import annotations

import logging
import math

import h5py
import numpy as np

from analysis.utils.io import select_segment, parse_cfg


def validate_baseline_acquisition(system):
    """Compare saved baseline detection settings with the proposed Probe config.

    No baseline input means the runner will acquire a new baseline itself.
    This check reads only small settings datasets, never raw data or spikes.
    """
    path = system["manual_inputs"].get("baseline_h5")
    if not path:
        return
    fields = {"spike_threshold": "event_threshold", "gain": "amplifier_gain"}
    with h5py.File(path, "r") as handle:
        settings = select_segment(handle, system["maxwell"]["well"])["settings"]
        for saved_key, config_key in fields.items():
            if saved_key not in settings:
                raise ValueError(f"基线H5未保存{saved_key}，无法确认Probe计数与基线设置一致")
            data = settings[saved_key]
            if not 0 < data.size <= 1024:
                raise ValueError(f"基线H5的{saved_key}不是可核对的小型设置")
            values = np.asarray(data[()], dtype=float).reshape(-1)
            expected = float(system["maxwell"][config_key])
            if not np.isfinite(values).all() or not math.isfinite(expected):
                raise ValueError(f"基线或Probe的{saved_key}含无效数值")
            unique = np.unique(values)
            if unique.size != 1:
                raise ValueError(f"基线H5的{saved_key}不是单一值，需要核对检测设置")
            actual = float(unique[0])
            if not math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6):
                raise ValueError(
                    f"基线H5 {saved_key}={actual:g}，Probe配置maxwell.{config_key}={expected:g}；"
                    "两份spike计数不能直接比较，尚未初始化设备或发送刺激。"
                    "请统一采集设置；若保留现有基线，将Probe配置改为基线值")
            logging.info("基线/Probe采集设置核对通过：%s=%g", saved_key, actual)
        cfg_path = system["electrode_map"].get("cfg_path")
        if cfg_path:
            cfg = {row["channel"]: row for row in parse_cfg(cfg_path)}
            saved = [dict(channel=int(r["channel"]), electrode=int(r["electrode"]),
                          x=float(r["x"]), y=float(r["y"])) for r in settings["mapping"][:]
                     if 0 <= int(r["channel"]) < 1024 and int(r["electrode"]) >= 0]
            if not saved or len(saved) != len(cfg) or len({r["channel"] for r in saved}) != len(saved):
                raise ValueError("基线H5与CFG的有效记录映射数量不一致")
            for row in saved:
                expected = cfg.get(row["channel"])
                if (expected is None or row["electrode"] != expected["electrode"] or not np.allclose(
                        [row["x"], row["y"]], [expected["x"], expected["y"]], atol=.1, rtol=0)):
                    raise ValueError(f"基线H5与CFG映射冲突，通道{row['channel']}；尚未连接设备")
            logging.info("基线/CFG映射核对通过：%d个记录通道", len(saved))
