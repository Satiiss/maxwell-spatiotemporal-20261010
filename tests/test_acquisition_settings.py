"""Prevent mismatched baseline/Probe counts before constructing a hardware backend."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import h5py

from main import ROOT, execute, load_configuration
from python.acquisition_settings import validate_baseline_acquisition
from python.utils.persistence import sha256


class AcquisitionChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "data").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data")
        self.addCleanup(self.temp.cleanup)
        self.system, self.stimulation = load_configuration(ROOT / "config")
        self.path = Path(self.temp.name) / "baseline.h5"
        self.system["manual_inputs"]["baseline_h5"] = str(self.path)
        self.system["maxwell"].update(well=0, event_threshold=5.0, amplifier_gain=512)
        self.make_settings()

    def make_settings(self, threshold=5.0, gain=512):
        with h5py.File(self.path, "w") as f:
            seg = f.create_group("data_store/data0000")
            seg.create_dataset("spikes", data=[])
            settings = seg.create_group("settings")
            if threshold is not None:
                settings.create_dataset("spike_threshold", data=threshold)
            settings.create_dataset("gain", data=gain)

    def test_matching_settings_accept_without_modifying_baseline(self):
        before = sha256(self.path)
        validate_baseline_acquisition(self.system)
        self.assertEqual(sha256(self.path), before)

    def test_mismatched_missing_and_nonuniform_settings_reject(self):
        for threshold, gain in ((8.5, 512), (5.0, 256), (None, 512),
                                ([5.0, 8.5], 512), (float("nan"), 512)):
            with self.subTest(threshold=threshold, gain=gain):
                self.make_settings(threshold, gain)
                with self.assertRaises(ValueError):
                    validate_baseline_acquisition(self.system)

    def test_repeated_uniform_threshold_is_accepted(self):
        self.make_settings([5.0] * 1024)
        validate_baseline_acquisition(self.system)

    def test_fresh_baseline_requires_no_existing_file(self):
        self.system["manual_inputs"]["baseline_h5"] = None
        validate_baseline_acquisition(self.system)

    def test_real_probe_rejects_mismatch_before_backend_construction(self):
        self.system["maxwell"]["event_threshold"] = 8.5
        self.system["data"]["root"] = str(Path(self.temp.name) / "runs")
        self.stimulation["probe"]["threshold_rule"] = "mean_plus_3variance"
        with patch("main.check_environment"), patch("main.validate"), \
                patch("python.maxwell_setup.MaxLabBackend") as backend, \
                self.assertLogs(level="ERROR"), self.assertRaisesRegex(ValueError, "spike_threshold=5"):
            execute(self.system, self.stimulation, mode="hardware")
        backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
