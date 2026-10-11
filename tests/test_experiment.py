"""Offline software checks, without hardware or biological-pipeline validation."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import numpy as np
import h5py

from main import load_configuration, make_plan, ROOT, validate
from python.schedule import formal_conditions, round_robin, Condition, waveform_commands, next_onset_interval
from python.site_selection import build_local_units
from python.mock_backend import synthetic_mapping
from python.maxwell_setup import MaxLabBackend
from python.utils.progress import Reporter, ExperimentStopped
from python.utils.time_log import SegmentStimLog
from analysis.utils.io import read_session
from analysis.utils.probe import evaluate_probe, rank_units, baseline_distribution
from python.experiment_runner import ExperimentRunner
from python.mock_backend import MockBackend


class SoftwareChecks(unittest.TestCase):
    def setUp(self):
        self.system, self.stimulation = load_configuration(ROOT / "config")
        self.units = [dict(electrodes=[i * 10, i * 10 + 1]) for i in range(1, 4)]
        (ROOT / "data").mkdir(exist_ok=True)

    def test_full_protocol_counts_and_interleaving(self):
        stages = formal_conditions(self.units, self.stimulation["formal"]["dt_ms"])
        schedule = [list(round_robin(conditions, 50)) for _, conditions in stages]
        self.assertEqual(sum(map(len, schedule)), 5400)
        self.assertEqual(sum(1 if c.kind != "temporal" else 2 for stage in schedule for _, c in stage), 10200)
        self.assertEqual([c.name for _, c in schedule[0][:6]], [c.name for _, c in schedule[0][6:12]])
        self.assertTrue(all(r == 1 for r, _ in schedule[0][:6]))
        self.assertTrue(all(r == 2 for r, _ in schedule[0][6:12]))
        for _, conditions in stages[2:]:
            pairs = {c.electrodes for c in conditions}
            self.assertEqual(len(pairs), 12)
            self.assertTrue(all((b, a) in pairs for a, b in pairs))

    def test_negative_first_and_exact_temporal_delays(self):
        for dt in self.stimulation["formal"]["dt_ms"]:
            c = Condition("test", "temporal", (1, 2), ("a", "b"), dt)
            commands = waveform_commands(c, self.stimulation["protocol"], 2.98, 1)
            elapsed = 0
            onsets = []
            voltages = []
            for opcode, args in commands:
                if opcode == "event":
                    onsets.append(elapsed)
                elif opcode == "delay":
                    elapsed += args[0] * 50
                elif opcode == "dac":
                    voltages.append((512 - args[1]) * 2.98)
            self.assertEqual(onsets[1] - onsets[0], dt * 1000)
            self.assertLess(voltages[0], 0)
            self.assertGreater(voltages[1], 0)
            self.assertEqual(voltages[2], 0)

    def test_synchronous_dac_has_one_event_and_atomic_update(self):
        c = Condition("sync", "synchronous", (1, 2), ("a", "b"))
        cmds = waveform_commands(c, self.stimulation["protocol"], 2.98, 1)
        self.assertEqual(sum(op == "event" for op, _ in cmds), 1)
        self.assertTrue(all(args[0] == 3 and args[1] == args[2] for op, args in cmds if op == "dac"))

    def test_onset_spacing_rejects_dt_one_second_overlap(self):
        cfg = copy.deepcopy(self.stimulation["formal"])
        condition = Condition("dt1000", "temporal", (1, 2), ("a", "b"), 1000)
        self.assertAlmostEqual(next_onset_interval(condition, cfg, .0006), 2.0006)
        cfg["temporal_interval_mode"] = "onset_to_onset"
        with self.assertRaises(ValueError):
            next_onset_interval(condition, cfg, .0006)

    def test_one_second_rest_starts_after_second_pulse_end(self):
        cfg = self.stimulation["formal"]
        for dt in cfg["dt_ms"]:
            condition = Condition("pair", "temporal", (1, 2), ("a", "b"), dt)
            period = next_onset_interval(condition, cfg, .0006)
            second_pulse_end = dt / 1000 + .0006
            self.assertAlmostEqual(period - second_pulse_end, 1.0)

    def test_strict_probe_requires_seven_of_ten(self):
        cfg = copy.deepcopy(self.stimulation["probe"])
        cfg["threshold_rule"] = "mean_plus_3sd"
        stimulus = np.arange(10, dtype=float) + 1
        background = dict(window_ms=40, mean=0, sd=0, variance=0)
        for count, expected in ((6, False), (7, True)):
            times = []
            for i, t in enumerate(stimulus):
                times.extend(t + np.linspace(.0001, .0029, 301))
                if i < count:
                    times.append(t + .020)
            session = SimpleNamespace(times=np.sort(times), duration_sec=20)
            _, summary = evaluate_probe(session, stimulus, background, cfg)
            self.assertEqual(summary["eligible"], expected)
            self.assertEqual(summary["success_trials"], count)

    def test_artifact_count_300_is_not_above_300(self):
        cfg = copy.deepcopy(self.stimulation["probe"])
        cfg["threshold_rule"] = "mean_plus_3sd"
        times = np.r_[1 + np.linspace(.0001, .0029, 300), 1.02]
        session = SimpleNamespace(times=times, duration_sec=3)
        _, result = evaluate_probe(session, [1], dict(window_ms=40, mean=0, sd=0, variance=0), cfg)
        self.assertFalse(result["eligible"])

    def test_unit_count_takes_priority_and_insufficient_units_stop(self):
        rows = []
        for unit, n, prob in (("A", 4, .7), ("B", 3, 1), ("C", 2, .9), ("D", 2, .8)):
            for i in range(n):
                rows.append(dict(unit_id=unit, electrode=len(rows), eligible=True,
                                 success_fraction=prob, response_10_50ms_mean=20))
        selected = rank_units(rows)
        self.assertEqual([u["source_unit"] for u in selected], ["A", "B", "C"])
        with self.assertRaises(ValueError):
            rank_units([r for r in rows if r["unit_id"] in ("A", "B")])

    def test_physical_nearest_neighbours_and_spacing(self):
        mapping, centers = synthetic_mapping()
        units = build_local_units(mapping, centers, 52.5)
        self.assertEqual(len(units), 6)
        self.assertTrue(all(len(u["electrodes"]) == 4 for u in units))
        with self.assertRaises(ValueError):
            build_local_units(mapping, [centers[0], mapping[1]["electrode"], *centers[2:]], 52.5)

    def test_baseline_excludes_whole_windows_overlapping_bursts(self):
        background = baseline_distribution([.1, .15, .21], 2.0, [(.08, .2)], 40)
        starts = background["starts"]
        self.assertFalse(np.any((starts < .2) & (starts + .04 > .08)))

    def test_h5_does_not_use_first_spike_as_origin(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as d:
            path = Path(d) / "input.raw.h5"
            with h5py.File(path, "w") as f:
                seg = f.create_group("data_store/data0000")
                seg.create_dataset("well_id", data=[0])
                seg.create_dataset("settings/sampling", data=[20000])
                seg.create_dataset("settings/mapping", data=np.array([(0, 42, 0., 0.)],
                    dtype=[("channel", "i4"), ("electrode", "i4"), ("x", "f8"), ("y", "f8")]))
                seg.create_dataset("spikes", data=np.array([(30000, 0, -10.)],
                    dtype=[("frameno", "i8"), ("channel", "i4"), ("amplitude", "f4")]))
                seg.create_dataset("groups/exp/frame_nos", data=[10000, 49999])
            session = read_session(path)
            self.assertEqual(session.times[0], 1)
            self.assertEqual(session.duration_sec, 2)
            with h5py.File(path, "a") as f:
                del f["data_store/data0000/groups/exp/frame_nos"]
            with self.assertRaises(ValueError):
                read_session(path)

    def test_fast_switch_never_touches_inactive_pool_units(self):
        backend = MaxLabBackend(self.system, Reporter())
        mx = MagicMock()
        mx.send.return_value = "Ok"
        backend.mx = mx
        backend.pool = {11: 1, 12: 2, 13: 3, 14: 4}
        backend.active = {11: 0}
        backend.switch([12])
        touched = [call.args[0] for call in mx.StimulationUnit.call_args_list]
        self.assertEqual(touched, [1, 2])
        mx.StimulationUnit.reset_mock()
        backend.switch([12])
        self.assertEqual(mx.StimulationUnit.call_count, 0)

    def test_missing_events_are_preserved_and_fail_gate(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as d:
            log = SegmentStimLog(d, "block", "02_stim", "test", 100)
            log.add_trial(dict(trial_id=1, condition="single", electrodes=[42], kind="single",
                               dt_ms=0, event_ids=[1], estimated_onsets_sec=[.5]))
            session = SimpleNamespace(events=[], path=Path(d) / "test.raw.h5", sampling_hz=20000, frame_origin=1000)
            with self.assertRaises(ValueError):
                log.finalize(session, 1)
            self.assertTrue((Path(d) / "trial_journal.jsonl").is_file())
            self.assertIn("HOST_ESTIMATE_UNVERIFIED", (Path(d) / "stim_times.txt").read_text())

    def test_stop_remains_actionable_and_waveform_bounds_are_checked(self):
        reporter = Reporter()
        reporter.stop_event.set()
        with self.assertRaises(ExperimentStopped):
            reporter.check()
        condition = Condition("single", "single", (1,), ("a",))
        with self.assertRaises(ValueError):
            waveform_commands(condition, self.stimulation["protocol"], .1, 1)

    def test_blank_field_config_can_generate_complete_plan(self):
        validate(self.system, self.stimulation)
        plan = make_plan(self.system, self.stimulation)
        self.assertEqual(plan["formal_trial_count"], 5400)
        self.assertIsNone(self.system["electrode_map"]["cfg_path"])
        self.assertIsNone(self.stimulation["probe"]["threshold_rule"])

    def test_probe_only_plan_has_no_formal_schedule(self):
        plan = make_plan(self.system, self.stimulation, probe_only=True)
        self.assertEqual(plan["execution_scope"], "probe_only")
        self.assertEqual(plan["formal_trial_count"], 0)
        self.assertEqual(plan["stages"], [])
        self.assertEqual(plan["probe_trial_count"], 240)
        self.stimulation["formal"]["temporal_interval_mode"] = "onset_to_onset"
        validate(self.system, self.stimulation, probe_only=True)
        with self.assertRaises(ValueError):
            validate(self.system, self.stimulation)

    def test_probe_only_records_all_sites_without_formal_stimulation(self):
        self.system["mock"]["baseline_duration_override_sec"] = 40
        self.stimulation["probe"]["threshold_rule"] = "mean_plus_3variance"
        reporter = Reporter()
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            backend = MockBackend(self.system, reporter)
            backend.shutdown = MagicMock(wraps=backend.shutdown)
            runner = ExperimentRunner(self.system, self.stimulation, directory, backend, reporter, probe_only=True)
            with patch("python.experiment_runner.unit_map"), patch("python.experiment_runner.raster"), \
                    patch("python.experiment_runner.summary_table"), patch.object(runner, "formal") as formal:
                runner.run()
            formal.assert_not_called()
            backend.shutdown.assert_called_once()
            self.assertFalse(backend.recording)
            self.assertEqual(backend.active, {})
            path = Path(directory)
            result = json.loads((path / "experiment_result.json").read_text())
            self.assertEqual(result["status"], "probe_complete")
            self.assertEqual(result["probe_trial_count"], 240)
            self.assertEqual(result["formal_trial_count"], 0)
            self.assertFalse(result["formal_started"])
            self.assertEqual(runner.global_total, 240)
            self.assertEqual(len(list((path / "pre_experiment/02_probe").rglob("*.raw.h5"))), 24)
            self.assertTrue((path / "pre_experiment/selected_formal_sites.yaml").is_file())
            self.assertFalse((path / "raw_data").exists())
            state = json.loads((path / "pre_experiment/workflow_state.json").read_text())
            self.assertEqual(state["stage"], "PROBE_COMPLETE")

    def test_full_run_still_continues_after_probe(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            backend = MagicMock()
            runner = ExperimentRunner(self.system, self.stimulation, directory, backend, Reporter())
            runner.initialize = MagicMock()
            runner.prepare_baseline = MagicMock()
            runner.probes = MagicMock()
            def formal():
                runner.result = dict(status="complete")
            runner.formal = MagicMock(side_effect=formal)
            runner.run()
            runner.probes.assert_called_once()
            runner.formal.assert_called_once()
            backend.shutdown.assert_called_once()

    def test_probe_only_failure_still_shuts_down_without_formal(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            backend = MagicMock()
            runner = ExperimentRunner(self.system, self.stimulation, directory, backend, Reporter(), probe_only=True)
            runner.initialize = MagicMock()
            runner.prepare_baseline = MagicMock()
            runner.probes = MagicMock(side_effect=ValueError("insufficient units"))
            runner.formal = MagicMock()
            with self.assertLogs(level="ERROR"), self.assertRaises(ValueError):
                runner.run()
            runner.formal.assert_not_called()
            backend.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
