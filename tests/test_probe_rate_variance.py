"""Verify rate scaling and preserve official inputs during offline comparison."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

import numpy as np
import yaml

from main import ROOT
from tools.probe_rate_variance import rate_statistics, review_rate_variance
from python.utils.persistence import atomic_json, sha256, write_csv


class RateVarianceChecks(unittest.TestCase):
    def test_rate_variance_scales_by_window_squared(self):
        counts = np.array([0, 1] * 10)
        stats = rate_statistics(counts, .04)
        self.assertAlmostEqual(stats["mean_rate_hz"], counts.mean() / .04)
        self.assertAlmostEqual(stats["variance_rate_hz2"], counts.var(ddof=1) / .04 ** 2)
        self.assertAlmostEqual(stats["equivalent_count_threshold"], counts.mean() + 3 * counts.var(ddof=1) / .04)
        self.assertGreater(stats["equivalent_count_threshold"], counts.mean() + 3 * counts.var(ddof=1))

    def test_offline_comparison_preserves_source_and_requires_seven_of_ten(self):
        (ROOT / "data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "data") as directory:
            source = Path(directory) / "source"
            pre = source / "pre_experiment"
            pre.mkdir(parents=True)
            snapshot = source / "config_snapshot"
            snapshot.mkdir()
            probe = dict(response_window_ms=[10, 50], trials_per_electrode=10, success_fraction_threshold=.6)
            (snapshot / "stimulation.yaml").write_text(yaml.safe_dump(dict(probe=probe)), encoding="utf-8")
            counts = np.array([0, 1] * 10)
            atomic_json(pre / "baseline_statistics.json", dict(window_ms=40, windows=20,
                mean=float(counts.mean()), variance=float(counts.var(ddof=1))))
            write_csv(pre / "baseline_background_windows.csv", [dict(start_sec=i * .04, count=c) for i, c in enumerate(counts.tolist())])
            rows = []
            for electrode, successes in ((100, 6), (101, 7)):
                metrics = pre / f"ele_{electrode}" / "trial_metrics.csv"
                response = [21] * successes + [0] * (10 - successes)
                write_csv(metrics, [dict(trial=i + 1, stim_time_sec=i * .5, response_10_50ms=c) for i, c in enumerate(response)])
                rows.append(dict(unit_id="unit_1", electrode=electrode, status="complete", h5=str(metrics.parent / "unused.h5"),
                    trial_metrics_path=str(metrics), success_trials=successes, all_trials=10, response_10_50ms_mean=sum(response) / 10))
            rows.append(dict(unit_id="unit_2", electrode=102, status="unroutable", h5="", success_trials=0, all_trials=0))
            write_csv(pre / "probe_summary.csv", rows)
            originals = {p: sha256(p) for p in source.rglob("*") if p.is_file()}
            with redirect_stdout(io.StringIO()):
                result, _, summaries = review_rate_variance(source, Path(directory) / "review")
            self.assertFalse(summaries[0]["eligible_rate"])
            self.assertTrue(summaries[1]["eligible_rate"])
            self.assertEqual(summaries[2]["all_trials"], 0)
            self.assertEqual(result["eligible_sites_rate"], 1)
            self.assertEqual(result["evaluated_trials"], 20)
            self.assertEqual(originals, {p: sha256(p) for p in source.rglob("*") if p.is_file()})
            self.assertFalse((Path(directory) / "review/experiment_result.json").exists())
            self.assertFalse(result["hardware_contacted"])


if __name__ == "__main__":
    unittest.main()
