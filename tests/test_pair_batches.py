"""Protocol coverage, reference separation and real runner/mock-H5 integration."""
from collections import Counter,defaultdict
import copy
import csv
import json
import unittest
from unittest.mock import patch

from main import ROOT,load_configuration,make_plan,validate,execute
from python.pair_batches import (configure_pair_batches,build_batch_schedule,schedule_manifest,
                                 condition_manifest,batch_plan)


class PairBatchChecks(unittest.TestCase):
    def setUp(self):
        self.system,self.stim=load_configuration(ROOT/'config')
        self.stim=configure_pair_batches(self.stim)
        self.units=[dict(electrodes=[10*i,10*i+1]) for i in range(1,4)]
        self.steps=build_batch_schedule(self.units,self.stim['formal'])

    def test_all_primary_conditions_fifty_trials_and_references_separate(self):
        rows=condition_manifest(self.steps)
        primary=[r for r in rows if r['relation']!='drift_reference']
        self.assertEqual(len(primary),72)
        self.assertTrue(all(r['planned_trials']==50 for r in primary))
        self.assertEqual(Counter(r['kind'] for r in primary),{'single':6,'synchronous':6,'temporal':60})
        refs=[r for r in rows if r['relation']=='drift_reference']
        self.assertEqual(len(refs),36);self.assertTrue(all(r['planned_trials']==10 for r in refs))
        self.assertEqual(sum(r['planned_trials'] for r in rows),3960)

    def test_each_batch_has_two_disjoint_pairs_and_all_dt_both_orders_each_round(self):
        rows=schedule_manifest(self.steps);batches=defaultdict(list)
        for r in rows:
            if r['pair_id']:batches[r['batch_id']].append(r)
        self.assertEqual(len(batches),3)
        for records in batches.values():
            pairs={r['pair_id']:set(r['electrodes']) for r in records if r['kind']=='synchronous'}
            a,b=list(pairs.values());self.assertFalse(a&b)
            for pair in pairs:
                for round_index in range(1,51):
                    block=[r for r in records if r['pair_id']==pair and r['round']==round_index]
                    self.assertEqual(len(block),11)
                    self.assertEqual(Counter(r['dt_ms'] for r in block if r['kind']=='temporal'),{1.:2,5.:2,10.:2,50.:2,100.:2})
                    self.assertEqual(len({tuple(r['electrodes']) for r in block if r['kind']=='temporal'}),2)

    def test_reference_checkpoint_occurs_at_zero_twenty_five_fifty_rounds(self):
        rows=schedule_manifest(self.steps)
        for batch in {r['batch_id'] for r in rows if r['batch_id']}:
            batch_rows=[r for r in rows if r['batch_id']==batch]
            refs={k:[i for i,r in enumerate(batch_rows) if r['checkpoint']==k] for k in ('pre','mid','post')}
            self.assertTrue(max(refs['pre'])<min(i for i,r in enumerate(batch_rows) if r['pair_id']))
            before=[r['round'] for r in batch_rows[:min(refs['mid'])] if r['pair_id']]
            after=[r['round'] for r in batch_rows[max(refs['mid'])+1:] if r['pair_id']]
            self.assertEqual(max(before),25);self.assertEqual(min(after),26)
            self.assertTrue(min(refs['post'])>max(i for i,r in enumerate(batch_rows) if r['pair_id']))

    def test_only_two_batch_rests_no_hidden_final_or_small_block_rests(self):
        rests=[s for s in self.steps if s['action']=='rest']
        self.assertEqual([s['seconds'] for s in rests],[300.,300.])
        self.assertEqual(self.steps[-1]['checkpoint'],'post')

    def test_reproducible_seed_and_source_config_unchanged(self):
        before=copy.deepcopy(self.stim)
        self.assertEqual(schedule_manifest(self.steps),schedule_manifest(build_batch_schedule(self.units,self.stim['formal'])))
        other=configure_pair_batches(self.stim,seed=99)
        self.assertNotEqual(schedule_manifest(self.steps),schedule_manifest(build_batch_schedule(self.units,other['formal'])))
        self.assertEqual(self.stim,before)

    def test_plan_duration_covers_references_rest_and_pair_spans(self):
        plan=make_plan(self.system,self.stim,formal_units=self.units)
        self.assertEqual(plan['formal_trial_count'],3960)
        self.assertEqual((plan['primary_trial_count'],plan['reference_trial_count']),(3600,360))
        self.assertAlmostEqual(plan['formal_nominal_duration_sec']/60,101.3296+.25/60,places=5)
        self.assertTrue(all(18*60<r['span_sec']<22*60 for r in plan['pair_time_ranges'].values()))
        longer=copy.deepcopy(self.stim);longer['formal']['trial_interval_sec']=2.
        self.assertAlmostEqual(make_plan(self.system,longer,formal_units=self.units)['formal_nominal_duration_sec']-plan['formal_nominal_duration_sec'],3960)

    def test_invalid_batch_config_rejected_before_any_device(self):
        for key,value in [('reference_trials',0),('schedule_seed',True),('dt_ms',[1,1]),('dt_ms',[float('nan')]),('rest_between_batches_sec',-1)]:
            bad=copy.deepcopy(self.stim);bad['formal'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):validate(self.system,bad)

    def test_existing_legacy_schedule_remains_five_thousand_four_hundred(self):
        system,stim=load_configuration(ROOT/'config')
        self.assertEqual(make_plan(system,stim)['formal_trial_count'],5400)

    def test_mock_recording_manifest_actual_h5_and_stop_before_hardware(self):
        from test_formal_selection import FormalSelectionChecks
        from analysis.utils.io import read_session
        from analysis.utils.recordings import resolve_trials
        fixture=FormalSelectionChecks();fixture.setUp()
        try:
            fixture.system['mock']['trials_per_condition_override']=2
            stim=configure_pair_batches(fixture.stim,reference_trials=2,rest_between_batches_sec=0)
            before=fixture.hashes()
            with patch('python.maxwell_setup.MaxLabBackend') as hardware,patch('python.experiment_runner.unit_map'):
                dry=execute(fixture.system,stim,formal_selection=fixture.file,dry_run=True)
                hardware.assert_not_called()
                self.assertFalse(list(dry.rglob('*.raw.h5')))
                # create_run uses second-resolution names; isolate dry/mock roots.
                fixture.system['data']['root']=str(fixture.root/'mock_outputs')
                run=execute(fixture.system,stim,formal_selection=fixture.file)
                hardware.assert_not_called()
            result=json.loads((run/'experiment_result.json').read_text())
            self.assertEqual(result['formal_trial_count'],216);self.assertEqual(result['probe_trial_count'],0)
            h5s=list((run/'raw_data').rglob('*.raw.h5'));self.assertEqual(len(h5s),3)
            directory=run/'raw_data/spatiotemporal'
            path=next(p for p in h5s if p.parent.name=='02_stim')
            trials,annotation=resolve_trials(read_session(path),directory/'02_stim/trial_manifest.csv')
            self.assertEqual(len(trials),216);self.assertEqual(annotation['unreferenced_events'],0)
            with (directory/'02_stim/trial_manifest.csv').open(encoding='utf-8-sig') as f:actual=list(csv.DictReader(f))
            self.assertEqual(sum(bool(r['checkpoint']) for r in actual),72)
            self.assertEqual({r['checkpoint'] for r in actual if r['checkpoint']},{'pre','mid','post'})
            planned=json.loads((directory/'batch_schedule.json').read_text())
            self.assertEqual(planned['formal_trial_count'],216)
            self.assertEqual(before,fixture.hashes())
        finally:fixture.doCleanups()


if __name__=='__main__':unittest.main()
