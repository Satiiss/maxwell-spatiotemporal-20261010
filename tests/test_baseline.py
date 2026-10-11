"""Software contracts for offline pre-experiment network analysis."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from datetime import datetime
import json
import hashlib

import h5py
import numpy as np
import yaml

from tools.analyze_baseline import ROOT, validate_config
from tools import analyze_baseline as cli
from analysis.utils.io import Session, read_session
from analysis.utils.spontaneous import (histogram_peak_ratio, first_activation_matrix,
    directed_delay_edges, analyze_spontaneous_network, select_network_candidates)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((ROOT/"config/baseline_analysis.yaml").read_text(encoding="utf-8"))

    def test_strict_shared_burst_and_peak_threshold(self):
        cfg = dict(self.config["network"], pair_min_shared_bursts=22)
        delays = np.array([10.2]*20+[15.2, 20.2, 25.2])
        first = np.array([np.zeros(len(delays)), delays])
        edges = directed_delay_edges(first, [0, 1], {0: 100, 1: 200}, cfg)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["delay_peak_ratio"], 20)
        self.assertEqual((edges[0]["source_electrode"], edges[0]["target_electrode"]), (100, 200))
        self.assertEqual(directed_delay_edges(first, [0, 1], {0: 100, 1: 200},
            dict(cfg, pair_min_shared_bursts=23)), [])
        self.assertEqual(directed_delay_edges(first, [0, 1], {0: 100, 1: 200},
            dict(cfg, pair_peak_ratio_threshold=20.01)), [])

    def test_negative_delay_reverses_direction_and_strict_delay_boundary(self):
        delays = np.array([10.2]*20+[15.2, 20.2, 25.2])
        first = np.array([delays, np.zeros(len(delays))])
        cfg = dict(self.config["network"], pair_min_shared_bursts=22)
        edge = directed_delay_edges(first, [0, 1], {0: 100, 1: 200}, cfg)[0]
        self.assertEqual((edge["source_electrode"], edge["target_electrode"]), (200, 100))
        self.assertEqual(directed_delay_edges(first, [0, 1], {0: 100, 1: 200},
            dict(cfg, pair_min_abs_delay_ms=edge["delay_peak_ms"])), [])

    def test_histogram_legacy_median_includes_peak(self):
        # Preserve the documented legacy statistic, even for a perfectly narrow peak.
        self.assertEqual(histogram_peak_ratio([8]*40, 1, True)[0], 1)

    def test_full_graph_has_no_eighty_edge_display_cap(self):
        first = np.repeat(np.arange(15)[:, None]*10.0, 40, axis=1)
        cfg = dict(self.config["network"], pair_peak_ratio_threshold=1)
        edges = directed_delay_edges(first, list(range(15)), dict(enumerate(range(100, 115))), cfg)
        self.assertEqual(len(edges), 105)
        self.assertEqual(sum(e["source_electrode"] == 100 for e in edges), 14)

    def test_first_activation_and_absent_channel(self):
        first = first_activation_matrix({0: np.array([1.01, 1.02, 3.01])}, [0, 1], [(1, 1.1), (3, 3.1)])
        np.testing.assert_allclose(first[0], [10, 10])
        self.assertTrue(np.isnan(first[1]).all())

    def test_rates_use_record_duration_and_quiet_mapping_is_retained(self):
        mapping = [dict(channel=i, electrode=100+i, x=i*17.5, y=0) for i in range(3)]
        session = Session(Path("fixture.h5"), np.array([1., 3.]), np.array([0, 1]), mapping,
                          20000, 12345, 600, [], 0, np.array([-100., -200.]))
        with patch("analysis.utils.spontaneous.detect_burst_intervals", return_value=([], {})):
            result = analyze_spontaneous_network(session, self.config["burst"], self.config["network"])
        self.assertEqual(len(result["metrics"]), 3)
        by_channel = {r["channel"]: r for r in result["metrics"]}
        self.assertEqual(by_channel[0]["firing_rate_hz"], 1/600)
        self.assertEqual(by_channel[1]["mean_abs_amplitude"], 200)
        self.assertEqual(by_channel[2]["spike_count"], 0)
        self.assertEqual(result["metrics"][0]["electrode"], 101)

    def test_center_distance_and_disjoint_units(self):
        rows = []
        for unit, x in enumerate([0, 52.5, 160]):
            for site, dx in enumerate([0, 2, 4, 6]):
                rows.append(dict(electrode=unit*10+site, channel=unit*10+site, x_um=x+dx, y_um=0,
                    out_degree=20-unit if site == 0 else 0, in_degree=0, firing_rate_hz=1, mean_abs_amplitude=100))
        candidates = select_network_candidates(list(reversed(rows)), self.config["candidates"])
        self.assertEqual([r["electrode"] for r in candidates], [0, 20])
        self.assertEqual(candidates[0]["unit_electrodes"], [0, 1, 2, 3])
        self.assertFalse(set(candidates[0]["unit_electrodes"]) & set(candidates[1]["unit_electrodes"]))

    def test_manual_bursts_reject_overlap_or_outside_recording(self):
        session = Session(Path("fixture.h5"), np.array([]), np.array([], dtype=int),
                          [dict(channel=0, electrode=1, x=0, y=0)], 20000, 0, 600, [], 0)
        for intervals in [[(0, 1), (1, 2)], [(599, 601)]]:
            with self.assertRaises(ValueError):
                analyze_spontaneous_network(session, self.config["burst"], self.config["network"], burst_intervals=intervals)

    def test_config_does_not_weaken_fixed_review_or_spacing_rules(self):
        validate_config(self.config)
        for key, value in [("minimum_center_distance_um", 52), ("manual_review_below_peak_outdegree", 9)]:
            changed = deepcopy(self.config)
            changed["candidates"][key] = value
            with self.assertRaises(ValueError):
                validate_config(changed)

    def test_h5_amplitudes_follow_time_sort_and_true_origin(self):
        (ROOT/"data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/"data") as directory:
            path = Path(directory)/"fixture.h5"
            with h5py.File(path, "w") as handle:
                seg = handle.create_group("data_store/data0000")
                seg.create_dataset("settings/sampling", data=[20000.])
                mapping = np.array([(0, 100, 0., 0.)], dtype=[("channel", "i4"), ("electrode", "i4"), ("x", "f4"), ("y", "f4")])
                seg.create_dataset("settings/mapping", data=mapping)
                seg.create_dataset("groups/routed/frame_nos", data=[50000, 12549999])
                spikes = np.array([(90000, 0, -90.), (70000, 0, -70.)],
                    dtype=[("frameno", "i8"), ("channel", "i4"), ("amplitude", "f4")])
                seg.create_dataset("spikes", data=spikes)
            session = read_session(path)
            self.assertEqual(session.duration_sec, 625)
            self.assertEqual(session.frame_origin, 50000)
            np.testing.assert_equal(session.times, [1, 2])
            np.testing.assert_equal(session.amplitudes, [-70, -90])

    def test_review_history_and_read_only_inputs(self):
        (ROOT/"data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/"data") as directory:
            root = Path(directory)
            h5, cfg, parameters = root/"input.h5", root/"input.cfg", root/"parameters.yaml"
            h5.write_bytes(b"not read: reader mocked for review-state test")
            cfg.write_text("0(1)0/0;", encoding="utf-8")
            parameters.write_text(yaml.safe_dump(self.config), encoding="utf-8")
            inputs = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (h5, cfg)]
            session = Session(h5, np.array([]), np.array([], dtype=int),
                [dict(channel=0, electrode=1, x=0, y=0)], 20000, 0, 600, [], 0)
            args = SimpleNamespace(h5=str(h5), cfg=str(cfg), config=str(parameters), bursts_csv=None,
                well=0, previous_run=None, keep_low_outdegree=False, review_reason="")
            with patch.object(cli, "ROOT", root), patch.object(cli, "read_session", return_value=session), \
                    patch.object(cli, "render_network_figures", return_value=[]), patch.object(cli, "datetime") as clock:
                clock.now.return_value = datetime(2000, 1, 1, 0, 0, 1)
                with self.assertLogs("baseline_analysis", level="INFO"):
                    first = cli.run(args)
                state = json.loads((first/"workflow_state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["phase"], "REVIEW_NETWORK_PARAMETERS")
                self.assertTrue((first/"network/stable_delay_edges.csv").read_text(encoding="utf-8-sig").startswith("source_channel"))
                args.previous_run, args.keep_low_outdegree, args.review_reason = str(first), True, "manual test review"
                clock.now.return_value = datetime(2000, 1, 1, 0, 0, 2)
                with self.assertLogs("baseline_analysis", level="INFO"):
                    second = cli.run(args)
                history = json.loads((second/"network/network_parameter_history.json").read_text(encoding="utf-8"))
                self.assertEqual([r["revision"] for r in history], [1, 2])
                self.assertEqual(history[-1]["decision"], "keep_current")
                self.assertEqual(history[-1]["reason"], "manual test review")
                self.assertEqual(json.loads((second/"workflow_state.json").read_text(encoding="utf-8"))["phase"], "INSUFFICIENT_CANDIDATES")
                changed = deepcopy(self.config)
                changed["burst"]["threshold_z"] = 7
                parameters.write_text(yaml.safe_dump(changed), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "参数只读"):
                    cli.run(args)
            self.assertEqual(inputs, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (h5, cfg)])


if __name__ == "__main__":
    unittest.main()
