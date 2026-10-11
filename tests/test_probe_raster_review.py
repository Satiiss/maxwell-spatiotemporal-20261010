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

from tools.probe_raster_review import align_saved_events, electrode_rows, local_spikes, pooled_psth
from python.utils.persistence import sha256, write_csv
from tools.plot_probe_rasters import plot_run


class ProbeRasterChecks(unittest.TestCase):
    def test_electrode_rows_include_silent_sites_and_use_actual_ids(self):
        mapping = [dict(channel=914,electrode=23209), dict(channel=7,electrode=5193),
                   dict(channel=100,electrode=13953)]
        rows = electrode_rows(mapping)
        self.assertEqual(rows, [dict(row_index=1,electrode=5193,channel=7),
                                dict(row_index=2,electrode=13953,channel=100),
                                dict(row_index=3,electrode=23209,channel=914)])
        with self.assertRaises(ValueError):
            electrode_rows(mapping+[dict(channel=1,electrode=23209)])
    def test_actual_event_required(self):
        event = dict(event_id=1, time_sec=.25, eventmessage='probe')
        session = SimpleNamespace(events=[event], sampling_hz=20000, duration_sec=1.)
        times, events = align_saved_events(session, [dict(stim_time_sec='.25')])
        self.assertEqual(times.tolist(), [.25])
        self.assertEqual(events, [event])
        with self.assertRaises(ValueError):
            align_saved_events(session, [dict(stim_time_sec='.3')])
        session.events.append(dict(event))
        with self.assertRaises(ValueError):
            align_saved_events(session, [dict(stim_time_sec='.25')])

    def test_window_boundaries_coverage_and_pooled_rate(self):
        session = SimpleNamespace(times=np.array([-.001, 0., .0005, .001, .002, .003, .004, .006]),
                                  channels=np.array([0, 1, 0, 1, 0, 1, 0, 1]), duration_sec=.0045)
        x, channels = local_spikes(session, .001, [-1, 2])
        np.testing.assert_allclose(x, [-1., -.5, 0., 1.])
        self.assertEqual(channels.tolist(), [1, 0, 1, 0])
        edges, counts, exposure, rate = pooled_psth(session, [.001, .004], [-1, 2])
        self.assertEqual(edges.tolist(), [-1., 0., 1., 2.])
        self.assertEqual(counts.tolist(), [3, 1, 1])
        self.assertEqual(exposure.tolist(), [2, 1, 1])
        np.testing.assert_allclose(rate, [1500., 1000., 1000.])

    def test_h5_readonly_figures_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root/'source'
            site = run/'pre_experiment/02_probe/candidate_01/ele_100_attempt_01'
            site.mkdir(parents=True)
            snapshot = run/'config_snapshot'
            snapshot.mkdir()
            (snapshot/'system.yaml').write_text(yaml.safe_dump({'maxwell':{'well':0}}), encoding='utf-8')
            (snapshot/'stimulation.yaml').write_text(yaml.safe_dump({'probe':{'order':'sequential'}}), encoding='utf-8')
            h5 = site/'probe.raw.h5'
            with h5py.File(h5,'w') as f:
                seg = f.create_group('data_store/data0000')
                seg.create_dataset('well_id',data=0)
                settings = seg.create_group('settings')
                settings.create_dataset('sampling',data=[20000.])
                settings.create_dataset('mapping',data=np.array([(0,100,0.,0.),(1,101,17.5,0.)],
                    dtype=[('channel','i4'),('electrode','i4'),('x','f8'),('y','f8')]))
                group=seg.create_group('groups/group0')
                group.create_dataset('frame_nos',data=[40000,80000])
                group.create_dataset('raw',data=np.zeros((2,5),dtype='u2'))
                times=[.247,.251,.261,.273,.305,.747,.751,.761,.773,.805]
                seg.create_dataset('spikes',data=np.array([(40000+round(t*20000),i%2,-10.) for i,t in enumerate(times)],
                    dtype=[('frameno','i8'),('channel','i4'),('amplitude','f8')]))
                seg.create_dataset('events',data=np.array([(101,45000,b'probe'),(102,55000,b'probe')],
                    dtype=[('eventid','i4'),('frameno','i8'),('eventmessage','S20')]))
            write_csv(site/'trial_metrics.csv',[dict(trial=i+1,stim_time_sec=t,response_10_50ms=2)
                                               for i,t in enumerate([.25,.75])])
            write_csv(run/'pre_experiment/probe_summary.csv',[
                dict(unit_id='candidate_01',electrode=100,status='complete',h5=str(h5),all_trials=2),
                dict(unit_id='candidate_02',electrode=102,status='unroutable',h5='',all_trials=0)])
            hashes={p:sha256(p) for p in run.rglob('*') if p.is_file()}
            original_get=h5py.Dataset.__getitem__
            def guarded_read(dataset,key):
                if dataset.name.endswith('/raw'):
                    raise AssertionError('Must not read raw voltage to plot spikes')
                return original_get(dataset,key)
            with patch('tools.plot_probe_rasters.ROOT', root), patch.object(h5py.Dataset,'__getitem__',guarded_read), redirect_stdout(io.StringIO()):
                output=plot_run(run)
            figures=output/'candidate_01/ele_100'
            for name in ['aligned_trials','artifact_zoom','aligned_electrodes','electrode_artifact_zoom',
                         'trial_01_raster','trial_02_raster','trial_01_electrodes','trial_02_electrodes',
                         'psth','psth_after_3ms']:
                for suffix in ['png','pdf','svg']:
                    self.assertGreater((figures/f'{name}.{suffix}').stat().st_size,100)
            report=json.loads((output/'analysis_result.json').read_text(encoding='utf-8'))
            self.assertTrue(report['source_files_unchanged'])
            self.assertEqual(report['sites'][0]['recorded_electrodes'],2)
            self.assertTrue((figures/'electrode_rows.csv').exists())
            self.assertEqual(report['skipped'][0]['status'],'unroutable')
            self.assertFalse((output/'experiment_result.json').exists())
            self.assertEqual(hashes,{p:sha256(p) for p in run.rglob('*') if p.is_file()})
            with self.assertRaises(ValueError):
                plot_run(run, electrodes=[102])


if __name__=='__main__':
    unittest.main()
