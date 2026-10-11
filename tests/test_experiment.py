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
from python.maxwell_setup import MaxLabBackend, ElectrodeUnroutable
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
        cfg["enforce_artifact_qc"] = True  # Optional generic API gate, not this experiment's rule.
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
        self.stimulation["probe"]["enforce_artifact_qc"] = True  # Existing user's YAML remains accepted.
        reporter = Reporter()
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            backend = MockBackend(self.system, reporter)
            backend.route = MagicMock(wraps=backend.route)
            backend.shutdown = MagicMock(wraps=backend.shutdown)
            runner = ExperimentRunner(self.system, self.stimulation, directory, backend, reporter, probe_only=True)
            with patch("python.experiment_runner.unit_map"), patch("python.experiment_runner.raster"), \
                    patch("python.experiment_runner.summary_table"), patch.object(runner, "formal") as formal:
                runner.run()
            formal.assert_not_called()
            backend.shutdown.assert_called_once()
            self.assertEqual(backend.route.call_count, 24)
            self.assertTrue(all(len(call.args[0]) == 1 for call in backend.route.call_args_list))
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
            metrics = list((path / "pre_experiment/02_probe").rglob("probe_metrics.json"))
            self.assertEqual(len(metrics), 24)
            for metric in metrics:
                row = json.loads(metric.read_text())
                self.assertEqual(row["routing_mode"], "single_connect")
                self.assertIsInstance(row["stimulation_unit"], int)
                routing = json.loads((metric.parent / "routing.json").read_text())
                self.assertEqual(len(routing["pool"]), 1)
            self.assertTrue((path / "pre_experiment/selected_formal_sites.yaml").is_file())
            self.assertFalse((path / "raw_data").exists())
            state = json.loads((path / "pre_experiment/workflow_state.json").read_text())
            self.assertEqual(state["stage"], "PROBE_COMPLETE")
            self.assertFalse(runner.stimulation["probe"]["enforce_artifact_qc"])
            self.assertTrue(self.stimulation["probe"]["enforce_artifact_qc"])

    def test_formal_run_uses_handoff_and_never_repeats_probe(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            backend = MagicMock()
            runner = ExperimentRunner(self.system, self.stimulation, directory, backend, Reporter(),
                                      probe_only=False, formal_handoff=(self.units, {"source_probe_run": "reviewed"}))
            runner.initialize = MagicMock()
            runner.prepare_baseline = MagicMock()
            runner.probes = MagicMock()
            def formal():
                runner.result = dict(status="complete")
            runner.formal = MagicMock(side_effect=formal)
            runner.run()
            runner.probes.assert_not_called()
            runner.prepare_baseline.assert_not_called()
            runner.formal.assert_called_once()
            backend.shutdown.assert_called_once()

    def test_automatic_probe_to_formal_path_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            with self.assertRaisesRegex(ValueError, "禁止自动衔接"):
                ExperimentRunner(self.system, self.stimulation, directory, MagicMock(), Reporter(), probe_only=False)

    def test_no_artifact_gate_in_current_probe_definition(self):
        cfg = copy.deepcopy(self.stimulation["probe"])
        cfg.update(threshold_rule="mean_plus_3variance", enforce_artifact_qc=False)
        onsets = np.arange(10, dtype=float) + 1
        times = np.r_[onsets[:7] + .02, onsets + .001]
        session = SimpleNamespace(times=np.sort(times), duration_sec=12)
        trials, summary = evaluate_probe(session, onsets, dict(window_ms=40, mean=0, variance=0, sd=0), cfg)
        self.assertEqual(summary["success_trials"], 7)
        self.assertTrue(summary["eligible"])
        self.assertFalse(summary["artifact_file_valid"])
        self.assertTrue(all(not t["artifact_valid"] for t in trials))

    def test_completed_probe_with_insufficient_units_requires_review(self):
        self.system["mock"]["baseline_duration_override_sec"] = 40
        self.stimulation["probe"]["threshold_rule"] = "mean_plus_3variance"
        reporter = Reporter()
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            backend = MockBackend(self.system, reporter)
            def insufficient(session, onsets, background, cfg):
                trials, summary = evaluate_probe(session, onsets, background, cfg)
                summary["eligible"] = False
                return trials, summary
            runner = ExperimentRunner(self.system, self.stimulation, directory, backend, reporter)
            with patch("python.experiment_runner.unit_map"), patch("python.experiment_runner.raster"), \
                    patch("python.experiment_runner.summary_table"), \
                    patch("python.experiment_runner.evaluate_probe", side_effect=insufficient), \
                    patch.object(runner, "formal") as formal:
                runner.run()
            formal.assert_not_called()
            result = json.loads((Path(directory) / "experiment_result.json").read_text())
            self.assertEqual(result["status"], "probe_review_required")
            self.assertEqual(result["probe_trial_count"], 240)
            self.assertFalse(result["ready_for_formal"])
            self.assertEqual(result["selected_formal_units"], [])
            report = json.loads((Path(directory) / "pre_experiment/probe_unit_summary.json").read_text())
            self.assertEqual(len(report), 6)
            self.assertTrue(all(u["valid_electrode_count"] == 0 for u in report))

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

    def test_multi_electrode_pool_still_rejects_shared_stimulation_unit(self):
        backend = MaxLabBackend(self.system, Reporter())
        backend.mx, backend.array = MagicMock(), MagicMock()
        backend.mx.send.return_value = "Ok"
        backend.array.load_config.return_value = "Ok"
        backend.cfg = Path("unused_mock.cfg")
        backend.mapping = [dict(electrode=11), dict(electrode=12)]
        links = {}
        def connect(electrode):
            links[electrode] = 0
            return "Ok"
        backend.array.query_stimulation_at_electrode.side_effect = lambda e: str(links[e]) if e in links else ""
        backend.array.connect_electrode_to_stimulation.side_effect = connect
        with self.assertRaisesRegex(RuntimeError, "电极11与12共用stim unit 0"):
            backend.route([11, 12])
        backend.array.download.assert_not_called()
        backend.mx.Sequence.assert_not_called()

    def test_only_acknowledged_connect_with_empty_query_is_unroutable(self):
        for query, connect, error in (("", "Ok", ElectrodeUnroutable),
                                      ("ERROR", "Ok", RuntimeError),
                                      ("", "ERROR", RuntimeError),
                                      ("32", "Ok", RuntimeError),
                                      (None, "Ok", RuntimeError)):
            with self.subTest(query=query, connect=connect):
                backend = MaxLabBackend(self.system, Reporter())
                backend.mx, backend.array = MagicMock(), MagicMock()
                backend.cfg, backend.mapping = Path("unused.cfg"), []
                backend.array.load_config.return_value = "Ok"
                backend.array.connect_electrode_to_stimulation.return_value = connect
                backend.array.query_stimulation_at_electrode.return_value = query
                with self.assertRaises(error) as caught:
                    backend.route([13983])
                self.assertIs(type(caught.exception), error)
                backend.array.download.assert_not_called()
                backend.mx.Sequence.assert_not_called()

    def test_unroutable_probe_sites_are_saved_and_remaining_candidates_tested(self):
        self.system["mock"]["baseline_duration_override_sec"] = 40
        self.stimulation["probe"]["threshold_rule"] = "mean_plus_3variance"
        for skip_units in (0, 4):
            with self.subTest(skip_units=skip_units), tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
                reporter = Reporter()
                backend = MockBackend(self.system, reporter)
                runner = ExperimentRunner(self.system, self.stimulation, directory, backend, reporter)
                route = backend.route
                unavailable = set()
                def route_with_missing(electrodes):
                    if not unavailable:
                        unavailable.update(e for u in runner.units[:skip_units] for e in u["electrodes"])
                        if skip_units == 0:
                            unavailable.add(runner.units[0]["center"])
                    if electrodes[0] in unavailable:
                        raise ElectrodeUnroutable(electrodes[0])
                    return route(electrodes)
                backend.route = MagicMock(side_effect=route_with_missing)
                with patch("python.experiment_runner.unit_map"), patch("python.experiment_runner.raster"), \
                        patch("python.experiment_runner.summary_table"), patch.object(runner, "formal") as formal:
                    runner.run()
                formal.assert_not_called()
                path = Path(directory)
                result = json.loads((path / "experiment_result.json").read_text())
                self.assertEqual(result["probe_trial_count"], (24 - len(unavailable)) * 10)
                self.assertEqual(result["status"], "probe_complete" if skip_units == 0 else "probe_review_required")
                self.assertEqual(runner.global_total, result["probe_trial_count"])
                self.assertFalse(result["formal_started"])
                self.assertEqual(backend.route.call_count, 24)
                metrics = list(path.glob("pre_experiment/02_probe/*/*/probe_metrics.json"))
                self.assertEqual(len(metrics), 24)
                for file in metrics:
                    row = json.loads(file.read_text(encoding="utf-8"))
                    if row["electrode"] in unavailable:
                        self.assertEqual(row["status"], "unroutable")
                        self.assertFalse(row["eligible"])
                        self.assertEqual(row["all_trials"], 0)
                        self.assertIsNone(row["response_10_50ms_mean"])
                        self.assertFalse(list(file.parent.glob("*.h5")))
                        self.assertFalse((file.parent / "trial_journal.jsonl").exists())
                    else:
                        self.assertEqual(row["all_trials"], 10)
                        self.assertEqual(row["status"], "complete")
                reports = json.loads((path / "pre_experiment/probe_unit_summary.json").read_text())
                self.assertEqual(len(reports), 6)
                self.assertEqual(sum(len(u["unroutable_electrodes"]) for u in reports), len(unavailable))
                self.assertEqual(sum(len(u["tested_electrodes"]) for u in reports), 24 - len(unavailable))
                self.assertTrue(all(len(u["candidate_electrodes"]) == 4 for u in reports))


if __name__ == "__main__":
    unittest.main()
