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

    def json_fixture(self, mode='valid', raw=False, stimulus=False):
        path = self.fixture(raw=raw)
        origin, token = 123456789, 'a'*32
        start = {BOUNDARY_PREFIX:'start', 'token':token}
        end = {BOUNDARY_PREFIX:'end', 'token':token}
        start_id = BOUNDARY_IDS['start']
        if mode == 'wrong_token': end['token'] = 'b'*32
        if mode == 'missing_token': del start['token']
        if mode == 'wrong_role': start[BOUNDARY_PREFIX] = ['start']
        if mode == 'reserved_unrelated': start = {'other':'start'}
        if mode == 'wrong_id': start_id = 42
        start_message = json.dumps(start)+'\n'
        if mode == 'duplicate_key':
            start_message = '{"recording_boundary_v1":"end","recording_boundary_v1":"start","token":"'+token+'"}'
        if mode == 'malformed': start_message = '{"recording_boundary_v1":'
        events = [(origin,1,start_id,start_message),
                  (origin+60074,1,BOUNDARY_IDS['end'],json.dumps(end)+'\n')]
        if mode == 'missing_end': events.pop()
        if mode == 'duplicate_start': events.append(events[0])
        if stimulus: events.insert(1,(origin+40000,1,7,json.dumps({'condition':'test','pulse':'1'})))
        dtype = np.dtype([('frameno','<i8'),('eventtype','<u4'),('eventid','<u4'),
                          ('eventmessage',h5py.string_dtype('utf-8'))])
        with h5py.File(path,'r+') as handle:
            del handle['data_store/data0000/events']
            handle['data_store/data0000'].create_dataset('events',data=np.array(events,dtype=dtype))
        return path

    def test_sdk_json_vlen_messages_define_same_exact_hardware_frame_origin(self):
        path = self.json_fixture(stimulus=True)
        before = sha256(path)
        session = read_session(path,self.cfg)
        self.assertEqual(session.time_origin_source,'h5_boundary_events')
        self.assertEqual(session.frame_origin,123456789)
        self.assertAlmostEqual(session.duration_sec,3.0037)
        np.testing.assert_allclose(session.times,[2.05])
        self.assertEqual(session.amplitudes.tolist(),[-2.])
        self.assertEqual(next(e['time_sec'] for e in session.events if e['event_id']==7),2.)
        self.assertEqual(json.loads(session.events[0]['eventmessage'])[BOUNDARY_PREFIX],'start')
        self.assertEqual(sha256(path),before)

    def test_invalid_json_boundary_messages_never_fall_back_to_raw_frames(self):
        for mode in ('wrong_token','missing_token','wrong_role','reserved_unrelated',
                     'wrong_id','duplicate_key','malformed','missing_end','duplicate_start'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                read_session(self.json_fixture(mode,raw=True),self.cfg)

    def test_readonly_short_check_accepts_sdk_json_without_sdk_or_input_writes(self):
        import contextlib
        import io
        from tools import check_formal_recording as tool
        path = self.json_fixture()
        before = sha256(path),sha256(self.cfg),set(self.root.iterdir())
        output = io.StringIO()
        with patch.object(tool.runpy,'run_path',return_value=dict(CONFIG_DIR='config')), \
             patch.object(tool,'load_configuration',return_value=(self.system,self.stim)), \
             patch.object(tool,'check_environment') as environment, \
             patch.object(tool,'check_recording') as recording, \
             patch.object(tool,'MaxLabBackend') as backend, \
             patch('sys.argv',['check_formal_recording.py','--h5',str(path)]), \
             contextlib.redirect_stdout(output):
            tool.main()
        environment.assert_not_called()
        recording.assert_not_called()
        backend.assert_not_called()
        self.assertIn('存储检查通过',output.getvalue())
        self.assertIn('h5_boundary_events',output.getvalue())
        self.assertEqual((sha256(path),sha256(self.cfg),set(self.root.iterdir())),before)

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
        backend.mx.send_raw.return_value = 'OK'
        def new_sequence(name, **kwargs):
            seq = MagicMock(token=name)
            seq.append.return_value = seq
            return seq
        backend.mx.Sequence.side_effect = new_sequence
        backend.mx.Event.side_effect = lambda well, kind, eid, props: MagicMock(
            set=lambda: f'system_trigger_event {well} {kind} {eid} {props}')
        backend.mx.Timing.waitAfterRecording = 2
        backend.mx.Saving.return_value.group_define.return_value = 'OK'
        backend.array = MagicMock()
        backend.mapping = self.mapping
        return backend

    def test_sdk_low_storage_uses_event_only_sequences_and_skips_raw_groups(self):
        backend = self.backend()
        # Reproduce the real SDK's empty acknowledgement for direct Event sends.
        backend.mx.send.return_value = ''
        with patch('python.maxwell_setup.time.sleep') as sleep:
            backend.start_segment(self.root, 'check')
            self.assertEqual(len(backend.boundary_sequences), 1)
            retained = []
            def writer_stopped():
                # Neither marker sequence may be destroyed before saver closure.
                self.assertEqual(len(backend.boundary_sequences), 2)
                retained.extend(backend.boundary_sequences)
            backend.mx.Saving.return_value.stop_file.side_effect = writer_stopped
            backend._close_recording()
        backend.mx.Saving.return_value.group_define.assert_not_called()
        self.assertEqual(backend.mx.Event.call_count, 2)
        ids = [c.args[2] for c in backend.mx.Event.call_args_list]
        self.assertEqual(ids, [BOUNDARY_IDS['start'], BOUNDARY_IDS['end']])
        self.assertIn(unittest.mock.call(1.0), sleep.call_args_list)
        self.assertIn(unittest.mock.call(2), sleep.call_args_list)
        self.assertEqual(backend.mx.Sequence.call_count, 2)
        for call in backend.mx.Sequence.call_args_list:
            self.assertEqual(call.kwargs, dict(initial_delay=100, persistent=False))
        commands = [c.args[0] for c in backend.mx.send_raw.call_args_list]
        self.assertEqual(len(commands), 2)
        for seq, send in zip(retained, commands):
            self.assertEqual(send, 'sequence_send ' + seq.token)
            seq.append.assert_called_once()
            event = seq.append.call_args.args[0].set()
            self.assertTrue(event.startswith('system_trigger_event 0 1 '))
            self.assertNotIn('dac', event.lower())
        self.assertEqual(backend.boundary_sequences, [])
        backend.mx.send.assert_not_called()
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
            backend.mx.send_raw.side_effect = RuntimeError('end marker failed')
            with self.assertRaisesRegex(RuntimeError, 'end marker failed'):
                backend._close_recording()
        backend.mx.Saving.return_value.stop_recording.assert_called_once()
        backend.mx.Saving.return_value.stop_file.assert_called_once()

    def test_empty_boundary_sequence_ack_is_rejected_and_saver_still_closes(self):
        for replies, error in ((['', 'OK'], '写入录制start'),):
            with self.subTest(error=error):
                backend = self.backend()
                backend.mx.send_raw.side_effect = replies
                with patch('python.maxwell_setup.time.sleep'):
                    with self.assertRaisesRegex(RuntimeError, error):
                        backend.start_segment(self.root, 'check')
                    backend._close_recording()
                backend.mx.Saving.return_value.stop_recording.assert_called_once()
                backend.mx.Saving.return_value.stop_file.assert_called_once()
                backend.mx.DAC.assert_not_called()

    def test_boundary_append_failure_stops_before_send_and_still_closes_file(self):
        backend = self.backend()
        good_factory = backend.mx.Sequence.side_effect
        def broken_sequence(name, **kwargs):
            seq = good_factory(name, **kwargs)
            if not backend.boundary_sequences:
                seq.append.return_value = None
            return seq
        backend.mx.Sequence.side_effect = broken_sequence
        with patch('python.maxwell_setup.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'SDK未返回Sequence对象'):
                backend.start_segment(self.root, 'check')
            backend.mx.send_raw.assert_not_called()
            backend._close_recording()
        backend.mx.Saving.return_value.stop_file.assert_called_once()

    def test_short_recording_default_never_connects_or_creates_output(self):
        from tools import check_formal_recording as tool
        with patch.object(tool.runpy, 'run_path', return_value=dict(CONFIG_DIR='config')), \
             patch.object(tool, 'load_configuration', return_value=(self.system,self.stim)), \
             patch.object(tool, 'check_environment') as environment, \
             patch.object(tool, 'check_recording') as recording, \
             patch('sys.argv', ['check_formal_recording.py']):
            tool.main()
        environment.assert_not_called()
        recording.assert_not_called()

    def short_recording_sdk(self, fixture, output):
        import shutil
        mx = self.backend().mx
        mx.Array.return_value.load_config.return_value = 'OK'
        mx.Saving.return_value.stop_file.side_effect = lambda: shutil.copyfile(
            fixture, output/'storage_check.raw.h5')
        return mx

    def test_short_recording_reads_real_h5_contract_without_initializing_or_routing(self):
        from tools.check_formal_recording import check_recording
        path = self.fixture()
        with h5py.File(path, 'r+') as handle:
            rows = handle['data_store/data0000/events'][:]
            del handle['data_store/data0000/events']
            handle['data_store/data0000'].create_dataset('events',data=rows[rows['eventid']!=7])
        output = self.root/'short_check'
        mx = self.short_recording_sdk(path,output)
        before = copy.deepcopy(self.system), sha256(self.cfg)
        with patch('tools.check_formal_recording.time.sleep'):
            result = check_recording(self.system,output,mx)
        self.assertEqual(result['status'],'recording_check_passed')
        self.assertEqual(result['time_origin_source'],'h5_boundary_events')
        self.assertEqual(result['stimulus_pulses_sent'],0)
        self.assertFalse(result['raw_recording'])
        self.assertTrue((output/'recording_check.json').is_file())
        self.assertEqual((self.system,sha256(self.cfg)),before)
        mx.Array.return_value.download.assert_not_called()
        mx.Array.return_value.route.assert_not_called()
        mx.Array.return_value.close.assert_called_once()
        for action in (mx.send,mx.DAC,mx.StimulationUnit,mx.initialize,mx.offset,mx.set_event_threshold):
            action.assert_not_called()
        mx.Saving.return_value.group_define.assert_not_called()

    def test_short_recording_rejects_missing_markers_raw_and_other_events(self):
        from tools.check_formal_recording import check_recording
        for mode, raw in (('missing_end',False),('valid',True),('valid',False)):
            with self.subTest(mode=mode,raw=raw):
                path = self.fixture(mode,raw=raw)
                if raw:
                    with h5py.File(path,'r+') as handle:
                        rows = handle['data_store/data0000/events'][:]
                        del handle['data_store/data0000/events']
                        handle['data_store/data0000'].create_dataset('events',data=rows[rows['eventid']!=7])
                        handle['data_store/data0000'].create_dataset('groups/exp/raw',data=[[1]])
                # The valid fixture contains an unrelated stimulus event, so it
                # must also be refused as a no-stimulation recording check.
                output = self.root/f'short_{mode}_{raw}'
                mx = self.short_recording_sdk(path,output)
                expected = '起止帧' if mode == 'missing_end' else 'raw电压' if raw else '其他事件'
                with patch('tools.check_formal_recording.time.sleep'), self.assertRaisesRegex(ValueError,expected):
                    check_recording(self.system,output,mx)
                mx.Array.return_value.close.assert_called_once()
                self.assertFalse((output/'recording_check.json').exists())
                mx.DAC.assert_not_called()

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

    def test_formal_three_segments_accept_sdk_json_and_keep_stimulus_alignment(self):
        original = MockBackend.finish_segment
        def sdk_json(backend):
            path = original(backend)
            with h5py.File(path,'r+') as handle:
                dataset = handle['data_store/data0000/events']
                rows = dataset[:]
                for row in rows:
                    words = row['eventmessage'].decode().split()
                    row['eventmessage'] = (json.dumps(dict(zip(words[::2],words[1::2])))+'\n').encode()
                dataset[:] = rows
            return path
        system, stim = self.mocked()
        with patch.object(MockBackend,'finish_segment',sdk_json), patch('python.experiment_runner.unit_map'):
            out = execute(system,stim,formal_selection=self.selection_file)
        files = list((out/'raw_data').rglob('*.raw.h5'))
        self.assertEqual(len(files),3)
        for path in files:
            session = read_session(path,self.cfg)
            self.assertEqual(session.time_origin_source,'h5_boundary_events')
            self.assertTrue(all(isinstance(json.loads(e['eventmessage']),dict) for e in session.events))
        stim_path = next(p for p in files if p.parent.name=='02_stim')
        timing = json.loads((stim_path.parent/'segment_time_meta.json').read_text(encoding='utf-8'))
        self.assertTrue(timing['verified'])
        self.assertEqual(timing['pulse_count'],204)

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
