"""Joint software routing: real sites preserved, no chip, no fake biological eligibility."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from analysis.utils.io import parse_cfg
from python.prepare_probe_routing import prepare_stimulation_candidate, validate_stimulation_request
from python.utils.persistence import sha256

ROOT = Path(__file__).resolve().parents[1]


def cfg_text(rows, generation=0):
    return ''.join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in rows) + f'\n# generation {generation}\n'


class SoftwareArray:
    def __init__(self, sdk, token):
        self.sdk, self.token = sdk, token
        self.connected, self.mapping, self.generation = set(), [], 0
        self.closed = False

    def reset(self):
        self.connected.clear()
        return 'OK'

    def clear_selected_electrodes(self):
        return 'OK'

    def select_electrodes(self, sites):
        self.sdk.recording_orders.append(list(sites))
        return 'OK'

    def select_stimulation_electrodes(self, sites):
        self.sdk.target_orders.append(list(sites))
        return 'OK'

    def route(self):
        self.sdk.routes += 1
        self.generation = self.sdk.routes
        self.mapping = [dict(r) for r in self.sdk.rows]
        if self.sdk.drop_recording:
            self.mapping = self.mapping[:-1]
        if self.sdk.change_coordinates:
            self.mapping[0]['x'] += 17.5
        if self.sdk.change_channels:
            for row in self.mapping:
                row['channel'] = (row['channel'] + 1) % len(self.mapping)
        return self.sdk.route_ack

    def save_config(self, path):
        Path(path).write_text(cfg_text(self.mapping, self.generation), encoding='utf-8')
        return 0

    def load_config(self, path):
        self.mapping = parse_cfg(path)
        text = Path(path).read_text(encoding='utf-8')
        self.generation = int(text.rsplit('generation ', 1)[-1].strip())
        self.connected.clear()
        self.sdk.loaded.append(Path(path))
        return 'OK'

    def connect_electrode_to_stimulation(self, electrode):
        self.connected.add(electrode)
        return 'OK'

    def disconnect_electrode_from_stimulation(self, electrode):
        self.connected.discard(electrode)
        return 'OK'

    def query_stimulation_at_electrode(self, electrode):
        if electrode not in self.connected:
            return ''
        if self.sdk.invalid_query is not None:
            return self.sdk.invalid_query
        if electrode == self.sdk.unavailable:
            return ''
        # Before the SECOND electrode is connected the first one appears unique.
        # Both must be re-queried after all connections to observe the conflict.
        if self.generation < self.sdk.first_good_generation and len(self.connected) > 1:
            if electrode in self.sdk.sites[:2]:
                return '10'
        return str(self.sdk.sites.index(electrode))

    def get_config(self):
        rows = [dict(r) for r in self.mapping]
        if self.sdk.mapping_changed_by_connect:
            rows[0]['x'] += 17.5
        return SimpleNamespace(mappings=[SimpleNamespace(**r) for r in rows])

    def close(self):
        self.closed = True
        if self.sdk.mutate_source:
            with self.sdk.source.open('a', encoding='utf-8') as handle:
                handle.write('\n# external edit')

    def __getattr__(self, name):
        raise AssertionError(f'Forbidden/unimplemented Array method: {name}')


class SoftwareSDK:
    def __init__(self, source, rows, sites):
        self.source, self.rows, self.sites = source, rows, sites
        self.routes, self.arrays, self.loaded = 0, [], []
        self.recording_orders, self.target_orders = [], []
        self.first_good_generation = 1
        self.drop_recording = self.change_coordinates = self.change_channels = False
        self.mapping_changed_by_connect = self.mutate_source = False
        self.unavailable = self.invalid_query = None
        self.route_ack = 'OK'

    def Array(self, token):
        result = SoftwareArray(self, token)
        self.arrays.append(result)
        return result

    def __getattr__(self, name):
        # Includes initialize/activate/send/download/offset/DAC/Sequence/Saving/StimulationUnit.
        raise AssertionError(f'Forbidden/unimplemented SDK method: {name}')


class FormalRoutingPreparationChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / 'data').mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=ROOT / 'data')
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        self.rows = [dict(channel=i, electrode=100+i, x=i*17.5, y=0.) for i in range(12)]
        self.sites = [100, 101, 104, 105, 108, 109]
        self.source = self.path / 'source.cfg'
        self.source.write_text(cfg_text(self.rows), encoding='utf-8')
        self.before = sha256(self.source)
        self.sdk = SoftwareSDK(self.source, self.rows, self.sites)
        self.output = self.path / 'output'
        self.messages = []

    def run_candidate(self, **kwargs):
        result = prepare_stimulation_candidate(self.source, self.sites, self.output, self.sdk,
                                               emit=lambda msg, **kw: self.messages.append(msg), **kwargs)
        self.assertEqual(self.before, sha256(self.source))
        self.assertTrue(all(a.closed for a in self.sdk.arrays))
        self.assertEqual(len({a.token for a in self.sdk.arrays}), len(self.sdk.arrays))
        for flag in ('hardware_downloaded', 'hardware_tested', 'stimulation_sent',
                     'configuration_activated', 'formal_started', 'probe_evidence_reused'):
            self.assertFalse(result[flag])
        self.assertEqual(json.loads((self.output/'routing_preparation.json').read_text())['status'], result['status'])
        for order in self.sdk.recording_orders:
            self.assertEqual(set(order), {r['electrode'] for r in self.rows})
        for order in self.sdk.target_orders:
            self.assertEqual(set(order), set(self.sites))
        return result

    def test_conflicting_original_fixed_without_replacing_any_site_and_reloaded(self):
        r = self.run_candidate()
        self.assertFalse(r['original_check']['independent'])
        self.assertEqual(r['original_check']['conflicts'][0]['electrodes'], [100, 101])
        self.assertEqual(r['status'], 'candidate_for_hardware_routing_check')
        self.assertEqual(set(map(int, r['joint_check']['pool'])), set(self.sites))
        self.assertEqual(len(set(r['joint_check']['pool'].values())), 6)
        self.assertTrue(r['baseline_mapping_reusable'])
        self.assertTrue(r['fresh_probe_required'])
        self.assertFalse(r['automatic_handoff_allowed'])
        self.assertEqual(len(self.sdk.arrays), 3)
        self.assertIn(self.output/'attempt_01.cfg', self.sdk.loaded)
        self.assertEqual(sha256(r['candidate_cfg']), r['candidate_cfg_sha256'])
        self.assertEqual(len(list(csv.DictReader((self.output/'joint_pool.csv').open(encoding='utf-8-sig')))), 6)

    def test_already_independent_original_is_not_rerouted(self):
        self.sdk.first_good_generation = 0
        r = self.run_candidate()
        self.assertEqual(r['status'], 'original_cfg_already_independent')
        self.assertIsNone(r['candidate_cfg'])
        self.assertEqual(self.sdk.routes, 0)
        self.assertEqual(len(self.sdk.arrays), 1)

    def test_conflicts_across_attempts_are_bounded_and_do_not_claim_impossibility(self):
        self.sdk.first_good_generation = 99
        r = self.run_candidate(max_attempts=4)
        self.assertEqual(r['status'], 'no_solution_found')
        self.assertEqual(self.sdk.routes, 4)
        self.assertTrue(all(x['status']=='rejected_joint_pool' for x in r['attempts']))
        self.assertFalse((self.output/'candidate_formal.cfg').exists())
        self.assertNotEqual(self.sdk.recording_orders[0], self.sdk.recording_orders[1])

    def test_channel_changes_reported_and_old_baseline_not_marked_reusable(self):
        self.sdk.change_channels = True
        r = self.run_candidate()
        self.assertFalse(r['baseline_mapping_reusable'])
        self.assertEqual(len(r['mapping_comparison']['changed_channel_electrodes']), 12)
        self.assertTrue(r['same_recording_electrodes'])

    def test_dropped_recording_electrodes_rejected_even_when_stim_sites_retained(self):
        self.sdk.drop_recording = True
        r = self.run_candidate(max_attempts=2)
        self.assertEqual(r['status'], 'no_solution_found')
        self.assertTrue(all(x['status']=='rejected_recording_electrodes_changed' for x in r['attempts']))

    def test_changed_physical_coordinates_rejected(self):
        self.sdk.change_coordinates = True
        r = self.run_candidate(max_attempts=1)
        self.assertEqual(r['status'], 'no_solution_found')
        self.assertEqual(r['attempts'][0]['mapping_comparison']['changed_coordinate_electrodes'], [100])

    def test_connection_changes_mapping_is_not_accepted(self):
        self.sdk.mapping_changed_by_connect = True
        r = self.run_candidate(max_attempts=1)
        self.assertEqual(r['status'], 'no_solution_found')
        self.assertFalse(r['attempts'][0]['joint_check']['recording_mapping_unchanged_by_connect'])

    def test_unavailable_site_never_replaced_with_neighbor(self):
        self.sdk.unavailable = 100
        r = self.run_candidate(max_attempts=1)
        self.assertEqual(r['status'], 'no_solution_found')
        self.assertEqual(r['attempts'][0]['joint_check']['unavailable_electrodes'], [100])

    def test_failed_ack_closes_array_writes_failure_and_stops(self):
        self.sdk.route_ack = 'ERROR'
        with self.assertRaisesRegex(RuntimeError, '计算候选软件路由'):
            prepare_stimulation_candidate(self.source, self.sites, self.output, self.sdk, emit=lambda *a, **k: None)
        r = json.loads((self.output/'routing_preparation.json').read_text())
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(self.sdk.routes, 1)
        self.assertTrue(all(a.closed for a in self.sdk.arrays))

    def test_source_changes_invalidate_result(self):
        self.sdk.mutate_source = True
        with self.assertRaisesRegex(RuntimeError, '来源CFG发生变化'):
            prepare_stimulation_candidate(self.source, self.sites, self.output, self.sdk, emit=lambda *a, **k: None)
        r = json.loads((self.output/'routing_preparation.json').read_text())
        self.assertEqual(r['status'], 'source_changed')
        self.assertIsNone(r['candidate_cfg'])

    def test_invalid_inputs_rejected_before_server_or_output(self):
        for sites, attempts in (([],4), ([100,100],4), ([True],4), ([999],4), ([100],True), ([100],17)):
            with self.subTest(sites=sites, attempts=attempts), self.assertRaises(ValueError):
                validate_stimulation_request(self.source, sites, attempts)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(self.sdk.arrays), 0)

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        marker = self.output/'keep.txt'; marker.write_text('keep')
        with self.assertRaises(FileExistsError):
            prepare_stimulation_candidate(self.source, self.sites, self.output, self.sdk)
        self.assertEqual(marker.read_text(), 'keep')
        self.assertEqual(len(self.sdk.arrays), 0)

    def test_fixed_command_entry_uses_top_fields_and_does_not_read_run_configs(self):
        spec = importlib.util.spec_from_file_location('formal_routing_entry', ROOT/'scripts/prepare_formal_routing.py')
        entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
        entry.CFG_PATH, entry.STIMULATION_ELECTRODES = str(self.source), self.sites
        entry.OUTPUT_ROOT, entry.MAX_ATTEMPTS = str(self.path/'entry_output'), 1
        with patch.dict('sys.modules', {'maxlab':self.sdk}), patch('builtins.print'):
            code = entry.main()
        self.assertEqual(code, 0)
        reports = list((self.path/'entry_output').glob('*/candidate/routing_preparation.json'))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text())['requested_electrodes'], self.sites)
        self.assertEqual(sha256(self.source), self.before)


if __name__ == '__main__':
    unittest.main()
