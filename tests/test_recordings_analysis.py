"""Standalone inputs, strict event timing, paired windows, cache and read-only outputs."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np

from analysis.utils.io import Session,BOUNDARY_IDS,BOUNDARY_PREFIX
from analysis.utils.recordings import (DEFAULT_PARAMETERS,validate_parameters,summarize_activity,
    resolve_trials,analyze_responses,compare_pre_post)
from python.utils.persistence import sha256,write_csv
from tools.analyze_recordings import run_analysis

ROOT = Path(__file__).resolve().parents[1]
MAP = [dict(channel=i,electrode=100+i,x=i*17.5,y=0.) for i in range(3)]


class RecordingAnalysisChecks(unittest.TestCase):
    def setUp(self):
        (ROOT/'data').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT/'data')
        self.addCleanup(temporary.cleanup); self.root = Path(temporary.name)
        self.parameters = copy.deepcopy(DEFAULT_PARAMETERS)

    def event(self,time,event_id,kind='single',condition='A',pulse=1,dt=0,json_message=True):
        props = dict(condition=condition,pulse=str(pulse),kind=kind,dt_ms=str(dt))
        message = json.dumps(props)+'\n' if json_message else ' '.join(str(x) for pair in props.items() for x in pair)
        return dict(event_id=event_id,time_sec=time,eventmessage=message)

    def session(self,times=(),events=(),duration=10):
        times = np.sort(np.asarray(times,float))
        return Session(self.root/'neutral.h5',times,np.zeros(len(times),int),copy.deepcopy(MAP),
            20000.,100000,duration,list(events),0,None,'raw_frame_numbers')

    def h5(self,name,events=(),times=(),gain=512,threshold=5.5):
        path = self.root/name; path.parent.mkdir(parents=True,exist_ok=True)
        md = np.dtype([('channel','i4'),('electrode','i4'),('x','f8'),('y','f8')])
        sd = np.dtype([('frameno','i8'),('channel','i4'),('amplitude','f8')])
        ed = np.dtype([('frameno','i8'),('eventtype','u4'),('eventid','u4'),('eventmessage',h5py.string_dtype())])
        origin,fs = 100000,20000
        with h5py.File(path,'w') as handle:
            g = handle.create_group('data_store/strange_segment')
            g.create_dataset('well_id',data=[0]); g.create_dataset('settings/sampling',data=[fs])
            g.create_dataset('settings/gain',data=[gain]); g.create_dataset('settings/spike_threshold',data=[threshold])
            g.create_dataset('settings/mapping',data=np.array([tuple(r[k] for k in md.names) for r in MAP],dtype=md))
            g.create_dataset('groups/any/frame_nos',data=[origin,origin+10*fs-1])
            g.create_dataset('spikes',data=np.array([(origin+round(t*fs),0,-20.) for t in times],dtype=sd))
            g.create_dataset('events',data=np.array([(origin+round(e['time_sec']*fs),1,e['event_id'],e['eventmessage']) for e in events],dtype=ed))
        return path

    def test_silent_channels_and_silent_lead_in_use_full_duration(self):
        summary,electrodes,bursts,population = summarize_activity(self.session([4.,5.]),self.parameters)
        self.assertEqual(summary['spike_count'],2)
        self.assertEqual(summary['mapped_electrodes'],3)
        self.assertAlmostEqual(summary['mean_electrode_rate_hz'],2/10/3)
        self.assertEqual([r['spike_count'] for r in electrodes],[2,0,0])
        self.assertEqual(sum(r['spike_count'] for r in population),2)
        duplicate=self.session([1]); duplicate.mapping.append(dict(duplicate.mapping[0]))
        with self.assertRaisesRegex(ValueError,'重复'): summarize_activity(duplicate,self.parameters)

    def test_new_analysis_can_disable_density_fallback_without_changing_legacy_default(self):
        from analysis.utils.detection.burst import detect_burst_intervals
        spikes = {0:np.arange(0,1,.001)}
        self.assertTrue(detect_burst_intervals(spikes)[0])
        self.assertEqual(detect_burst_intervals(spikes,density_fallback=False)[0],[])

    def test_json_and_text_events_reconstruct_pairs_without_a_run_directory(self):
        events = [self.event(1,1,json_message=False),self.event(2,2,'temporal','AB',1,1000),
                  self.event(3,3,'temporal','AB',2,1000),self.event(4,4,'synchronous','AC')]
        trials,info = resolve_trials(self.session(events=events))
        self.assertEqual(len(trials),3)
        self.assertEqual(trials[1]['onsets_sec'],[2.,3.])
        self.assertEqual(info['stimulus_event_count'],4)
        self.assertEqual(trials[1]['electrodes'],[])

    def test_temporal_background_is_before_first_pulse_not_before_second(self):
        events = [self.event(2,1,'temporal','AB',1,1000),self.event(3,2,'temporal','AB',2,1000)]
        times = [1.96]+[2.96]*20+[3.01,3.02,3.04]
        session = self.session(times,events)
        trials,_ = resolve_trials(session)
        rows,summary,psth,bg = analyze_responses(session,trials,self.parameters,[])
        self.assertEqual(rows[0]['baseline_count'],1)
        self.assertEqual(rows[0]['response_count'],3)
        self.assertTrue(rows[0]['success'])

    def test_strict_ratio_zero_baseline_and_half_open_edges(self):
        session = self.session([1.95,1.96,1.99,2.01,2.02,2.0499,2.05], [self.event(2,1)])
        trials,_ = resolve_trials(session)
        rows,*_ = analyze_responses(session,trials,self.parameters,[])
        self.assertEqual((rows[0]['baseline_count'],rows[0]['response_count']),(2,3))
        self.assertFalse(rows[0]['success'])
        session = self.session([2.01],[self.event(2,1)])
        rows,*_ = analyze_responses(session,resolve_trials(session)[0],self.parameters,[])
        self.assertTrue(rows[0]['success'])

    def test_incomplete_window_and_other_trial_pulses_are_not_zero_background(self):
        session = self.session([.025,.03],[self.event(.02,1),self.event(.04,2)])
        rows,summary,*_ = analyze_responses(session,resolve_trials(session)[0],self.parameters,[])
        self.assertTrue(all(row['success'] is None for row in rows))
        self.assertEqual(summary[0]['complete_trials'],0)
        self.assertIsNone(summary[0]['success_fraction'])

    def test_psth_artifact_bins_are_missing_not_artificial_suppression(self):
        session = self.session([2.001]*30+[2.02],[self.event(2,1)])
        _,_,psth,_ = analyze_responses(session,resolve_trials(session)[0],self.parameters,[])
        early = [r for r in psth if r['start_ms']>=0 and r['start_ms']<3]
        self.assertTrue(early)
        self.assertTrue(all(r['available_trials']==0 and r['population_rate_hz'] is None for r in early))

    def test_annotation_timestamps_cannot_replace_missing_h5_events(self):
        table = self.root/'labels.csv'
        write_csv(table,[dict(trial_id=1,condition='A',kind='single',dt_ms=0,event_ids='[999]',
                              electrodes='[123]',onsets_sec='[2.0]')])
        with self.assertRaisesRegex(ValueError,'禁止使用主机估计时间'):
            resolve_trials(self.session(events=[self.event(2,1)]),table)

    def test_annotation_condition_conflict_and_reused_events_are_rejected(self):
        session = self.session(events=[self.event(2,1)])
        table = self.root/'labels.csv'
        row = dict(trial_id=1,condition='WRONG',kind='single',dt_ms=0,event_ids='[1]',electrodes='[100]')
        write_csv(table,[row])
        with self.assertRaisesRegex(ValueError,'condition.*冲突'): resolve_trials(session,table)
        row['condition']='A'; write_csv(table,[row,dict(row,trial_id=2)])
        with self.assertRaisesRegex(ValueError,'重复引用'): resolve_trials(session,table)

    def test_missing_labels_can_be_explicitly_annotated_but_wrong_dt_cannot(self):
        session = self.session(events=[dict(event_id=1,time_sec=2,eventmessage=''),
                                       dict(event_id=2,time_sec=2.5,eventmessage='')])
        with self.assertRaises(ValueError): resolve_trials(session)
        table = self.root/'labels.csv'
        row = dict(trial_id=1,condition='AB',kind='temporal',dt_ms=500,event_ids='[1,2]',electrodes='[100,101]')
        write_csv(table,[row]); trials,_ = resolve_trials(session,table)
        self.assertEqual(trials[0]['electrodes'],[100,101])
        write_csv(table,[dict(row,dt_ms=1000)])
        with self.assertRaisesRegex(ValueError,'间隔'): resolve_trials(session,table)

    def test_pre_post_comparison_blocks_settings_or_mapping_changes(self):
        summary,electrodes,*_ = summarize_activity(self.session([1,2]),self.parameters)
        summary.update(gain=512,spike_threshold=5.5)
        comparison,rows = compare_pre_post(summary,summary,electrodes,electrodes)
        self.assertEqual(comparison['status'],'descriptive'); self.assertEqual(len(rows),3)
        self.assertEqual(compare_pre_post(summary,dict(summary,spike_threshold=5),electrodes,electrodes)[0]['status'],'blocked')
        self.assertEqual(compare_pre_post(dict(summary,chip_id='A'),dict(summary,chip_id='B'),electrodes,electrodes)[0]['status'],'blocked')
        changed = copy.deepcopy(electrodes); changed[0]['electrode']=999
        self.assertEqual(compare_pre_post(summary,summary,electrodes,changed)[0]['status'],'blocked')

    def test_standalone_three_files_cache_and_input_hashes(self):
        a = self.h5('arbitrary/a.h5',times=[1,2])
        b = self.h5('unrelated/b.h5',events=[self.event(2,1)],times=[1.96,2.01,2.02])
        c = self.h5('another/place/c.h5',times=[1,2,3])
        inputs = [dict(name=n,role=n,h5=str(p)) for n,p in zip(('pre','stim','post'),(a,b,c))]
        before = {p:sha256(p) for p in (a,b,c)}
        output,result = run_analysis(inputs,self.parameters,self.root/'analysis',render=False)
        self.assertNotEqual(result['status'],'partial')
        self.assertTrue((output/'pre_post_electrodes.csv').exists())
        self.assertTrue((output/'stim/condition_summary.csv').exists())
        self.assertTrue((output/'config.yaml').exists())
        with patch('tools.analyze_recordings.read_session',side_effect=AssertionError('Cache must skip H5 analysis')):
            second,cached = run_analysis(inputs,self.parameters,self.root/'analysis',render=False)
        self.assertNotEqual(second,output)
        self.assertTrue(all(row['cache_hit'] for row in cached['files']))
        self.assertEqual({p:sha256(p) for p in (a,b,c)},before)

    def test_spikes_only_sdk_json_inputs_work_without_cfg_runner_or_sdk(self):
        import builtins
        path = self.h5('unstructured/no_configuration_needed.h5',events=[self.event(2,1)],times=[1.96,2.01,2.02])
        with h5py.File(path,'r+') as handle:
            g = handle['data_store/strange_segment']; rows = list(g['events'][:]); dtype=g['events'].dtype
            del g['groups']; del g['events']
            token='b'*32
            markers = [(100000,1,BOUNDARY_IDS['start'],json.dumps({BOUNDARY_PREFIX:'start','token':token})),
                       (300000,1,BOUNDARY_IDS['end'],json.dumps({BOUNDARY_PREFIX:'end','token':token}))]
            events = [markers[0]]+[tuple(row[n] for n in dtype.names) for row in rows]+[markers[1]]
            g.create_dataset('events',data=np.array(events,dtype=dtype))
        original_import = builtins.__import__
        def no_sdk(name,*args,**kwargs):
            if name=='maxlab' or name.startswith('maxlab.'): raise AssertionError('SDK is forbidden')
            return original_import(name,*args,**kwargs)
        before = sha256(path)
        with patch('builtins.__import__',side_effect=no_sdk):
            output,result = run_analysis([dict(name='stim',role='stim',h5=str(path))],
                self.parameters,self.root/'no_config_analysis',render=False,use_cache=False)
        summary=json.loads((output/'stim/summary.json').read_text(encoding='utf-8'))
        self.assertFalse(summary['contains_raw'])
        self.assertEqual(summary['time_origin_source'],'h5_boundary_events')
        self.assertEqual(summary['trial_count'],1)
        self.assertNotEqual(result['status'],'partial')
        self.assertEqual(sha256(path),before)

    def test_cache_is_invalidated_when_inputs_or_cached_results_change(self):
        path=self.h5('sample.h5',times=[1,2])
        inputs=[dict(name='sample',role='pre',h5=str(path))]
        root=self.root/'cache_test'
        run_analysis(inputs,self.parameters,root,render=False)
        with h5py.File(path,'r+') as handle: handle['data_store/strange_segment/settings/gain'][:]=1024
        second,result=run_analysis(inputs,self.parameters,root,render=False)
        self.assertFalse(result['files'][0]['cache_hit'])
        self.assertEqual(json.loads((second/'sample/summary.json').read_text())['gain'],1024)
        for cache in (root/'.cache').iterdir():
            (cache/'quality.json').write_text('{}',encoding='utf-8')
        third,result=run_analysis(inputs,self.parameters,root,render=False)
        self.assertFalse(result['files'][0]['cache_hit'])
        self.assertTrue((third/'sample/summary.json').exists())

    def test_missing_stimulus_labels_do_not_invent_old_electrodes_or_conditions(self):
        path=self.h5('candidate_01.h5',events=[dict(event_id=1,time_sec=2,eventmessage='')],times=[2.02])
        output,result=run_analysis([dict(name='stim',role='stim',h5=str(path))],self.parameters,
            self.root/'no_labels',render=False,use_cache=False)
        self.assertEqual(result['files'][0]['status'],'response_blocked')
        self.assertFalse((output/'stim/condition_summary.csv').exists())
        self.assertTrue((output/'stim/electrode_metrics.csv').exists())

    def test_old_raw_out_of_interval_spikes_are_reported_and_not_counted(self):
        path=self.h5('old_raw.h5',times=[-0.01,1.,10.])
        before=sha256(path)
        output,result=run_analysis([dict(name='old',role='pre',h5=str(path))],self.parameters,
            self.root/'old_bounds',render=False,use_cache=False)
        summary=json.loads((output/'old/summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['spike_count'],1)
        self.assertEqual(summary['out_of_interval_spikes'],2)
        self.assertEqual(result['files'][0]['status'],'review')
        self.assertEqual(sha256(path),before)

    def test_bad_file_does_not_hide_good_file_or_report_complete(self):
        good = self.h5('one.h5',times=[1])
        output,result = run_analysis([dict(name='missing',role='pre',h5=str(self.root/'missing.h5')),
            dict(name='valid',role='post',h5=str(good))],self.parameters,self.root/'out',render=False,use_cache=False)
        self.assertEqual(result['status'],'partial')
        self.assertTrue((output/'valid/summary.json').exists())
        self.assertEqual(result['files'][0]['status'],'file_error')

    def test_duplicate_file_roles_and_invalid_parameters_are_rejected(self):
        path = self.h5('a.h5')
        with self.assertRaisesRegex(ValueError,'同一文件'):
            run_analysis([dict(name='pre',role='pre',h5=str(path)),dict(name='post',role='post',h5=str(path))],
                         self.parameters,self.root/'out',render=False)
        for key,value in (('response_ratio',float('nan')),('psth_bin_ms',0),('artifact_window_ms',[0,4])):
            p = copy.deepcopy(self.parameters); p[key]=value
            with self.assertRaises(ValueError): validate_parameters(p)

    def test_single_h5_real_figures_use_600dpi_without_any_sdk(self):
        path = self.h5('independent.h5',times=[1,2])
        before = sha256(path)
        output,result = run_analysis([dict(name='sample',role='spontaneous',h5=str(path))],
            self.parameters,self.root/'figures',use_cache=False)
        from PIL import Image
        with Image.open(output/'sample/activity.png') as image:
            self.assertAlmostEqual(image.info['dpi'][0],600,delta=1)
        self.assertTrue((output/'sample/firing_rate_map.svg').exists())
        self.assertTrue((output/'data_check.png').exists())
        self.assertEqual(sha256(path),before)
        self.assertFalse(result['hardware_connected'])


if __name__=='__main__': unittest.main()
