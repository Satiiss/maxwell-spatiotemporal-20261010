"""Network and Probe plots must show the same physical coordinates and Y direction."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import matplotlib.pyplot as plt
import numpy as np

from analysis.utils.network_plots import render_network_figures
from python.figures import unit_map


class SpatialOrientationTests(unittest.TestCase):
    def test_network_and_probe_preserve_coordinates_and_y_down(self):
        mapping = [dict(channel=i, electrode=100+i, x=x, y=y)
                   for i, (x, y) in enumerate([(20., 30.), (60., 40.), (40., 90.), (100., 130.)])]
        metrics = [dict(electrode=r['electrode'], x_um=r['x'], y_um=r['y'],
                        out_degree=4-i, in_degree=i, burst_firing_rate_hz=1.)
                   for i, r in enumerate(mapping)]
        candidates = [dict(metrics[0], rank=1, unit_electrodes=[100, 101, 102, 103])]
        session = SimpleNamespace(times=np.array([.1]), channels=np.array([0]), duration_sec=1.)
        result = dict(metrics=metrics, burst_diagnostics={}, bursts=[], edges=[])
        config = dict(star_count=6, figure_formats=['png'], dpi=600,
                      raster_max_display_spikes=100, arrow_alpha=.2)
        captured = {}

        def capture(fig, filename, **kwargs):
            name = Path(filename).stem
            if name.endswith('_map') or name in ('spontaneous_network_all_edges',
                                                 'spontaneous_network_selected_sites', 'probe_units'):
                ax = fig.axes[0]
                captured[name] = dict(inverted=ax.yaxis_inverted(),
                    offsets=np.asarray(ax.collections[0].get_offsets()),
                    annotations=[t.get_text() for t in ax.texts],
                    small_y_screen=ax.transData.transform((20., 30.))[1],
                    large_y_screen=ax.transData.transform((100., 130.))[1])

        root = Path(__file__).resolve().parents[1] / 'data'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory, patch.object(plt.Figure, 'savefig', capture):
            render_network_figures(session, result, candidates, directory, config)
            unit_map(mapping, [dict(unit_id='candidate_01', center=100,
                                   electrodes=[100, 101, 102, 103])], Path(directory)/'probe_units')
        self.assertEqual(len(captured), 5)
        expected = np.array([[r['x'], r['y']] for r in mapping])
        for name, figure in captured.items():
            with self.subTest(figure=name):
                self.assertTrue(figure['inverted'])
                self.assertGreater(figure['small_y_screen'], figure['large_y_screen'])
                np.testing.assert_allclose(figure['offsets'], expected)
        self.assertIn('#1: 100', captured['spontaneous_network_selected_sites']['annotations'])
        self.assertIn('#1: 100', captured['probe_units']['annotations'])


if __name__ == '__main__':
    unittest.main()
