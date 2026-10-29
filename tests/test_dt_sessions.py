"""Third-batch coverage, reference identity, independent sessions and H5 execution."""
from collections import Counter
import copy
import csv
import json
import unittest
from unittest.mock import patch

from main import ROOT, load_configuration, make_plan, validate, execute
from python.dt_sessions import configure_dt_session, build_session_schedule
from python.pair_batches import condition_manifest


class DtSessionChecks(unittest.TestCase):
    def setUp(self):
        self.system, self.stim = load_configuration(ROOT / 'config')
        self.units = [dict(electrodes=[10*i, 10*i+1]) for i in range(1, 4)]

    def test_both_sessions_coverage_counts_duration_and_no_one_ms(self):
        for session, dt, minutes in [('short', [5,10], 77.652), ('long', [50,100], 79.002)]:
            stim = configure_dt_session(self.stim, session)
            validate(self.system, stim)
            steps = build_session_schedule(self.units, stim['formal'])
            rows = condition_manifest(steps)
            self.assertTrue(all(r['planned_trials'] == 50 for r in rows))
            self.assertEqual(sum(r['planned_trials'] for r in rows), 2700)
            temporal = [s for s in steps if s['action']=='trial' and s['condition'].kind=='temporal']
            self.assertEqual(Counter(s['condition'].dt_ms for s in temporal), {dt[0]:600, dt[1]:600})
            for d in dt:
                for r in range(1,51):
                    cs=[s['condition'] for s in temporal if s['round']==r and s['condition'].dt_ms==d]
                    self.assertEqual(len(cs), 12)
                    self.assertEqual(len({c.electrodes for c in cs}), 12)
                    for c in cs:self.assertIn(c.electrodes[::-1], [x.electrodes for x in cs])
            plan = make_plan(self.system, stim, formal_units=self.units)
            self.assertEqual((plan['primary_trial_count'], plan['reference_trial_count']), (1800,900))
            self.assertAlmostEqual(plan['formal_nominal_duration_sec']/60, minutes+.25/60, places=6)
            self.assertFalse(plan['automatically_starts_next_session'])

    def test_references_exactly_repeat_initial_single_order(self):
        stim=configure_dt_session(self.stim)
        steps=build_session_schedule(self.units,stim['formal'])
        initial=[(s['round'],s['condition'].electrodes) for s in steps if s['stage']=='single_initial']
        for checkpoint in ('R0','R1','R2'):
            self.assertEqual(initial, [(s['round'],s['condition'].electrodes) for s in steps
                                      if s.get('checkpoint')==checkpoint])
        self.assertEqual([s['stage'] for s in steps if s['action']=='rest'], ['rest_after_sync','rest_final'])
        transitions=list(dict.fromkeys(s['stage'] for s in steps))
        self.assertEqual(transitions, ['single_initial','synchronous','rest_after_sync','reference_R0',
            'temporal_dt5ms','reference_R1','temporal_dt10ms','reference_R2','rest_final'])

    def test_optional_rest_rechecks_all_six_sites_after_rest(self):
        stim=configure_dt_session(self.stim,rest_between_dt_sec=300)
        plan=make_plan(self.system,stim,formal_units=self.units)
        self.assertEqual(plan['formal_trial_count'],3000)
        self.assertEqual(plan['reference_trial_count'],1200)
        steps=build_session_schedule(self.units,stim['formal'])
        i=next(i for i,s in enumerate(steps) if s['stage']=='rest_between_dt')
        self.assertEqual(steps[i-1]['checkpoint'],'R1')
        self.assertEqual(steps[i+1]['checkpoint'],'after_rest')
        self.assertEqual(sum(s.get('checkpoint')=='after_rest' for s in steps),300)

    def test_bad_config_rejected_and_input_not_mutated(self):
        before=copy.deepcopy(self.stim)
        for kwargs in [dict(session='both'),dict(trials=True),dict(rest_between_dt_sec=float('nan'))]:
            with self.assertRaises(ValueError):configure_dt_session(self.stim,**kwargs)
        self.assertEqual(before,self.stim)
        for key,value in [('dt_ms',[1,5]),('trial_interval_sec',.1),('condition_order','random')]:
            bad=configure_dt_session(self.stim);bad['formal'][key]=value
            with self.assertRaises(ValueError):validate(self.system,bad)
        duplicate=copy.deepcopy(self.units);duplicate[1]['electrodes'][0]=duplicate[0]['electrodes'][0]
        with self.assertRaises(ValueError):build_session_schedule(duplicate,configure_dt_session(self.stim)['formal'])

    def test_preview_never_constructs_hardware_and_mock_has_three_verified_h5s(self):
        from test_formal_selection import FormalSelectionChecks
        from analysis.utils.io import read_session
        from analysis.utils.recordings import resolve_trials
        for session in ('short','long'):
            fixture=FormalSelectionChecks();fixture.setUp()
            try:
                fixture.system['mock']['trials_per_condition_override']=2
                stim=configure_dt_session(fixture.stim,session,rest_after_sync_sec=1,final_rest_sec=1,
                    rest_between_dt_sec=1 if session=='long' else 0)
                before=fixture.hashes()
                with patch('python.maxwell_setup.MaxLabBackend') as hardware,patch('python.experiment_runner.unit_map'):
                    dry=execute(fixture.system,stim,formal_selection=fixture.file,dry_run=True)
                    self.assertFalse(list(dry.rglob('*.raw.h5')))
                    fixture.system['data']['root']=str(fixture.root/'mock_outputs')
                    run=execute(fixture.system,stim,formal_selection=fixture.file)
                    hardware.assert_not_called()
                result=json.loads((run/'experiment_result.json').read_text(encoding='utf-8'))
                expected=120 if session=='long' else 108
                self.assertEqual(result['formal_trial_count'],expected)
                self.assertEqual(result['session_id'],session)
                self.assertEqual(result['reference_trials'],2)
                self.assertEqual(result['probe_trial_count'],0)
                h5s=list((run/'raw_data').rglob('*.raw.h5'));self.assertEqual(len(h5s),3)
                stim_h5=next(p for p in h5s if p.parent.name=='02_stim')
                trials,annotation=resolve_trials(read_session(stim_h5),stim_h5.parent/'trial_manifest.csv')
                self.assertEqual(len(trials),expected);self.assertEqual(annotation['unreferenced_events'],0)
                with (stim_h5.parent/'trial_manifest.csv').open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
                checkpoints={'R0':12,'R1':12,'R2':12}
                if session=='long':checkpoints['after_rest']=12
                self.assertEqual(Counter(r['checkpoint'] for r in rows if r['checkpoint']),checkpoints)
                self.assertTrue(all(r['timing_source']=='h5_events' and r['session_id']==session for r in rows))
                self.assertEqual(before,fixture.hashes())
            finally:fixture.doCleanups()


if __name__=='__main__':unittest.main()
