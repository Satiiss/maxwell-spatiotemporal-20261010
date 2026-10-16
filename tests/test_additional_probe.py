"""Additional units preserve provenance, ranks and explicit inclusive trial counts."""
import copy
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import yaml

from main import ROOT, load_configuration
from analysis.utils.probe import evaluate_probe, minimum_probe_successes
from python.additional_probe import validate_additional_probe
from python.mock_backend import synthetic_mapping, MockBackend
from python.site_selection import build_local_units
from python.utils.persistence import atomic_json, sha256, write_csv
from python.utils.progress import Reporter
from tools.prepare_additional_probe import prepare


class AdditionalProbeChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "data").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "config_snapshot").mkdir(parents=True)
        self.system, self.stim = load_configuration(ROOT / "config")
        self.system["culture"].update(id="fixture", div=16)
        self.mapping, self.centers = synthetic_mapping()
        self.cfg = self.source / "archived.cfg"
        self.cfg.write_text("".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in self.mapping))
        self.system["electrode_map"]["cfg_path"] = str(self.cfg)
        self.system["manual_inputs"].update(candidate_centers=self.centers[:2], baseline_h5=str(self.root / "baseline.h5"))
        self.stim["probe"].update(threshold_rule="paired_pre_stim_ratio", response_window_ms=[10., 50.],
            paired_baseline_window_ms=[-50., -10.], response_to_baseline_ratio=1.5,
            enforce_artifact_qc=False, order="sequential")
        for name, content in (("system", self.system), ("stimulation", self.stim)):
            (self.source / f"config_snapshot/{name}.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")
        atomic_json(self.source / "experiment_result.json", dict(status="probe_review_required", formal_started=False, synthetic=True))
        atomic_json(self.source / "source_inputs.json", dict(cfg=str(self.cfg), cfg_sha256=sha256(self.cfg)))
        old = build_local_units(self.mapping, self.centers[:2], 52.5, additional=True)
        write_csv(self.source / "pre_experiment/candidate_sites.csv", [dict(unit_id=u["unit_id"],
            electrodes=";".join(map(str, u["electrodes"]))) for u in old])
        self.system["manual_inputs"].update(additional_probe=True, additional_probe_from=str(self.source),
            candidate_centers=self.centers[2:], candidate_ranks=[7, 8, 9, 10])
        self.stim["probe"]["min_success_trials"] = 6

    def test_four_additional_units_keep_original_ranks_and_reject_reuse(self):
        with self.assertRaises(ValueError):
            build_local_units(self.mapping, self.centers[2:], 52.5)
        units = validate_additional_probe(self.system, self.stim)
        self.assertEqual([u["unit_id"] for u in units], [f"candidate_{i:02d}" for i in range(7, 11)])
        self.system["manual_inputs"]["candidate_centers"][0] = self.centers[0]
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_additional_probe(self.system, self.stim)

    def test_changed_source_and_synthetic_hardware_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "模拟"):
            validate_additional_probe(self.system, self.stim, hardware=True)
        for kind in ("culture", "cfg", "baseline", "ranks", "waveform"):
            system, stim = copy.deepcopy(self.system), copy.deepcopy(self.stim)
            if kind == "culture": system["culture"]["id"] = "other"
            if kind == "cfg":
                changed = self.root / "changed.cfg"
                changed.write_bytes(self.cfg.read_bytes() + b"\n")
                system["electrode_map"]["cfg_path"] = str(changed)
            if kind == "baseline": system["manual_inputs"]["baseline_h5"] = "other.h5"
            if kind == "ranks": system["manual_inputs"]["candidate_ranks"] = [1, 8, 9, 10]
            if kind == "waveform": stim["protocol"]["amplitude_mv"] = 300
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_additional_probe(system, stim)

    def test_six_of_ten_requires_complete_trials_and_keeps_legacy_seven(self):
        onsets = np.arange(10) + 1.
        for count in (5, 6, 7):
            points = np.sort(np.concatenate([t + np.r_[[-.04, -.03], np.linspace(.015, .035, 4 if i < count else 3)]
                                            for i, t in enumerate(onsets)]))
            session = SimpleNamespace(times=points, duration_sec=20)
            _, summary = evaluate_probe(session, onsets, None, self.stim["probe"])
            self.assertEqual(summary["eligible"], count >= 6)
            _, incomplete = evaluate_probe(session, onsets[:9], None, self.stim["probe"])
            self.assertFalse(incomplete["eligible"])
            legacy = dict(self.stim["probe"]); legacy.pop("min_success_trials")
            _, previous = evaluate_probe(session, onsets, None, legacy)
            self.assertEqual(previous["eligible"], count >= 7)
        for value in (0, 11, 6.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                minimum_probe_successes(dict(self.stim["probe"], min_success_trials=value))

    def test_prepare_new_config_preserves_source_and_checks_review(self):
        review = self.root / "candidate_units.json"
        units = build_local_units(self.mapping, self.centers[2:], 52.5, additional=True, ranks=[7, 8, 9, 10])
        atomic_json(review, dict(units=units))
        before = {p: sha256(p) for p in self.source.rglob("*") if p.is_file()}
        with patch("tools.prepare_additional_probe.ROOT", self.root):
            destination = prepare(self.source, review)
        system, stim = load_configuration(destination)
        self.assertEqual(system["culture"]["id"], "fixture")
        self.assertEqual(stim["probe"]["min_success_trials"], 6)
        self.assertEqual(json.loads((destination / "additional_probe_plan.json").read_text())["expected_probe_trial_count"], 160)
        self.assertEqual(before, {p: sha256(p) for p in self.source.rglob("*") if p.is_file()})
        units[0]["electrodes"][1] = -1
        atomic_json(review, dict(units=units))
        with patch("tools.prepare_additional_probe.ROOT", self.root), self.assertRaisesRegex(ValueError, "最近邻"):
            prepare(self.source, review)

    def test_mock_additional_probe_tests_only_sixteen_new_sites(self):
        from python.experiment_runner import ExperimentRunner
        self.system["mock"]["baseline_duration_override_sec"] = 40
        reporter = Reporter()
        run = self.root / "new_run"
        run.mkdir()
        runner = ExperimentRunner(self.system, self.stim, run, MockBackend(self.system, reporter), reporter)
        with patch("python.experiment_runner.unit_map"), patch("python.experiment_runner.raster"), \
             patch("python.experiment_runner.summary_table"), patch.object(runner, "formal") as formal:
            runner.run()
        formal.assert_not_called()
        result = json.loads((run / "experiment_result.json").read_text())
        self.assertEqual(result["probe_trial_count"], 160)
        self.assertFalse(result["formal_started"])
        with (run / "pre_experiment/probe_summary.csv").open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 16)
        self.assertEqual({r["unit_id"] for r in rows}, {f"candidate_{i:02d}" for i in range(7, 11)})
        self.assertTrue(all(int(r["all_trials"]) == 10 for r in rows))


if __name__ == "__main__":
    unittest.main()
