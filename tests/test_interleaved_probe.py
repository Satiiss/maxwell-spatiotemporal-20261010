"""Interleaved Probe cadence and hardware-routing gates, using synthetic data."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import h5py

from main import ROOT, load_configuration
from analysis.utils.io import read_session
from python.experiment_runner import ExperimentRunner
from python.maxwell_setup import StimPoolConflict
from python.mock_backend import MockBackend
from python.utils.progress import Reporter
from python.utils.persistence import sha256
from scripts.inspect_recording_settings import read_settings


class InterleavedChecks(unittest.TestCase):
    def setUp(self):
        self.system, self.stimulation = load_configuration(ROOT / "config")
        self.system["mock"]["baseline_duration_override_sec"] = 40
        self.stimulation["probe"].update(order="unit_interleaved", threshold_rule="mean_plus_3variance")
        (ROOT / "data").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.reporter = Reporter()
        self.backend = MockBackend(self.system, self.reporter)
        self.backend.stimulate = MagicMock(wraps=self.backend.stimulate)
        self.backend.start_segment = MagicMock(wraps=self.backend.start_segment)
        self.backend.shutdown = MagicMock(wraps=self.backend.shutdown)

    def run_checked(self, runner):
        with patch("python.experiment_runner.unit_map"), patch("python.experiment_runner.raster"), \
                patch("python.experiment_runner.summary_table"), patch("python.interleaved_probe.raster"), \
                patch.object(runner, "formal") as formal:
            runner.run()
            formal.assert_not_called()
        self.backend.shutdown.assert_called_once()

    def test_abcd_cycle_global_half_second_and_site_two_seconds(self):
        self.backend.route = MagicMock(wraps=self.backend.route)
        runner = ExperimentRunner(self.system, self.stimulation, self.path, self.backend, self.reporter)
        self.run_checked(runner)
        result = json.loads((self.path / "experiment_result.json").read_text())
        self.assertEqual(result["status"], "probe_complete")
        self.assertEqual(result["probe_trial_count"], 240)
        self.assertFalse(result["formal_started"])
        self.assertEqual(self.backend.route.call_count, 12)
        self.assertTrue(all(len(c.args[0]) == 4 for c in self.backend.route.call_args_list))
        h5s = list(self.path.glob("pre_experiment/02_probe/*/interleaved_attempt_01/*.h5"))
        self.assertEqual(len(h5s), 6)
        for h5 in h5s:
            unit = next(u for u in runner.units if u["unit_id"] == h5.parent.parent.name)
            with (h5.parent / "trial_manifest.csv").open(encoding="utf-8-sig", newline="") as f:
                manifest = list(csv.DictReader(f))
            electrodes = [json.loads(r["electrodes"])[0] for r in manifest]
            self.assertEqual(electrodes, unit["electrodes"] * 10)
            times = np.array([json.loads(r["onsets_sec"])[0] for r in manifest])
            np.testing.assert_allclose(np.diff(times), .5, atol=.00005)
            for i in range(4):
                np.testing.assert_allclose(np.diff(times[i::4]), 2.0, atol=.00005)
            self.assertTrue(all(r["routing_mode"] == "unit_pool_fast" for r in manifest))
            session = read_session(h5)
            self.assertEqual(len(session.events), 40)
            self.assertGreaterEqual(session.duration_sec - times[-1], .995 - .0001)
        with (self.path / "pre_experiment/probe_summary.csv").open(encoding="utf-8-sig", newline="") as f:
            summaries = list(csv.DictReader(f))
        self.assertEqual(len(summaries), 24)
        for row in summaries:
            self.assertEqual(row["all_trials"], "10")
            self.assertEqual(float(row["per_electrode_interval_sec"]), 2.0)
            self.assertTrue(Path(row["trial_metrics_path"]).is_file())
        self.assertFalse((self.path / "raw_data").exists())

    def test_routing_check_never_prepares_or_sends_a_sequence_or_records(self):
        self.backend.prepare_sequence = MagicMock(wraps=self.backend.prepare_sequence)
        runner = ExperimentRunner(self.system, self.stimulation, self.path, self.backend, self.reporter,
                                  routing_check=True)
        runner.prepare_baseline = MagicMock()
        self.run_checked(runner)
        runner.prepare_baseline.assert_not_called()
        self.backend.start_segment.assert_not_called()
        self.backend.prepare_sequence.assert_not_called()
        self.backend.stimulate.assert_not_called()
        result = json.loads((self.path / "experiment_result.json").read_text())
        self.assertEqual(result["status"], "probe_routing_check_complete")
        self.assertEqual(result["routable_unit_count"], 6)
        self.assertFalse(result["stimulation_sent"])
        self.assertEqual(result["probe_trial_count"], 0)
        self.assertFalse(list(self.path.rglob("*.h5")))
        state = json.loads((self.path / "pre_experiment/workflow_state.json").read_text())
        self.assertEqual(state["execution_scope"], "probe_routing_check")

    def test_fewer_than_three_routable_units_stops_before_any_probe(self):
        runner = ExperimentRunner(self.system, self.stimulation, self.path, self.backend, self.reporter)
        route = self.backend.route
        def route_with_conflict(electrodes):
            if electrodes in [u["electrodes"] for u in runner.units[:4]]:
                raise StimPoolConflict("shared stimulation unit")
            return route(electrodes)
        self.backend.route = MagicMock(side_effect=route_with_conflict)
        self.run_checked(runner)
        self.backend.stimulate.assert_not_called()
        result = json.loads((self.path / "experiment_result.json").read_text())
        self.assertEqual(result["status"], "probe_review_required")
        self.assertEqual(result["probe_trial_count"], 0)
        self.assertEqual(result["selected_formal_units"], [])
        self.assertFalse(list(self.path.glob("pre_experiment/02_probe/**/*.h5")))
        reports = json.loads((self.path / "pre_experiment/probe_routing_summary.json").read_text())
        self.assertEqual(sum(r["can_interleave"] for r in reports), 2)

    def test_unknown_api_failure_is_not_treated_as_a_pool_conflict(self):
        self.backend.route = MagicMock(side_effect=RuntimeError("communication failed"))
        runner = ExperimentRunner(self.system, self.stimulation, self.path, self.backend, self.reporter,
                                  routing_check=True)
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "communication failed"):
            runner.run()
        self.backend.stimulate.assert_not_called()
        self.backend.shutdown.assert_called_once()

    def test_small_h5_settings_inspection_preserves_source_and_bounds_output(self):
        path = self.path / "metadata.h5"
        with h5py.File(path, "w") as f:
            seg = f.create_group("data_store/data0000")
            seg.create_dataset("spikes", data=[])
            seg.create_dataset("settings/sampling", data=[20000])
            seg.create_dataset("settings/gain", data=[512])
            seg.create_dataset("settings/spike_threshold", data=[5.5])
            seg.create_dataset("settings/lsb", data=np.arange(1024, dtype=float))
        before = sha256(path)
        values = read_settings(path, 0)
        self.assertEqual(values["spike_threshold"], "[5.5]")
        self.assertEqual(values["hpf"], "未保存")
        self.assertLess(len(values["lsb"]), 100)
        self.assertEqual(sha256(path), before)


if __name__ == "__main__":
    unittest.main()
