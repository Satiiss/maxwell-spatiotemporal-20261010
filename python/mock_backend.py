"""Deterministic synthetic H5; virtual time, no SDK import or network calls."""
from __future__ import annotations

import time
import uuid
from pathlib import Path
import numpy as np
import h5py
from analysis.utils.io import BOUNDARY_IDS, BOUNDARY_PREFIX

SPIKE_DTYPE = np.dtype([("frameno", "<i8"), ("channel", "<i4"), ("amplitude", "<f4")])
MAP_DTYPE = np.dtype([("channel", "<i4"), ("electrode", "<i4"), ("x", "<f8"), ("y", "<f8")])
EVENT_DTYPE = np.dtype([("frameno", "<i8"), ("eventtype", "<u4"), ("eventid", "<u4"), ("eventmessage", "S512")])


def synthetic_mapping():
    rows, centers = [], []
    for i in range(6):
        cx, cy = 20 + 12 * i, 30
        for j, (dx, dy) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1))):
            ele = 220 * (cy + dy) + cx + dx
            if j == 0:
                centers.append(ele)
            rows.append(dict(channel=len(rows), electrode=ele, x=(cx + dx) * 17.5, y=(cy + dy) * 17.5))
    for col in range(80):
        rows.append(dict(channel=len(rows), electrode=220 * 60 + col, x=col * 17.5, y=60 * 17.5))
    return rows, centers


class MockBackend:
    realtime = False
    lsb_mv = 2.98

    def __init__(self, system, reporter):
        self.system, self.reporter = system, reporter
        self.mapping, self.centers = synthetic_mapping()
        self.rng = np.random.default_rng(system["mock"]["seed"])
        self.clock = 0.0
        self.recording = False
        self.active = {}

    def initialize(self, cfg):
        if not cfg.exists():
            cfg.write_text("".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in self.mapping), encoding="utf-8")

    def route(self, electrodes):
        self.pool = {e: i for i, e in enumerate(electrodes)}
        self.active = {}
        return self.pool

    def switch(self, electrodes):
        self.active = {e: i for i, e in enumerate(electrodes)}

    def start_segment(self, directory, basename):
        self.directory, self.basename = Path(directory), basename
        self.record_start = self.clock
        self.record_epoch = time.time()
        self.spikes, self.events = [], []
        self.recording = True
        return self.record_epoch

    def prepare_sequence(self, condition, commands, event_id, protocol):
        return condition, commands, event_id, protocol

    def stimulate(self, sequence):
        condition, commands, first_id, protocol = sequence
        t = self.clock - self.record_start + protocol["sequence_initial_delay_samples"] * 50e-6
        onsets = [t, t + condition.dt_ms / 1000] if condition.kind == "temporal" else [t]
        ids = [first_id, first_id + 1] if condition.kind == "temporal" else [first_id]
        for onset, event_id in zip(onsets, ids):
            self.events.append((onset, event_id, f"condition {condition.name} type {condition.kind}"))
            # Population artifact spread across 104 channels; not claimed to model biology.
            channels = self.rng.choice([r["channel"] for r in self.mapping], size=440)
            for s, channel in zip(onset + self.rng.uniform(0.0001, 0.0019, 440), channels):
                self.spikes.append((s, int(channel), -500.0))
            strength = 80 + condition.electrodes[0] % 40
            channels = self.rng.choice([r["channel"] for r in self.mapping], size=strength)
            for s, channel in zip(onset + self.rng.uniform(0.0101, 0.045, strength), channels):
                self.spikes.append((s, int(channel), -100.0))
        return time.time(), onsets

    def advance(self, seconds):
        self.reporter.check()
        self.clock += max(0, seconds)

    def finish_segment(self):
        self.reporter.check()
        duration = max(0.1, self.clock - self.record_start)
        total = int(duration * len(self.mapping) * 0.12)
        bg = self.rng.uniform(0, duration, total)
        bg_ch = self.rng.choice([r["channel"] for r in self.mapping], total)
        self.spikes.extend((float(t), int(c), -50.0) for t, c in zip(bg, bg_ch))
        fs, origin = 20000, 98765432
        with h5py.File(self.directory / (self.basename + ".raw.h5"), "w") as handle:
            handle.attrs["synthetic"] = True
            handle.attrs["mock_warning"] = "Synthetic spikes and sparse frame coordinates; no raw voltage."
            seg = handle.create_group("data_store/data0000")
            seg.create_dataset("well_id", data=[self.system["maxwell"]["well"]])
            seg.create_dataset("start_time", data=[round(self.record_epoch * 1000)])
            seg.create_dataset("stop_time", data=[round((self.record_epoch + duration) * 1000)])
            seg.create_dataset("settings/sampling", data=[fs])
            seg.create_dataset("settings/gain", data=[self.system["maxwell"]["amplifier_gain"]])
            seg.create_dataset("settings/spike_threshold", data=[self.system["maxwell"]["event_threshold"]])
            seg.create_dataset("settings/mapping", data=np.array([tuple(r[k] for k in MAP_DTYPE.names) for r in self.mapping], dtype=MAP_DTYPE))
            data = np.array([(origin + round(t * fs), c, a) for t, c, a in self.spikes], dtype=SPIKE_DTYPE)
            data.sort(order="frameno")
            seg.create_dataset("spikes", data=data, compression="gzip")
            events = np.array([(origin + round(t * fs), 1, e, msg.encode()) for t, e, msg in self.events], dtype=EVENT_DTYPE)
            seg.create_dataset("events", data=events)
            if self.system["maxwell"]["raw_recording"]:
                seg.create_dataset("groups/exp/frame_nos", data=[origin, origin + round(duration * fs) - 1])
                seg.create_dataset("groups/exp/channels", data=[r["channel"] for r in self.mapping])
            else:
                token = uuid.uuid4().hex
                markers = np.array([(origin + frame, 1, BOUNDARY_IDS[role],
                    f"{BOUNDARY_PREFIX} {role} token {token}".encode())
                    for role, frame in (("start", 0), ("end", round(duration * fs)))], dtype=EVENT_DTYPE)
                del seg["events"]
                seg.create_dataset("events", data=np.concatenate([markers[:1], events, markers[1:]]))
        self.recording = False
        return self.directory / (self.basename + ".raw.h5")

    def abort_segment(self):
        # Do not call a stop-aware finish after an interruption; preserve incomplete status.
        self.recording = False

    def shutdown(self):
        self.active = {}
