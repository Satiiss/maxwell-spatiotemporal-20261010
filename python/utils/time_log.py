from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .persistence import atomic_json, write_csv, utc_now


class ExternalTimeLog:
    def __init__(self, run_dir, time_origin_epoch):
        self.path = Path(run_dir) / "external_time_table.csv"
        self.origin = time_origin_epoch
        self.fields = ["event_type", "block", "phase", "segment_name", "wall_time_utc", "wall_time_local",
                       "epoch_sec", "offset_sec", "trial_id", "electrodes", "event_ids", "note"]
        self.handle = self.path.open("w", encoding="utf-8-sig", newline="")
        self.writer = csv.DictWriter(self.handle, fieldnames=self.fields)
        self.writer.writeheader()

    def add(self, event_type, epoch_sec=None, **extra):
        epoch = time.time() if epoch_sec is None else epoch_sec
        row = dict(event_type=event_type, epoch_sec=epoch, offset_sec=epoch - self.origin,
                   wall_time_utc=datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                   wall_time_local=datetime.fromtimestamp(epoch).astimezone().isoformat())
        row.update(extra)
        self.writer.writerow({k: row.get(k, "") for k in self.fields})
        self.handle.flush()

    def close(self):
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()


class SegmentStimLog:
    """Append durable command journal, then export canonical H5 event times after close."""
    def __init__(self, phase_dir, block, phase, segment_name, record_start_epoch):
        self.phase_dir = Path(phase_dir)
        self.block, self.phase, self.segment_name = block, phase, segment_name
        self.origin = record_start_epoch
        self.rows = []
        self.journal = self.phase_dir / "trial_journal.jsonl"

    def add_trial(self, row):
        self.rows.append(dict(row))
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def finalize(self, session, expected_rows, allow_estimated=False):
        events = {}
        for event in session.events:
            events.setdefault(event["event_id"], []).append(event["time_sec"])
        pulse_rows = []
        errors = []
        for row in self.rows:
            onset_times = []
            for index, event_id in enumerate(row["event_ids"], 1):
                values = events.get(event_id, [])
                if len(values) != 1:
                    errors.append(f"event_id={event_id}存在{len(values)}个事件，预期1个")
                    t = row["estimated_onsets_sec"][index - 1]
                    source = "host_estimate_unverified"
                else:
                    t = float(values[0])
                    source = "h5_events"
                estimate = row["estimated_onsets_sec"][index - 1]
                if abs(t - estimate) > 0.05:
                    logging.warning("段 %s event %s: H5与主机估计差 %.6fs", self.segment_name, event_id, t - estimate)
                onset_times.append(t)
                pulse_rows.append(dict(trial_id=row["trial_id"], condition=row["condition"], event_id=event_id,
                                       pulse_index=index, time_sec=t, timing_source=source,
                                       electrodes=json.dumps(row["electrodes"]),
                                       dt_ms=row["dt_ms"], kind=row["kind"]))
            row["onsets_sec"] = onset_times
            row["response_reference_sec"] = onset_times[-1]
            row["timing_source"] = "h5_events" if all(len(events.get(e, [])) == 1 for e in row["event_ids"]) else "host_estimate_unverified"
            if row["kind"] == "temporal" and row["timing_source"] == "h5_events":
                actual = 1000 * (onset_times[1] - onset_times[0])
                if abs(actual - row["dt_ms"]) > max(0.05, 1000 / session.sampling_hz) + 1e-6:
                    errors.append(f"trial {row['trial_id']}: 实际dt={actual}ms，要求{row['dt_ms']}ms")
        if len(self.rows) != expected_rows:
            errors.append(f"trial数 {len(self.rows)} 与预期 {expected_rows} 不符")
        flat_rows = [dict(r, electrodes=json.dumps(r["electrodes"]), event_ids=json.dumps(r["event_ids"]),
                          estimated_onsets_sec=json.dumps(r["estimated_onsets_sec"]), onsets_sec=json.dumps(r["onsets_sec"]))
                     for r in self.rows]
        write_csv(self.phase_dir / "trial_manifest.csv", flat_rows)
        write_csv(self.phase_dir / "stim_events.csv", pulse_rows)
        times = sorted(row["time_sec"] for row in pulse_rows)
        path = self.phase_dir / "stim_times.txt"
        with path.open("w", encoding="utf-8") as handle:
            handle.write(f"# block: {self.block}\n# phase: {self.phase}\n# segment_name: {self.segment_name}\n")
            handle.write(f"# record_start_epoch: {self.origin}\n# pulse_count: {len(times)}\n# generated_utc: {utc_now()}\n")
            handle.write(f"# time_origin_source: {getattr(session, 'time_origin_source', 'raw_frame_numbers')}\n")
            handle.write("# source: " + ("HOST_ESTIMATE_UNVERIFIED" if errors else "H5_EVENTS") + "\n")
            for t in times:
                handle.write(f"{t:.9f}\n")
        atomic_json(self.phase_dir / "segment_time_meta.json", dict(block=self.block, phase=self.phase,
                    segment_name=self.segment_name, h5_basename=session.path.name, record_start_epoch=self.origin,
                    sampling_hz=session.sampling_hz, frame_origin=session.frame_origin, stim_times_sec=times,
                    time_origin_source=getattr(session, "time_origin_source", "raw_frame_numbers"),
                    analysis_duration_sec=getattr(session, "duration_sec", None),
                    pulse_count=len(times), errors=errors, closed=True, verified=not errors))
        if errors and not allow_estimated:
            raise ValueError("刺激事件校验失败：" + "; ".join(errors[:10]))
        return self.rows

    def save_incomplete(self, reason):
        atomic_json(self.phase_dir / "incomplete_segment.json", dict(reason=reason, closed=False,
                    verified=False, segment_name=self.segment_name, journal=str(self.journal),
                    completed_commands=len(self.rows), generated_utc=utc_now()))
