from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import h5py
import numpy as np
import yaml

from analysis.utils.probe import evaluate_probe, validate_paired_config
from python.utils.persistence import sha256, write_csv
from tools.review_probe_paired_ratio import review_run


def config():
    return dict(threshold_rule='paired_pre_stim_ratio',paired_baseline_window_ms=[-50.,-10.],
                response_window_ms=[10.,50.],response_to_baseline_ratio=1.5,
                artifact_min_spikes=300,enforce_artifact_qc=False,success_fraction_threshold=.6,
                trials_per_electrode=10)


class PairedRatioChecks(unittest.TestCase):
    def test_paired_mock_probe_has_complete_pre_windows_and_no_formal_run(self):
        from main import ROOT, load_configuration
        from python.experiment_runner import ExperimentRunner
        from python.mock_backend import MockBackend
        from python.utils.progress import Reporter
        system,stimulation=load_configuration(ROOT/'config')
        system['mock']['baseline_duration_override_sec']=40
        stimulation['probe'].update(config())
        reporter=Reporter()
        with tempfile.TemporaryDirectory() as temporary:
            backend=MockBackend(system,reporter)
            runner=ExperimentRunner(system,stimulation,temporary,backend,reporter,probe_only=True)
            with patch('python.experiment_runner.unit_map'), patch('python.experiment_runner.raster'), \
                    patch('python.experiment_runner.summary_table'),patch.object(runner,'formal') as formal:
                runner.run()
            formal.assert_not_called()
            run=Path(temporary)
            result=json.loads((run/'experiment_result.json').read_text())
            self.assertEqual(result['probe_trial_count'],240)
            trial_files=list((run/'pre_experiment/02_probe').rglob('trial_metrics.csv'))
            self.assertEqual(len(trial_files),24)
            import csv
            for file in trial_files:
                with file.open(encoding='utf-8-sig',newline='') as f:
                    trials=list(csv.DictReader(f))
                self.assertEqual(len(trials),10)
                self.assertGreaterEqual(float(trials[0]['stim_time_sec']),.05)
                self.assertTrue(all(float(t['response_threshold'])==1.5*int(t['baseline_minus50_minus10ms']) for t in trials))

    def test_strict_ratio_boundary_zero_baseline_and_seven_of_ten(self):
        onsets=np.arange(10)+1.
        for successes in [6,7]:
            points=[]
            for i,t in enumerate(onsets):
                points.extend(t+np.array([-.04,-.03]))
                points.extend(t+np.linspace(.015,.035,4 if i<successes else 3))
            trials,summary=evaluate_probe(SimpleNamespace(times=np.sort(points),duration_sec=20),onsets,None,config())
            self.assertEqual(summary['success_trials'],successes)
            self.assertEqual(summary['eligible'],successes==7)
            self.assertIsNone(summary['response_threshold'])
            self.assertTrue(all(t['response_threshold']==3 for t in trials))
        trials,_=evaluate_probe(SimpleNamespace(times=np.array([1.02]),duration_sec=3),[1,2],None,config())
        self.assertEqual([t['success'] for t in trials],[True,False])

    def test_boundaries_and_incomplete_pre_window(self):
        session=SimpleNamespace(times=np.array([.95,.96,.99,1.01,1.02,1.05]),duration_sec=3)
        trials,_=evaluate_probe(session,[1],None,config())
        self.assertEqual(trials[0]['baseline_minus50_minus10ms'],2)
        self.assertEqual(trials[0]['response_10_50ms'],2)
        with self.assertRaises(ValueError):
            evaluate_probe(session,[.03],None,config())
        bad=config(); bad['response_to_baseline_ratio']=np.nan
        with self.assertRaises(ValueError):
            validate_paired_config(bad)

    def test_old_h5_offline_review_preserves_inputs_and_uses_actual_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            run=root/'source'
            site=run/'pre_experiment/02_probe/candidate_01/ele_100_attempt_01'
            site.mkdir(parents=True)
            snapshot=run/'config_snapshot'; snapshot.mkdir()
            (snapshot/'system.yaml').write_text(yaml.safe_dump({'maxwell':{'well':0}}),encoding='utf-8')
            (snapshot/'stimulation.yaml').write_text(yaml.safe_dump({'probe':config()}),encoding='utf-8')
            onsets=.2+np.arange(10)*.5
            points=np.sort(np.concatenate([t+np.array([-.04,-.03,.015,.020,.025,.030]) for t in onsets]))
            h5=site/'probe.raw.h5'
            with h5py.File(h5,'w') as f:
                segment=f.create_group('data_store/data0000')
                segment.create_dataset('well_id',data=0)
                settings=segment.create_group('settings')
                settings.create_dataset('sampling',data=[20000.])
                settings.create_dataset('mapping',data=np.array([(0,100,0.,0.)],dtype=[('channel','i4'),('electrode','i4'),('x','f8'),('y','f8')]))
                group=segment.create_group('groups/group0')
                group.create_dataset('frame_nos',data=[100000,219999])
                group.create_dataset('raw',data=np.zeros((1,2),dtype='u2'))
                segment.create_dataset('spikes',data=np.array([(100000+round(t*20000),0) for t in points],dtype=[('frameno','i8'),('channel','i4')]))
                segment.create_dataset('events',data=np.array([(i+1,100000+round(t*20000),b'probe') for i,t in enumerate(onsets)],dtype=[('eventid','i4'),('frameno','i8'),('eventmessage','S20')]))
            write_csv(site/'trial_metrics.csv',[dict(stim_time_sec=t,response_10_50ms=4) for t in onsets])
            write_csv(run/'pre_experiment/probe_summary.csv',[dict(unit_id='candidate_01',electrode=100,status='complete',h5=str(h5),all_trials=10,success_trials=0)])
            hashes={p:sha256(p) for p in run.rglob('*') if p.is_file()}
            original=h5py.Dataset.__getitem__
            def guarded(dataset,key):
                if dataset.name.endswith('/raw'):
                    raise AssertionError('No raw voltage reads')
                return original(dataset,key)
            with patch('tools.review_probe_paired_ratio.ROOT',root), patch.object(h5py.Dataset,'__getitem__',guarded), redirect_stdout(io.StringIO()):
                output=review_run(run)
            self.assertEqual(hashes,{p:sha256(p) for p in run.rglob('*') if p.is_file()})
            self.assertTrue(json.loads((output/'analysis_result.json').read_text())['source_files_unchanged'])
            self.assertFalse((output/'experiment_result.json').exists())
            import csv
            with (output/'paired_probe_summary.csv').open(encoding='utf-8-sig',newline='') as f:
                row=next(csv.DictReader(f))
            self.assertEqual(row['success_trials'],'10')
            self.assertEqual(row['eligible'],'True')
