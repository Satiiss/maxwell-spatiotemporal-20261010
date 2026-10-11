"""Checks for two separate acquisitions and reviewed Probe input validation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

from main import ROOT, load_configuration, execute
from python.site_selection import build_local_units
from python.mock_backend import synthetic_mapping
from python.probe_handoff import load_probe_handoff
from python.utils.persistence import atomic_json, sha256, write_csv


class HandoffChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "data").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "probe"
        self.pre = self.source / "pre_experiment"
        self.pre.mkdir(parents=True)
        self.system, self.stimulation = load_configuration(ROOT / "config")
        self.stimulation["probe"].update(threshold_rule="mean_plus_3variance", enforce_artifact_qc=False)
        self.system["culture"].update(id="28878", div=30)
        mapping, centers = synthetic_mapping()
        self.system["manual_inputs"]["candidate_centers"] = centers
        cfg = self.source / "probe.cfg"
        cfg.write_text("".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in mapping))
        self.system["electrode_map"]["cfg_path"] = str(cfg)
        units = build_local_units(mapping, centers, 52.5)
        rows = [dict(unit_id=u["unit_id"], electrode=e, eligible=True, status="complete",
                     all_trials=10, success_trials=7, success_fraction=.7, response_10_50ms_mean=20)
                for u in units for e in u["electrodes"]]
        write_csv(self.pre / "probe_summary.csv", rows)
        self.selected = [dict(source_unit=u["unit_id"], electrodes=u["electrodes"][:2]) for u in units[:3]]
        (self.pre / "selected_formal_sites.yaml").write_text(yaml.safe_dump(self.selected))
        (self.source / "config_snapshot").mkdir()
        (self.source / "config_snapshot/system.yaml").write_text(yaml.safe_dump(self.system))
        (self.source / "config_snapshot/stimulation.yaml").write_text(yaml.safe_dump(self.stimulation))
        atomic_json(self.source / "source_inputs.json", dict(cfg_sha256=sha256(cfg)))
        self.result = dict(status="probe_complete", formal_started=False, synthetic=True, probe_trial_count=240)
        atomic_json(self.source / "experiment_result.json", self.result)

    def test_valid_reviewed_selection_loads_without_hardware(self):
        units, reference = load_probe_handoff(self.source, self.system, self.stimulation)
        self.assertEqual(len(units), 3)
        self.assertEqual(len({e for u in units for e in u["electrodes"]}), 6)
        self.assertEqual(reference["source_probe_trial_count"], 240)

    def test_changed_spike_detection_or_probe_order_is_rejected(self):
        for key, value in (("event_threshold", 5.5), ("amplifier_gain", 256)):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.system)
                changed["maxwell"][key] = value
                with self.assertRaisesRegex(ValueError, "spike检测阈值或gain"):
                    load_probe_handoff(self.source, changed, self.stimulation)
        changed = copy.deepcopy(self.stimulation)
        changed["probe"]["order"] = "unit_interleaved"
        with self.assertRaisesRegex(ValueError, "顺序"):
            load_probe_handoff(self.source, self.system, changed)

    def test_incomplete_probe_and_synthetic_hardware_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "模拟Probe"):
            load_probe_handoff(self.source, self.system, self.stimulation, hardware=True)
        atomic_json(self.source / "experiment_result.json", dict(self.result, status="probe_review_required"))
        with self.assertRaises(ValueError):
            load_probe_handoff(self.source, self.system, self.stimulation)

    def test_changed_culture_cfg_and_old_artifact_gate_are_rejected(self):
        changed = copy.deepcopy(self.system)
        changed["culture"]["id"] = "another"
        with self.assertRaisesRegex(ValueError, "培养物"):
            load_probe_handoff(self.source, changed, self.stimulation)
        cfg = Path(self.system["electrode_map"]["cfg_path"])
        original = cfg.read_bytes()
        cfg.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "CFG"):
            load_probe_handoff(self.source, self.system, self.stimulation)
        cfg.write_bytes(original)
        changed_stim = copy.deepcopy(self.stimulation)
        changed_stim["probe"]["enforce_artifact_qc"] = True
        (self.source / "config_snapshot/stimulation.yaml").write_text(yaml.safe_dump(changed_stim))
        with self.assertRaisesRegex(ValueError, "伪迹硬门槛"):
            load_probe_handoff(self.source, self.system, self.stimulation)

    def test_unprobed_or_six_of_ten_selection_is_rejected(self):
        changed = copy.deepcopy(self.selected)
        changed[0]["electrodes"][0] = 999999
        (self.pre / "selected_formal_sites.yaml").write_text(yaml.safe_dump(changed))
        with self.assertRaises(ValueError):
            load_probe_handoff(self.source, self.system, self.stimulation)
        (self.pre / "selected_formal_sites.yaml").write_text(yaml.safe_dump(self.selected))
        import csv
        with (self.pre / "probe_summary.csv").open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        rows[0].update(success_trials=6, success_fraction=.6)
        write_csv(self.pre / "probe_summary.csv", rows)
        with self.assertRaisesRegex(ValueError, "至少7次"):
            load_probe_handoff(self.source, self.system, self.stimulation)

    def test_formal_only_mock_acquisition_creates_no_probe_segments(self):
        self.system["data"]["root"] = str(Path(self.temporary.name) / "formal_runs")
        with patch("python.experiment_runner.unit_map"), self.assertLogs(level="INFO"):
            output = execute(self.system, self.stimulation, mode="mock", quick=True, formal_from=self.source)
        result = json.loads((output / "experiment_result.json").read_text())
        self.assertEqual(result["formal_trial_count"], 216)
        self.assertEqual(result["probe_trial_count"], 0)
        self.assertTrue(result["formal_started"])
        self.assertFalse((output / "pre_experiment/02_probe").exists())
        self.assertTrue((output / "pre_experiment/probe_source_reference.json").is_file())
        self.assertEqual(len(list((output / "raw_data").rglob("*.raw.h5"))), 3)


if __name__ == "__main__":
    unittest.main()
