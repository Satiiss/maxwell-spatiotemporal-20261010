"""Independent formal selection, evidence audit and zero-stimulus execution boundaries."""
import copy
import csv
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import yaml

from main import ROOT, execute, load_configuration, validate
from python.formal_selection import load_formal_selection, validate_formal_selection
from python.mock_backend import synthetic_mapping
from python.utils.persistence import atomic_json, sha256, write_csv


class FormalSelectionChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "data").mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.system, self.stim = load_configuration(ROOT / "config")
        self.system["culture"].update(id="formal_fixture", div=17)
        self.system["data"]["root"] = str(self.root / "outputs")
        mapping, centers = synthetic_mapping()
        self.pairs = [[centers[i], centers[i] + 1] for i in range(3)]
        cfg = self.root / "recording.cfg"
        cfg.write_text("".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in mapping))
        self.system["electrode_map"]["cfg_path"] = str(cfg)
        self.cfg = cfg
        for name, groups in (("first", [0]), ("second", [1, 2])):
            source = self.root / name
            (source / "config_snapshot").mkdir(parents=True)
            for kind, value in (("system", self.system), ("stimulation", self.stim)):
                (source / f"config_snapshot/{kind}.yaml").write_text(yaml.safe_dump(value), encoding="utf-8")
            atomic_json(source / "source_inputs.json", dict(cfg_sha256=sha256(cfg)))
            atomic_json(source / "experiment_result.json", dict(status="probe_review_required",
                formal_started=False, synthetic=True, probe_trial_count=len(groups) * 40))
            write_csv(source / "pre_experiment/candidate_sites.csv", [dict(unit_id=f"candidate_{i+1:02d}",
                electrodes=";".join(map(str, self.pairs[i]))) for i in groups])
            write_csv(source / "pre_experiment/probe_summary.csv", [dict(unit_id=f"candidate_{i+1:02d}",
                electrode=e, status="complete", success_trials=5 if i == 0 and j == 1 else 6,
                all_trials=10, success_fraction=.5 if i == 0 and j == 1 else .6,
                eligible=False, response_10_50ms_mean=20) for i in groups for j, e in enumerate(self.pairs[i])])
        self.selection = dict(culture_id="formal_fixture", minimum_success_trials=6, units=[
            dict(source_run="first" if i == 0 else "second", source_unit=f"candidate_{i+1:02d}",
                 electrodes=pair, exceptions={pair[1]: "Explicit manual inclusion at 5/10"} if i == 0 else {})
            for i, pair in enumerate(self.pairs)])
        self.file = self.root / "selection.json"
        self.save_selection()
        # These fields must be irrelevant to the independent formal execution path.
        self.system.pop("manual_inputs")
        self.stim.pop("probe")
        self.system["mock"]["trials_per_condition_override"] = 1
        self.stim["formal"].update(pre_spontaneous_sec=1, post_spontaneous_sec=1,
                                  rest_between_stages_sec=0, rest_after_last_stage_sec=0)

    def save_selection(self):
        atomic_json(self.file, self.selection)

    def hashes(self):
        return {p: sha256(p) for name in ("first", "second") for p in (self.root / name).rglob("*") if p.is_file()}

    def test_cross_run_review_required_evidence_keeps_real_scores_and_exceptions(self):
        before = self.hashes()
        units, audit = load_formal_selection(self.file, self.system, self.stim)
        self.assertEqual(len(units), 3)
        self.assertEqual(len(audit["sources"]), 2)
        first = units[0]["site_metrics"][1]
        self.assertEqual(first["success_trials"], 5)
        self.assertFalse(first["meets_reference_policy"])
        self.assertTrue(first["manual_exception_reason"])
        self.assertFalse(units[1]["site_metrics"][0]["source_eligible"])
        self.assertTrue(units[1]["site_metrics"][0]["meets_reference_policy"])
        self.assertEqual(before, self.hashes())

    def test_low_score_requires_a_site_specific_reason_and_incomplete_cannot_pass(self):
        changed = copy.deepcopy(self.selection)
        changed["units"][0]["exceptions"] = {}
        with self.assertRaisesRegex(ValueError, "5/10"):
            validate_formal_selection(changed, self.system, self.stim, self.root)
        summary = self.root / "first/pre_experiment/probe_summary.csv"
        with summary.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        rows[1].update(all_trials=9, success_fraction=5/9)
        write_csv(summary, rows)
        with self.assertRaisesRegex(ValueError, "完整10次"):
            load_formal_selection(self.file, self.system, self.stim)

    def test_structural_and_provenance_mismatches_are_rejected_before_device_access(self):
        for kind in ("culture", "duplicate", "unknown", "unit", "exception", "bad_minimum"):
            selected = copy.deepcopy(self.selection)
            if kind == "culture": selected["culture_id"] = "other"
            if kind == "duplicate": selected["units"][2]["electrodes"] = self.pairs[0]
            if kind == "unknown": selected["units"][2]["electrodes"][0] = 999999
            if kind == "unit": selected["units"][2]["source_unit"] = "missing"
            if kind == "exception": selected["units"][0]["exceptions"] = {999999: "invalid"}
            if kind == "bad_minimum": selected["minimum_success_trials"] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_formal_selection(selected, self.system, self.stim, self.root)
        with self.assertRaisesRegex(ValueError, "模拟"):
            load_formal_selection(self.file, self.system, self.stim, hardware=True)
        with patch("python.maxwell_setup.MaxLabBackend") as device:
            with self.assertRaisesRegex(ValueError, "模拟"):
                execute(self.system, self.stim, mode="hardware", formal_selection=self.file)
        device.assert_not_called()
        for kind in ("cfg", "gain", "waveform"):
            system, stimulation = copy.deepcopy(self.system), copy.deepcopy(self.stim)
            if kind == "cfg":
                other = self.root / "other.cfg"
                other.write_bytes(self.cfg.read_bytes() + b"\n")
                system["electrode_map"]["cfg_path"] = str(other)
            if kind == "gain": system["maxwell"]["amplifier_gain"] = 256
            if kind == "waveform": stimulation["protocol"]["amplitude_mv"] = 300
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                load_formal_selection(self.file, system, stimulation)

    def test_manual_unscored_selection_works_without_any_probe_configuration(self):
        manual = dict(culture_id="formal_fixture", units=[dict(name=f"manual_{i}", electrodes=pair,
            selection_reason="Independent manual selection") for i, pair in enumerate(self.pairs)])
        units, reference = validate_formal_selection(manual, self.system, self.stim, self.root, hardware=True)
        self.assertEqual(reference["sources"], [])
        self.assertIsNone(units[0]["site_metrics"][0]["success_trials"])
        manual["units"][0].pop("selection_reason")
        with self.assertRaisesRegex(ValueError, "selection_reason"):
            validate_formal_selection(manual, self.system, self.stim, self.root)
        validate(self.system, self.stim, probe_only=False)

    def test_python_selection_file_and_dry_run_archive_exact_sites_without_sdk(self):
        python_file = self.root / "editable.py"
        python_file.write_text("SELECTION = " + repr(self.selection), encoding="utf-8")
        before = self.hashes()
        with patch("python.maxwell_setup.MaxLabBackend") as device:
            output = execute(self.system, self.stim, formal_selection=python_file, dry_run=True)
        device.assert_not_called()
        plan = json.loads((output / "experiment_plan.json").read_text())
        self.assertEqual(plan["formal_trial_count"], 5400)
        self.assertEqual(plan["probe_trial_count"], 0)
        self.assertEqual([u["electrodes"] for u in plan["formal_units"]], self.pairs)
        self.assertTrue((output / "formal_condition_plan.csv").is_file())
        self.assertFalse((output / "raw_data").exists())
        self.assertEqual(python_file.read_bytes(), (output / "formal_selection_source.py").read_bytes())
        self.assertEqual(before, self.hashes())

    def test_formal_mock_records_three_segments_and_never_evaluates_or_runs_probe(self):
        before = self.hashes()
        with patch("python.experiment_runner.unit_map"), \
             patch("python.experiment_runner.ExperimentRunner.probes", side_effect=AssertionError("No Probe")), \
             patch("python.experiment_runner.ExperimentRunner.prepare_baseline", side_effect=AssertionError("No old baseline")), \
             patch("python.probe_handoff.load_probe_handoff", side_effect=AssertionError("No legacy handoff")):
            output = execute(self.system, self.stim, formal_selection=self.file)
        result = json.loads((output / "experiment_result.json").read_text())
        self.assertEqual(result["formal_trial_count"], 108)
        self.assertEqual(result["probe_trial_count"], 0)
        self.assertTrue(result["formal_started"])
        self.assertEqual(len(list((output / "raw_data").rglob("*.raw.h5"))), 3)
        self.assertFalse((output / "pre_experiment/02_probe").exists())
        self.assertEqual(before, self.hashes())

    def test_formal_route_check_never_creates_sequences_or_recordings(self):
        with patch("python.experiment_runner.unit_map"), \
             patch("python.mock_backend.MockBackend.prepare_sequence", side_effect=AssertionError("No Sequence")), \
             patch("python.mock_backend.MockBackend.stimulate", side_effect=AssertionError("No stimulus")), \
             patch("python.mock_backend.MockBackend.start_segment", side_effect=AssertionError("No recording")):
            output = execute(self.system, self.stim, formal_selection=self.file, formal_routing_check=True)
        result = json.loads((output / "experiment_result.json").read_text())
        self.assertEqual(result["status"], "formal_routing_check_complete")
        self.assertFalse(result["formal_started"])
        self.assertFalse(result["stimulation_sent"])
        self.assertEqual(len(result["stimulation_pool"]), 6)
        self.assertFalse(list(output.rglob("*.raw.h5")))
        plan = json.loads((output / "experiment_plan.json").read_text())
        self.assertEqual(plan["formal_trial_count"], 0)
        self.assertEqual(plan["planned_formal_trial_count"], 5400)
        self.assertEqual(plan["stages"], [])

    def test_routing_conflict_stops_before_recording(self):
        with patch("python.experiment_runner.unit_map"), \
             patch("python.mock_backend.MockBackend.route", return_value={e: 1 for p in self.pairs for e in p}), \
             patch("python.mock_backend.MockBackend.start_segment", side_effect=AssertionError("No recording")):
            with self.assertRaisesRegex(ValueError, "6个不同"):
                execute(self.system, self.stim, formal_selection=self.file, formal_routing_check=True)

    def test_standalone_entry_defaults_to_plan_only_after_editing_file(self):
        config = self.root / "config"
        config.mkdir()
        self.system["manual_inputs"] = None
        self.stim["probe"] = None
        for name, value in (("system", self.system), ("stimulation", self.stim)):
            (config / f"{name}.yaml").write_text(yaml.safe_dump(value), encoding="utf-8")
        template = (ROOT / "run_formal.py").read_text(encoding="utf-8")
        start, end = template.index("SELECTION = {"), template.index("# End editable section")
        template = template[:start] + "SELECTION = " + repr(self.selection) + "\n" + template[end:]
        template = template.replace('CONFIG_DIR = "config"', 'CONFIG_DIR = ' + repr(str(config)))
        template = template.replace("INTER_SEQUENCE_REST_SEC = None", "INTER_SEQUENCE_REST_SEC = 3.0")
        # This case only tests the default no-device entry, using explicit manual selections.
        selected = copy.deepcopy(self.selection)
        for unit in selected["units"]:
            unit.pop("source_run")
            unit["selection_reason"] = "Manual default-entry test"
        start, end = template.index("SELECTION = "), template.index("# End editable section")
        template = template[:start] + "SELECTION = " + repr(selected) + "\n" + template[end:]
        script = self.root / "run_formal.py"
        script.write_text(template, encoding="utf-8")
        with patch("sys.argv", [str(script)]), redirect_stdout(io.StringIO()), \
             patch("python.maxwell_setup.MaxLabBackend") as device:
            runpy.run_path(str(script), run_name="__main__")
        device.assert_not_called()
        outputs = list((self.root / "outputs").glob("*/dry_run.json"))
        self.assertEqual(len(outputs), 1)
        self.assertFalse(json.loads(outputs[0].read_text())["hardware_contacted"])
        plan = json.loads((outputs[0].parent / "experiment_plan.json").read_text())
        self.assertEqual(plan["trial_interval_sec"], 3.)
        self.assertEqual(plan["temporal_interval_mode"], "after_second")


if __name__ == "__main__":
    unittest.main()
