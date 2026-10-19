"""Center-list acquisition must work without a previous Probe or review artifact."""
import copy
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np

from main import ROOT, execute, load_configuration
from python.experiment_runner import ExperimentRunner
from python.mock_backend import MAP_DTYPE, MockBackend, synthetic_mapping
from python.site_selection import build_local_units
from python.utils.persistence import sha256
from python.utils.progress import Reporter
from run_probe import configure_probe, preflight


class CenterProbeChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "data").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mapping, self.centers = synthetic_mapping()
        for i in range(6, 12):
            cx, cy = 20 + 12*i, 30
            self.centers.append(220*cy + cx)
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                self.mapping.append(dict(channel=len(self.mapping), electrode=220*(cy+dy)+cx+dx,
                    x=(cx+dx)*17.5, y=(cy+dy)*17.5))
        self.cfg = self.root / "current.cfg"
        self.cfg.write_text("".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in self.mapping))
        self.baseline = self.root / "network.h5"
        with h5py.File(self.baseline, "w") as handle:
            seg = handle.create_group("data_store/data0000")
            seg.create_dataset("spikes", data=[])
            seg.create_dataset("settings/spike_threshold", data=[5.])
            seg.create_dataset("settings/gain", data=[512.])
            seg.create_dataset("settings/mapping", data=np.array(
                [(r['channel'], r['electrode'], r['x'], r['y']) for r in self.mapping], dtype=MAP_DTYPE))
        self.system, self.stim = load_configuration(ROOT / "config")
        self.system["culture"].update(id="current_fixture", div=16)
        self.system["electrode_map"]["cfg_path"] = str(self.cfg)
        self.system["data"]["root"] = str(self.root / "runs")
        self.system["maxwell"]["event_threshold"] = 5.
        self.system["manual_inputs"].update(baseline_h5=str(self.baseline),
            additional_probe=True, additional_probe_from="nonexistent_old_run",
            repeat_probe=True, repeat_probe_from="another_nonexistent_run",
            candidate_ranks=[99], network_review_record="no_review.json", baseline_bursts_csv="no_bursts.csv")

    def configured(self, count=10):
        return configure_probe(self.system, self.stim, self.centers[:count], culture_div=17)

    def test_arbitrary_center_counts_select_physical_neighbors_without_history(self):
        original = copy.deepcopy((self.system, self.stim))
        before = sha256(self.cfg), sha256(self.baseline)
        for count in (1, 4, 10, 12):
            system, stim = self.configured(count)
            plan = preflight(system, stim)
            self.assertEqual(plan["probe_trial_count"], count*40)
            self.assertEqual([u["center"] for u in plan["candidate_units"]], self.centers[:count])
            self.assertEqual(plan["min_success_trials"], 6)
            self.assertEqual(system["culture"]["div"], 17)
            for unit in plan["candidate_units"]:
                self.assertEqual(len(unit["electrodes"]), 4)
                self.assertEqual([round(d, 2) for d in unit["distances_um"]], [0., 17.5, 17.5, 24.75])
            self.assertNotIn("additional_probe", system["manual_inputs"])
            self.assertNotIn("repeat_probe", system["manual_inputs"])
        self.assertEqual((self.system, self.stim), original)
        self.assertEqual((sha256(self.cfg), sha256(self.baseline)), before)

    def test_invalid_ids_unknown_centers_and_overlapping_units_reject(self):
        for centers in ([], [self.centers[0]]*2, [True], [1.5], [-1], [999999]):
            with self.subTest(centers=centers), self.assertRaises(ValueError):
                build_local_units(self.mapping, centers, 52.5)
        with self.assertRaisesRegex(ValueError, "重叠"):
            build_local_units(self.mapping, [self.centers[0], self.centers[0]+1], 0)

    def test_current_culture_inputs_override_old_yaml_without_modifying_sources(self):
        original = copy.deepcopy((self.system, self.stim))
        before = sha256(self.cfg), sha256(self.baseline)
        system, stim = configure_probe(self.system, self.stim, self.centers[:6],
            culture_id="new_culture", culture_div=38, baseline_h5=self.baseline,
            cfg_path=self.cfg, event_threshold=5., amplifier_gain=512)
        plan = preflight(system, stim)
        self.assertEqual(plan["probe_trial_count"], 240)
        self.assertEqual(system["culture"], {"id": "new_culture", "div": 38})
        self.assertEqual(system["manual_inputs"]["baseline_h5"], str(self.baseline))
        self.assertEqual(system["electrode_map"]["cfg_path"], str(self.cfg))
        self.assertEqual(system["maxwell"]["event_threshold"], 5.)
        self.assertEqual(system["maxwell"]["amplifier_gain"], 512)
        self.assertEqual((self.system, self.stim), original)
        self.assertEqual((sha256(self.cfg), sha256(self.baseline)), before)
        for kwargs in ({"culture_id": ""}, {"culture_id": True}, {"cfg_path": ""},
                       {"baseline_h5": " "}, {"event_threshold": float("nan")},
                       {"event_threshold": -1}, {"amplifier_gain": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                configure_probe(self.system, self.stim, self.centers[:6], **kwargs)

    def test_preflight_rejects_changed_h5_mapping_and_settings(self):
        system, stim = self.configured()
        with h5py.File(self.baseline, "r+") as handle:
            data = handle["data_store/data0000/settings/mapping"]
            rows = data[:]
            rows[0]["electrode"] += 100
            data[:] = rows
        with self.assertRaisesRegex(ValueError, "映射冲突"):
            preflight(system, stim)
        system["maxwell"]["event_threshold"] = 8.5
        with self.assertRaisesRegex(ValueError, "spike_threshold=5"):
            preflight(system, stim)

    def test_default_entry_generates_map_and_four_hundred_trial_plan_without_sdk(self):
        import run_probe
        before = sha256(self.cfg), sha256(self.baseline)
        with patch.object(run_probe, "CENTER_ELECTRODES", self.centers[:10]), \
             patch.object(run_probe, "CULTURE_DIV", 17), \
             patch.object(run_probe, "CULTURE_ID", "current_top_fields"), \
             patch.object(run_probe, "BASELINE_H5", str(self.baseline)), \
             patch.object(run_probe, "CFG_PATH", str(self.cfg)), \
             patch.object(run_probe, "EVENT_THRESHOLD", 5.), \
             patch.object(run_probe, "AMPLIFIER_GAIN", 512), \
             patch("main.load_configuration", return_value=(self.system, self.stim)), \
             patch("python.maxwell_setup.MaxLabBackend") as backend, \
             patch.object(sys, "argv", ["run_probe.py"]):
            run_probe.main()
        backend.assert_not_called()
        run = next((self.root / "runs").iterdir())
        plan = json.loads((run / "experiment_plan.json").read_text())
        self.assertEqual(plan["probe_trial_count"], 400)
        self.assertEqual(len(plan["candidate_units"]), 10)
        import yaml
        snapshot = yaml.safe_load((run / "config_snapshot/system.yaml").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["culture"]["id"], "current_top_fields")
        self.assertEqual(snapshot["manual_inputs"]["baseline_h5"], str(self.baseline))
        self.assertEqual(snapshot["maxwell"]["event_threshold"], 5.)
        self.assertFalse(json.loads((run / "dry_run.json").read_text())["hardware_contacted"])
        self.assertTrue((run / "pre_experiment/candidate_units.png").is_file())
        with (run / "pre_experiment/candidate_sites.csv").open(encoding="utf-8-sig") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 10)
        self.assertFalse(list(run.rglob("*.h5")))
        self.assertEqual((sha256(self.cfg), sha256(self.baseline)), before)

    def test_mock_subset_preserves_centers_and_never_requires_global_burst_background(self):
        system, stim = self.configured(4)
        system["mock"]["baseline_duration_override_sec"] = 40
        run = self.root / "mock"
        run.mkdir()
        reporter = Reporter()
        backend = MockBackend(system, reporter)
        backend.mapping, backend.centers = self.mapping, self.centers
        runner = ExperimentRunner(system, stim, run, backend, reporter)
        with patch("python.experiment_runner.unit_map"), patch("python.experiment_runner.raster"), \
             patch("python.experiment_runner.summary_table"), patch.object(runner, "formal") as formal, \
             patch("python.experiment_runner.detect_burst_intervals") as burst, \
             patch("python.experiment_runner.baseline_distribution") as global_background:
            runner.run()
        formal.assert_not_called()
        burst.assert_not_called()
        global_background.assert_not_called()
        self.assertEqual([u["center"] for u in runner.units], self.centers[:4])
        result = json.loads((run / "experiment_result.json").read_text())
        self.assertEqual(result["probe_trial_count"], 160)
        self.assertFalse(result["formal_started"])
        self.assertEqual(len(list((run / "pre_experiment/02_probe").rglob("trial_metrics.csv"))), 16)


if __name__ == "__main__":
    unittest.main()
