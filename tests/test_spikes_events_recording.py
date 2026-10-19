"""Storage mode must preserve all spike channels and exact stimulus-frame timing."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import h5py
import numpy as np
import yaml

from main import ROOT, execute, load_configuration, make_plan, validate
from analysis.utils.io import read_session, BOUNDARY_IDS, BOUNDARY_PREFIX
from python.formal_selection import validate_formal_selection
from python.maxwell_setup import MaxLabBackend
from python.mock_backend import MockBackend, MAP_DTYPE, SPIKE_DTYPE, EVENT_DTYPE, synthetic_mapping
from python.utils.persistence import sha256
from python.utils.progress import Reporter


class SpikesEventsChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / 'data').mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=ROOT / 'data')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.system, self.stim = load_configuration(ROOT / 'config')
        self.system['culture'].update(id='storage_fixture', div=20)
        self.system['data']['root'] = str(self.root / 'runs')
        self.mapping, centers = synthetic_mapping()
        self.cfg = self.root / 'recording.cfg'
        self.cfg.write_text(''.join(f"{r['channel']}({r['electrode']}){r['x']}/{r['y']};" for r in self.mapping))
        self.system['electrode_map']['cfg_path'] = str(self.cfg)
        self.selection = dict(culture_id='storage_fixture', recording_mode='spikes_events',
            units=[dict(name=f'manual_{i}', electrodes=[c, c+1], selection_reason='Test manual selection')
                   for i, c in enumerate(centers[:3])])
        self.selection_file = self.root / 'selection.json'
        self.selection_file.write_text(json.dumps(self.selection))

    def fixture(self, mode='valid', raw=False):
        path = self.root / (mode + '.h5')
        origin, token = 123456789, 'a'*32
        events = [(origin, 1, BOUNDARY_IDS['start'], f'{BOUNDARY_PREFIX} start token {token}'.encode()),
                  (origin+40000, 1, 7, b'condition test'),
                  (origin+200000, 1, BOUNDARY_IDS['end'], f'{BOUNDARY_PREFIX} end token {token}'.encode())]
        if mode == 'missing_end': events.pop()
        if mode == 'missing_start': events.pop(0)
        if mode == 'duplicate': events.append(events[0])
        if mode == 'wrong_token': events[-1] = (*events[-1][:3], f'{BOUNDARY_PREFIX} end token {"b"*32}'.encode())
        if mode == 'reversed': events[-1] = (origin-1, *events[-1][1:])
        if mode == 'wrong_id': events[0] = (origin, 1, 6, events[0][3])
        if mode == 'none': events = events[1:2]
        with h5py.File(path, 'w') as f:
            seg = f.create_group('data_store/data0000')
            seg.create_dataset('well_id', data=[0])
            seg.create_dataset('settings/sampling', data=[20000])
            seg.create_dataset('settings/mapping', data=np.array([tuple(r[k] for k in MAP_DTYPE.names) for r in self.mapping], dtype=MAP_DTYPE))
            seg.create_dataset('spikes', data=np.array([(origin-1, 0, -1), (origin+41000, 0, -2),
                                                      (origin+200000, 1, -3)], dtype=SPIKE_DTYPE))
            seg.create_dataset('events', data=np.array(events, dtype=EVENT_DTYPE))
            if raw:
                seg.create_dataset('groups/exp/frame_nos', data=[origin, origin+199999])
        return path

    def test_spikes_only_origin_is_h5_marker_not_first_spike_or_host_time(self):
        session = read_session(self.fixture(), self.cfg)
        self.assertEqual(session.time_origin_source, 'h5_boundary_events')
        self.assertEqual(session.frame_origin, 123456789)
        self.assertEqual(session.duration_sec, 10.)
        np.testing.assert_allclose(session.times, [2.05])
        self.assertEqual(session.amplitudes.tolist(), [-2.])
        self.assertEqual(next(e['time_sec'] for e in session.events if e['event_id'] == 7), 2.)
        self.assertEqual(len(session.mapping), len(self.mapping))
        self.assertEqual(session.unmapped_spike_count, 0)

    def test_missing_duplicate_mismatched_and_reversed_markers_cannot_fall_back(self):
        for mode in ('missing_start','missing_end','duplicate','wrong_token','reversed','wrong_id','none'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                read_session(self.fixture(mode), self.cfg)
        with self.assertRaises(ValueError):
            read_session(self.fixture('missing_end', raw=True), self.cfg)

    def backend(self, raw=False):
        system = copy.deepcopy(self.system)
        system['maxwell']['raw_recording'] = raw
        backend = MaxLabBackend(system, Reporter())
        backend.mx = MagicMock()
        backend.mx.send.return_value = 'OK'
        backend.mx.Timing.waitAfterRecording = 2
        backend.mx.Saving.return_value.group_define.return_value = 'OK'
        backend.array = MagicMock()
        backend.mapping = self.mapping
        return backend

    def test_sdk_low_storage_skips_raw_groups_and_uses_only_boundary_event_commands(self):
        backend = self.backend()
        with patch('python.maxwell_setup.time.sleep') as sleep:
            backend.start_segment(self.root, 'check')
            backend._close_recording()
        backend.mx.Saving.return_value.group_define.assert_not_called()
        self.assertEqual(backend.mx.Event.call_count, 2)
        ids = [c.args[2] for c in backend.mx.Event.call_args_list]
        self.assertEqual(ids, [BOUNDARY_IDS['start'], BOUNDARY_IDS['end']])
        self.assertIn(unittest.mock.call(1.0), sleep.call_args_list)
        self.assertIn(unittest.mock.call(2), sleep.call_args_list)
        backend.mx.Sequence.assert_not_called()
        backend.mx.DAC.assert_not_called()
        backend.mx.initialize.assert_not_called()
        backend.mx.Saving.return_value.stop_recording.assert_called_once()
        backend.mx.Saving.return_value.stop_file.assert_called_once()
        self.assertFalse(backend.recording)
        self.assertFalse(backend.file_open)

    def test_sdk_raw_mode_keeps_all_channels_and_has_no_boundary_markers(self):
        backend = self.backend(raw=True)
        with patch('python.maxwell_setup.time.sleep'):
            backend.start_segment(self.root, 'check')
            backend._close_recording()
        channels = backend.mx.Saving.return_value.group_define.call_args.args[2]
        self.assertEqual(channels, [r['channel'] for r in self.mapping])
        backend.mx.Event.assert_not_called()

    def test_boundary_write_failure_still_closes_recording_and_file(self):
        backend = self.backend()
        with patch('python.maxwell_setup.time.sleep'):
            backend.start_segment(self.root, 'check')
            backend.mx.send.side_effect = RuntimeError('end marker failed')
            with self.assertRaisesRegex(RuntimeError, 'end marker failed'):
                backend._close_recording()
        backend.mx.Saving.return_value.stop_recording.assert_called_once()
        backend.mx.Saving.return_value.stop_file.assert_called_once()

    def test_explicit_selection_mode_changes_only_runtime_and_raw_estimate_not_protocol(self):
        original = copy.deepcopy((self.system, self.stim))
        before = sha256(self.cfg), sha256(self.selection_file)
        with patch('python.maxwell_setup.MaxLabBackend') as sdk:
            out = execute(self.system, self.stim, formal_selection=self.selection_file, dry_run=True)
        sdk.assert_not_called()
        plan = json.loads((out/'experiment_plan.json').read_text())
        self.assertEqual(plan['formal_trial_count'], 5400)
        self.assertEqual(plan['recording_mode'], 'spikes_events')
        self.assertEqual(plan['raw_uncompressed_bytes_at_1024_channels_20khz'], 0)
        self.assertGreater(plan['full_raw_reference_bytes_at_1024_channels_20khz'], 400e9)
        self.assertIsNone(plan['spikes_events_size_estimate_bytes'])
        self.assertEqual(plan['formal_nominal_duration_sec'], make_plan(self.system, self.stim)['formal_nominal_duration_sec'])
        snapshot = yaml.safe_load((out/'config_snapshot/system.yaml').read_text(encoding='utf-8'))
        self.assertFalse(snapshot['maxwell']['raw_recording'])
        self.assertEqual((self.system,self.stim), original)
        self.assertEqual((sha256(self.cfg),sha256(self.selection_file)), before)
        wrong = dict(self.selection, recording_mode='compressed_raw')
        with self.assertRaisesRegex(ValueError,'recording_mode'):
            validate_formal_selection(wrong,self.system,self.stim,self.root)
        snapshot['maxwell']['raw_recording'] = False
        self.stim['probe']['threshold_rule'] = 'paired_pre_stim_ratio'
        from run_probe import configure_probe
        snapshot, stimulation = configure_probe(snapshot,self.stim,[self.mapping[0]['electrode']])
        with self.assertRaisesRegex(ValueError,'raw_recording'):
            validate(snapshot,stimulation,probe_only=True)

    def mocked(self):
        system, stim = copy.deepcopy((self.system,self.stim))
        system['mock']['trials_per_condition_override'] = 1
        stim['formal'].update(pre_spontaneous_sec=1, post_spontaneous_sec=1,
                             rest_between_stages_sec=0,rest_after_last_stage_sec=0)
        return system, stim

    def test_formal_mock_keeps_three_h5s_all_spike_channels_and_verified_stimulus_events(self):
        system, stim = self.mocked()
        with patch('python.experiment_runner.unit_map'):
            out = execute(system,stim,formal_selection=self.selection_file)
        result = json.loads((out/'experiment_result.json').read_text())
        self.assertEqual(result['formal_trial_count'],108)
        self.assertEqual(result['probe_trial_count'],0)
        self.assertFalse(result['raw_recording'])
        files = list((out/'raw_data').rglob('*.raw.h5'))
        self.assertEqual(len(files),3)
        for path in files:
            session = read_session(path,self.cfg)
            self.assertEqual(session.time_origin_source,'h5_boundary_events')
            self.assertEqual(len(session.mapping),len(self.mapping))
            with h5py.File(path) as f:
                self.assertNotIn('groups',f['data_store/data0000'])
            meta = json.loads((path.parent/'recording_interval.json').read_text(encoding='utf-8'))
            self.assertFalse(meta['raw_recording'])
        stim_path = next(p for p in files if p.parent.name=='02_stim')
        timing = json.loads((stim_path.parent/'segment_time_meta.json').read_text())
        self.assertTrue(timing['verified'])
        self.assertEqual(timing['pulse_count'],204)
        self.assertEqual(timing['time_origin_source'],'h5_boundary_events')

    def test_missing_first_segment_boundary_stops_before_any_formal_sequence(self):
        original = MockBackend.finish_segment
        def missing(backend):
            path = original(backend)
            with h5py.File(path,'r+') as f:
                seg=f['data_store/data0000']
                rows=seg['events'][:]
                del seg['events']
                seg.create_dataset('events',data=rows[rows['eventid']!=BOUNDARY_IDS['end']])
            return path
        system, stim = self.mocked()
        with patch.object(MockBackend,'finish_segment',missing), \
             patch.object(MockBackend,'prepare_sequence',side_effect=AssertionError('No stimulation')) as sequence, \
             patch('python.experiment_runner.unit_map'):
            with self.assertRaisesRegex(ValueError,'起止帧'):
                execute(system,stim,formal_selection=self.selection_file)
        sequence.assert_not_called()

    def test_accidental_raw_group_stops_before_any_formal_sequence(self):
        original = MockBackend.finish_segment
        def unexpected(backend):
            path = original(backend)
            with h5py.File(path,'r+') as f:
                f['data_store/data0000'].create_dataset('groups/exp/raw',data=[[1]])
            return path
        system, stim = self.mocked()
        with patch.object(MockBackend,'finish_segment',unexpected), \
             patch.object(MockBackend,'prepare_sequence',side_effect=AssertionError('No stimulation')) as sequence, \
             patch('python.experiment_runner.unit_map'):
            with self.assertRaisesRegex(ValueError,'raw波形'):
                execute(system,stim,formal_selection=self.selection_file)
        sequence.assert_not_called()

    def test_reserved_marker_ids_cannot_be_used_for_stimulation(self):
        from python.schedule import Condition
        backend = self.backend()
        with self.assertRaisesRegex(ValueError,'保留ID'):
            backend.prepare_sequence(Condition('AB','temporal',(1,2),('A','B'),1),[],
                                     BOUNDARY_IDS['start']-1,self.stim['protocol'])
        backend.mx.Sequence.assert_not_called()

    def test_raw_probe_evidence_is_preserved_when_formal_storage_changes(self):
        from python.utils.persistence import atomic_json, write_csv
        source = self.root/'probe'
        (source/'config_snapshot').mkdir(parents=True)
        for name, value in (('system',self.system),('stimulation',self.stim)):
            (source/f'config_snapshot/{name}.yaml').write_text(yaml.safe_dump(value),encoding='utf-8')
        atomic_json(source/'source_inputs.json',dict(cfg_sha256=sha256(self.cfg)))
        atomic_json(source/'experiment_result.json',dict(status='probe_review_required',synthetic=True,
                                                         probe_trial_count=60))
        self.selection['units'] = [dict(source_run=str(source),source_unit=f'candidate_{i:02d}',
            electrodes=u['electrodes'],exceptions={}) for i,u in enumerate(self.selection['units'],1)]
        write_csv(source/'pre_experiment/candidate_sites.csv',[
            dict(unit_id=u['source_unit'],electrodes=';'.join(map(str,u['electrodes'])))
            for u in self.selection['units']])
        write_csv(source/'pre_experiment/probe_summary.csv',[
            dict(unit_id=u['source_unit'],electrode=e,status='complete',success_trials=6,all_trials=10,
                 success_fraction=.6,eligible=True,response_10_50ms_mean=20)
            for u in self.selection['units'] for e in u['electrodes']])
        self.selection_file.write_text(json.dumps(self.selection))
        before={p:sha256(p) for p in source.rglob('*') if p.is_file()}
        out=execute(self.system,self.stim,formal_selection=self.selection_file,dry_run=True)
        self.assertEqual(before,{p:sha256(p) for p in source.rglob('*') if p.is_file()})
        saved=yaml.safe_load((out/'config_snapshot/system.yaml').read_text(encoding='utf-8'))
        self.assertFalse(saved['maxwell']['raw_recording'])
        audit=json.loads((out/'formal_selection.json').read_text(encoding='utf-8'))
        self.assertTrue(audit['units'][0]['site_metrics'][0]['source_eligible'])


if __name__=='__main__':
    unittest.main()
