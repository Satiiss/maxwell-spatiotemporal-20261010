"""Candidate routing must preserve sources and never apply changes to hardware."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from main import ROOT, load_configuration
from python.mock_backend import synthetic_mapping
from python.prepare_probe_routing import prepare_candidate, mapping_comparison
from python.utils.persistence import sha256


class CandidateRoutingChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "data").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.mapping, centers = synthetic_mapping()
        self.system, _ = load_configuration(ROOT / "config")
        self.system["manual_inputs"]["candidate_centers"] = centers
        self.source = self.path / "source.cfg"
        self.text = "".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in self.mapping)
        self.source.write_text(self.text)
        self.system["electrode_map"]["cfg_path"] = str(self.source)
        self.mx = MagicMock()
        self.array = self.mx.Array.return_value
        for name in ("reset", "clear_selected_electrodes", "select_electrodes", "select_stimulation_electrodes",
                     "route", "load_config", "connect_electrode_to_stimulation"):
            getattr(self.array, name).return_value = "OK"
        def save(path):
            Path(path).write_text(self.text)
            return 0
        self.array.save_config.side_effect = save
        self.pool = {}
        def query(electrode):
            if electrode not in self.pool:
                self.pool[electrode] = len(self.pool)
            return str(self.pool[electrode])
        self.array.query_stimulation_at_electrode.side_effect = query

    def run_candidate(self):
        before = sha256(self.source)
        report = prepare_candidate(self.system, self.path / "output", self.mx, emit=MagicMock())
        self.assertEqual(sha256(self.source), before)
        self.array.download.assert_not_called()
        self.mx.initialize.assert_not_called()
        self.mx.send.assert_not_called()
        self.mx.Sequence.assert_not_called()
        self.array.close.assert_called_once()
        self.assertFalse(report["hardware_downloaded"])
        self.assertFalse(report["stimulation_sent"])
        self.assertNotEqual(report["source_cfg"], report["candidate_cfg"])
        return report

    def test_preserved_mapping_and_neighbours_are_ready_for_hardware_check(self):
        report = self.run_candidate()
        self.assertEqual(report["software_routable_unit_count"], 6)
        self.assertTrue(report["baseline_mapping_reusable"])
        self.assertEqual(report["status"], "candidate_for_hardware_routing_check")
        self.assertEqual(len(self.array.select_stimulation_electrodes.call_args.args[0]), 24)

    def test_shared_units_do_not_produce_a_usable_candidate(self):
        self.array.query_stimulation_at_electrode.side_effect = lambda e: "3"
        report = self.run_candidate()
        self.assertEqual(report["software_routable_unit_count"], 0)
        self.assertEqual(report["status"], "routing_unavailable")

    def test_missing_stimulus_unit_is_reported_without_replacing_electrode(self):
        self.array.query_stimulation_at_electrode.side_effect = lambda e: ""
        report = self.run_candidate()
        self.assertEqual(report["software_routable_unit_count"], 0)
        self.assertTrue(report["nearest_neighbour_units_unchanged"])

    def test_channel_changes_require_mapping_review(self):
        changed = [dict(r, channel=(r["channel"] + 1) % 1024) for r in self.mapping]
        self.text = "".join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in changed)
        report = self.run_candidate()
        self.assertFalse(report["baseline_mapping_reusable"])
        self.assertEqual(report["status"], "mapping_review_required")

    def test_electrode_set_change_is_detected(self):
        comparison = mapping_comparison(self.mapping, self.mapping[:-1])
        self.assertEqual(comparison["missing_electrodes"], [self.mapping[-1]["electrode"]])
        self.assertFalse(comparison["exact_recording_mapping"])

    def test_unknown_sdk_error_closes_scratch_array_without_hardware_calls(self):
        self.array.route.return_value = "ERROR"
        with self.assertRaisesRegex(RuntimeError, "生成候选路由"):
            prepare_candidate(self.system, self.path / "output", self.mx, emit=MagicMock())
        self.array.close.assert_called_once()
        self.array.download.assert_not_called()
        self.mx.initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
