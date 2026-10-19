"""Strict MaxWell H5 time axis and CFG parsing. Never infer t0 from first spike."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import json
import re
import numpy as np
import h5py

CFG_ENTRY = re.compile(r"(\d+)\((\d+)\)([-+\d.eE]+)/([-+\d.eE]+);")
BOUNDARY_IDS = {"start": 2**20 - 2, "end": 2**20 - 1}
BOUNDARY_PREFIX = "recording_boundary_v1"


def parse_cfg(path):
    text = Path(path).read_text(encoding="utf-8", errors="strict")
    entries = {}
    channels = {}
    for match in CFG_ENTRY.finditer(text.split("H4sI", 1)[0]):
        channel, electrode = map(int, match.group(1, 2))
        row = dict(channel=channel, electrode=electrode, x=float(match[3]), y=float(match[4]))
        if not 0 <= channel < 1024 or not np.isfinite([row["x"], row["y"]]).all():
            raise ValueError("CFG含无效通道或坐标")
        if electrode in entries and entries[electrode] != row:
            raise ValueError(f"CFG电极 {electrode} 有冲突映射")
        if channel in channels and channels[channel] != electrode:
            raise ValueError(f"CFG通道 {channel} 绑定多个电极")
        entries[electrode] = row
        channels[channel] = electrode
    if not entries:
        raise ValueError("CFG中没有有效的channel(electrode)x/y;映射")
    return sorted(entries.values(), key=lambda r: r["channel"])


@dataclass
class Session:
    path: Path
    times: np.ndarray
    channels: np.ndarray
    mapping: list[dict]
    sampling_hz: float
    frame_origin: int
    duration_sec: float
    events: list[dict]
    unmapped_spike_count: int
    amplitudes: np.ndarray | None = None
    time_origin_source: str = "raw_frame_numbers"


def _unique_event_properties(pairs):
    properties = {}
    for key, value in pairs:
        if key in properties:
            raise ValueError("事件JSON属性包含重复键")
        properties[key] = value
    return properties


def _event_bounds(events):
    markers = []
    for row in events:
        message = row["eventmessage"]
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="strict")
        message = str(message).strip()
        event_id = int(row["eventid"])
        if message.startswith("{"):
            try:
                properties = json.loads(message, object_pairs_hook=_unique_event_properties)
            except ValueError as exc:
                if BOUNDARY_PREFIX in message or event_id in BOUNDARY_IDS.values():
                    raise ValueError("录制边界事件JSON格式无效") from exc
                continue
            if BOUNDARY_PREFIX not in properties:
                if event_id in BOUNDARY_IDS.values():
                    raise ValueError("录制边界保留ID缺少标记属性")
                continue
            role, token = properties[BOUNDARY_PREFIX], properties.get("token")
        elif message.startswith(BOUNDARY_PREFIX):
            match = re.fullmatch(BOUNDARY_PREFIX + r" (start|end) token ([0-9a-f]{32})", message)
            if not match:
                raise ValueError("录制边界事件格式或ID无效")
            role, token = match[1], match[2]
        elif event_id in BOUNDARY_IDS.values():
            raise ValueError("录制边界保留ID缺少有效标记")
        else:
            continue
        if (not isinstance(role, str) or role not in BOUNDARY_IDS
                or not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None
                or event_id != BOUNDARY_IDS[role]):
            raise ValueError("录制边界事件格式或ID无效")
        markers.append((role, token, int(row["frameno"])))
    if not markers:
        return None
    starts = [r for r in markers if r[0] == "start"]
    ends = [r for r in markers if r[0] == "end"]
    if len(starts) != 1 or len(ends) != 1 or starts[0][1] != ends[0][1] or ends[0][2] <= starts[0][2]:
        raise ValueError("录制必须具有唯一、同token且先后正确的起止帧事件；禁止猜测时间原点")
    return starts[0][2], ends[0][2]


def select_segment(handle, well=0):
    candidates = []
    if "data_store" in handle:
        candidates = [v for v in handle["data_store"].values() if "spikes" in v]
    if not candidates and "recordings" in handle:
        for rec in handle["recordings"].values():
            seg = rec.get(f"well{well:03d}", rec)
            if "spikes" in seg:
                candidates.append(seg)
    if not candidates:
        key = f"wells/well{well:03d}/rec0000"
        if key in handle:
            candidates = [handle[key]]
    candidates = [g for g in candidates if "well_id" not in g or int(np.asarray(g["well_id"]).reshape(-1)[0]) == well]
    if len(candidates) != 1:
        raise ValueError(f"必须明确单段H5和well；检测到 {len(candidates)} 个段")
    return candidates[0]


def read_session(path, cfg_path=None, well=0):
    with h5py.File(path, "r") as handle:
        seg = select_segment(handle, well)
        fs = float(np.asarray(seg["settings/sampling"]).reshape(-1)[0])
        if not np.isfinite(fs) or fs <= 0:
            raise ValueError("H5采样率无效")
        origins, endings = [], []
        for group in seg.get("groups", {}).values():
            if "frame_nos" in group and group["frame_nos"].size:
                origins.append(int(group["frame_nos"][0]))
                endings.append(int(group["frame_nos"][-1]))
        raw_events = seg["events"][:] if "events" in seg else []
        bounds = _event_bounds(raw_events)
        if bounds is not None:
            origin, ending = bounds
            duration = (ending - origin) / fs
            origin_source = "h5_boundary_events"
        elif origins:
            origin = min(origins)
            duration = (max(endings) - origin + 1) / fs
            origin_source = "raw_frame_numbers"
        else:
            raise ValueError("H5缺少明确的录制起始帧或完整边界事件，禁止以首个spike作为时间原点")
        mapping = [dict(channel=int(r["channel"]), electrode=int(r["electrode"]),
                        x=float(r["x"]), y=float(r["y"])) for r in seg["settings/mapping"][:]
                   if 0 <= int(r["channel"]) < 1024 and int(r["electrode"]) >= 0]
        if cfg_path:
            cfg = {r["channel"]: r for r in parse_cfg(cfg_path)}
            for row in mapping:
                expected = cfg.get(row["channel"])
                if expected is None or expected["electrode"] != row["electrode"] or not np.allclose(
                        [row["x"], row["y"]], [expected["x"], expected["y"]], atol=0.1):
                    raise ValueError(f"H5与CFG映射冲突，通道 {row['channel']}；需要人工核对")
        spikes = seg["spikes"][:]
        valid_channels = np.array([r["channel"] for r in mapping])
        valid = np.isin(spikes["channel"], valid_channels)
        if bounds is not None:
            # Boundary markers define an explicit analysis interval, not the file's
            # first acquired sample. Exclude recorder lead-in and writer-flush tail.
            valid &= (spikes["frameno"] >= origin) & (spikes["frameno"] < ending)
        times = (spikes["frameno"][valid].astype(np.int64) - origin) / fs
        channels = spikes["channel"][valid].astype(np.int32)
        amplitudes = spikes["amplitude"][valid].astype(float) if "amplitude" in spikes.dtype.names else None
        order = np.argsort(times, kind="stable")
        events = []
        for row in raw_events:
            message = row["eventmessage"]
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            events.append(dict(event_id=int(row["eventid"]), time_sec=(int(row["frameno"]) - origin) / fs,
                               eventmessage=str(message)))
        return Session(Path(path), times[order], channels[order], mapping, fs, origin, duration,
                       events, int(np.count_nonzero(~np.isin(spikes["channel"], valid_channels))),
                       None if amplitudes is None else amplitudes[order], origin_source)


def read_burst_table(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        intervals = [(float(r["start_sec"]), float(r["end_sec"])) for r in csv.DictReader(handle)]
    if any(not np.isfinite([a, b]).all() or a < 0 or b <= a for a, b in intervals):
        raise ValueError("burst表含无效区间")
    return sorted(intervals)
