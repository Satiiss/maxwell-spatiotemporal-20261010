import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from main import ROOT, load_configuration, make_plan
from python.additional_probe import validate_repeat_probe
from python.experiment_runner import ExperimentRunner
from python.mock_backend import MockBackend, synthetic_mapping
from python.site_selection import build_local_units
from python.utils.persistence import atomic_json, sha256, write_csv
from python.utils.progress import Reporter
from tools.prepare_repeat_probe import prepare_repeat


class RepeatProbeChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "data").mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.mapping, self.centers = synthetic_mapping()
        for i in range(6, 10):
            cx, cy = 20 + 12*i, 30
            self.centers.append(220*cy + cx)
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                self.mapping.append(dict(channel=len(self.mapping), electrode=220*(cy+dy)+cx+dx,
                                         x=(cx+dx)*17.5, y=(cy+dy)*17.5))
        self.cfg = self.root / "archive.cfg"
        self.cfg.write_text("".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in self.mapping))
        self.system, self.stim = load_configuration(ROOT / "config")
        self.system["culture"].update(id="repeat_fixture", div=16)
        self.system["electrode_map"]["cfg_path"] = str(self.cfg)
        self.system["manual_inputs"]["baseline_h5"] = str(self.root / "baseline.h5")
        self.stim["probe"].update(threshold_rule="paired_pre_stim_ratio", order="sequential",
            paired_baseline_window_ms=[-50., -10.], response_to_baseline_ratio=1.5, enforce_artifact_qc=False)
        self.first, self.latest = self.root / "first", self.root / "latest"
        for run, centers, ranks in ((self.first, self.centers[:6], list(range(1, 7))),
                                    (self.latest, self.centers[6:], list(range(7, 11)))):
            (run / "config_snapshot").mkdir(parents=True)
            system = copy.deepcopy(self.system)
            system["manual_inputs"].update(candidate_centers=centers, candidate_ranks=ranks,
                additional_probe=run == self.latest, additional_probe_from=str(self.first) if run == self.latest else None)
            for name, value in (("system", system), ("stimulation", self.stim)):
                (run / f"config_snapshot/{name}.yaml").write_text(yaml.safe_dump(value), encoding="utf-8")
            atomic_json(run / "source_inputs.json", dict(cfg=str(self.cfg), cfg_sha256=sha256(self.cfg)))
            atomic_json(run / "experiment_result.json", dict(status="probe_review_required", formal_started=False, synthetic=True))
            units = build_local_units(self.mapping, centers, 52.5, additional=run == self.latest, ranks=ranks)
            write_csv(run / "pre_experiment/candidate_sites.csv", [dict(unit_id=u["unit_id"], center=u["center"],
                electrodes=";".join(map(str, u["electrodes"]))) for u in units])

    def prepare(self):
        with patch("tools.prepare_repeat_probe.ROOT", self.root):
            return prepare_repeat(self.latest, culture_div=17)

    def test_all_ten_units_and_forty_original_sites_are_preserved_without_old_scores(self):
        before = {p: sha256(p) for run in (self.first, self.latest) for p in run.rglob("*") if p.is_file()}
        config = self.prepare()
        system, stim = load_configuration(config)
        units = validate_repeat_probe(system, stim)
        self.assertEqual([u["rank"] for u in units], list(range(1, 11)))
        self.assertEqual([u["center"] for u in units], self.centers)
        self.assertEqual(len({e for u in units for e in u["electrodes"]}), 40)
        self.assertEqual(make_plan(system, stim, probe_only=True)["probe_trial_count"], 400)
        self.assertEqual(system["culture"]["div"], 17)
        self.assertEqual(stim["probe"]["min_success_trials"], 6)
        self.assertEqual(before, {p: sha256(p) for run in (self.first, self.latest) for p in run.rglob("*") if p.is_file()})

    def test_partial_roster_ambiguous_mode_and_synthetic_hardware_are_rejected(self):
        config = self.prepare()
        system, stim = load_configuration(config)
        with self.assertRaisesRegex(ValueError, "模拟"):
            validate_repeat_probe(system, stim, hardware=True)
        changed = copy.deepcopy(system)
        changed["manual_inputs"]["candidate_centers"].pop()
        changed["manual_inputs"]["candidate_ranks"].pop()
        with self.assertRaisesRegex(ValueError, "全部候选"):
            validate_repeat_probe(changed, stim)
        changed = copy.deepcopy(system)
        changed["manual_inputs"]["additional_probe"] = True
        with self.assertRaisesRegex(ValueError, "同时启用"):
            validate_repeat_probe(changed, stim)

    def test_mock_whole_repeat_completes_four_hundred_trials_without_formal(self):
        system, stim = load_configuration(self.prepare())
        system["mock"]["baseline_duration_override_sec"] = 40
        run = self.root / "new_repeat"
        run.mkdir()
        reporter = Reporter()
        backend = MockBackend(system, reporter)
        backend.mapping, backend.centers = self.mapping, self.centers
        runner = ExperimentRunner(system, stim, run, backend, reporter)
        with patch("python.experiment_runner.unit_map"), patch("python.experiment_runner.raster"), \
             patch("python.experiment_runner.summary_table"), patch.object(runner, "formal") as formal:
            runner.run()
        formal.assert_not_called()
        result = json.loads((run / "experiment_result.json").read_text())
        self.assertEqual(result["probe_trial_count"], 400)
        self.assertFalse(result["formal_started"])
        self.assertEqual(len(list((run / "pre_experiment/02_probe").rglob("trial_metrics.csv"))), 40)
        self.assertEqual(json.loads((run / "source_inputs.json").read_text())["repeat_probe_from"], str(self.latest))


if __name__ == "__main__":
    unittest.main()
