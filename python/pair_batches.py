"""Pure two-pair batch schedules; all random choices and reference IDs are recorded."""
from collections import Counter
from dataclasses import asdict
import math
import random

from .schedule import Condition, formal_conditions, next_onset_interval, pulse_duration_sec


def configure_pair_batches(stimulation, *, dt_ms=(1,5,10,50,100), reference_trials=10,
                           seed=42, rest_between_batches_sec=300.):
    """Return a copy, without editing acquisition YAML or electrode selections."""
    import copy
    result=copy.deepcopy(stimulation)
    result['formal'].update(schedule_mode='pair_batches', condition_order='balanced_seeded',
        dt_ms=list(dt_ms),reference_trials=reference_trials,schedule_seed=seed,
        rest_between_batches_sec=rest_between_batches_sec,temporal_interval_mode='after_second')
    validate_batch_config(result['formal'])
    return result


def validate_batch_config(cfg):
    for key in ('trials_per_condition','reference_trials'):
        if isinstance(cfg.get(key),bool) or not isinstance(cfg.get(key),int) or cfg[key]<1:
            raise ValueError(f'{key}必须为正整数')
    if isinstance(cfg.get('schedule_seed'),bool) or not isinstance(cfg.get('schedule_seed'),int):
        raise ValueError('schedule_seed必须为整数')
    dt=cfg.get('dt_ms')
    if not isinstance(dt,list) or not dt or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<=0 for x in dt) or len(set(dt))!=len(dt):
        raise ValueError('dt_ms须为非重复有限正数列表')
    rest=cfg.get('rest_between_batches_sec')
    if isinstance(rest,bool) or not isinstance(rest,(int,float)) or not math.isfinite(rest) or rest<0:
        raise ValueError('批间休息须为有限非负秒数')
    if cfg.get('temporal_interval_mode')!='after_second' or cfg.get('condition_order')!='balanced_seeded':
        raise ValueError('两对一批要求最后脉冲结束后静息与有种子的平衡交错')


def build_batch_schedule(units,cfg,*,rounds=None):
    validate_batch_config(cfg)
    n=cfg['trials_per_condition'] if rounds is None else rounds
    if isinstance(n,bool) or not isinstance(n,int) or n<1:raise ValueError('轮数必须为正整数')
    stages=formal_conditions(units,cfg['dt_ms'])
    singles,sync=stages[0][1],stages[1][1]
    all_temporal=[c for _,cs in stages[2:] for c in cs]
    if len({e for u in units for e in u['electrodes']})!=6:raise ValueError('需要6个不同电极')
    # A cross-unit pair and the complementary local pair never share a site.
    pairing=((3,2),(4,1),(5,0))
    batches=[]
    for index,pair_ids in enumerate(pairing,1):
        pairs=[sync[i] for i in pair_ids]
        if set(pairs[0].electrodes)&set(pairs[1].electrodes):raise ValueError('批内两对电极不能共享位点')
        batches.append(dict(batch_id=f'batch_{index}',pairs=pairs))
    rng=random.Random(cfg['schedule_seed']);rng.shuffle(batches)
    steps=[]
    def add(c,stage,round_index,**context):
        steps.append(dict(action='trial',condition=c,stage=stage,round=round_index,
                          batch_id=context.get('batch_id',''),pair_id=context.get('pair_id',''),
                          checkpoint=context.get('checkpoint','')))
    for r in range(1,n+1):
        chosen=list(singles);rng.shuffle(chosen)
        for c in chosen:add(c,'single',r)
    for batch_index,batch in enumerate(batches):
        bid=batch['batch_id'];pairs=batch['pairs']
        sites=sorted({e for pair in pairs for e in pair.electrodes})
        labels={c.electrodes[0]:c.labels[0] for c in singles}
        def reference(checkpoint):
            for r in range(1,cfg['reference_trials']+1):
                chosen=list(sites);rng.shuffle(chosen)
                for e in chosen:
                    c=Condition(f'{bid}_ref_{checkpoint}_{labels[e]}','single',(e,),(labels[e],),relation='drift_reference')
                    add(c,f'{bid}_reference_{checkpoint}',r,batch_id=bid,checkpoint=checkpoint)
        reference('pre')
        for r in range(1,n+1):
            order=list(pairs)
            if r%2==0:order.reverse()
            for pair in order:
                # Eleven trials: one synchronous plus every dt in both directions.
                conditions=[pair]+[c for c in all_temporal if set(c.electrodes)==set(pair.electrodes)]
                rng.shuffle(conditions)
                for c in conditions:add(c,bid,r,batch_id=bid,pair_id=pair.name)
            if n>1 and r==n//2:reference('mid')
        # With a one-round software fixture the midpoint coincides with pre.
        if n==1:reference('mid')
        reference('post')
        if batch_index<len(batches)-1:
            steps.append(dict(action='rest',seconds=float(cfg['rest_between_batches_sec']),stage=bid,
                              batch_id=bid,pair_id='',checkpoint=''))
    return steps


def schedule_manifest(steps):
    return [dict(schedule_index=i,stage=s['stage'],round=s.get('round'),batch_id=s['batch_id'],
                 pair_id=s['pair_id'],checkpoint=s['checkpoint'],**asdict(s['condition']))
            for i,s in enumerate(steps,1) if s['action']=='trial']


def condition_manifest(steps):
    rows={}
    for s in steps:
        if s['action']!='trial':continue
        c=s['condition'];key=c.name
        if key not in rows:rows[key]=dict(stage=s['stage'],**asdict(c),planned_trials=0)
        rows[key]['planned_trials']+=1
    return list(rows.values())


def batch_plan(steps,system,stimulation):
    cfg=stimulation['formal'];protocol=stimulation['protocol']
    overhead=protocol['sequence_initial_delay_samples']*50e-6+system['routing']['settle_wait_ms']/1000
    pulse=pulse_duration_sec(protocol);elapsed=0.;ranges={};primary=refs=0
    for s in steps:
        if s['action']=='rest':elapsed+=s['seconds'];continue
        c=s['condition'];cost=next_onset_interval(c,cfg,pulse)+overhead
        is_reference=c.relation=='drift_reference';refs+=int(is_reference);primary+=int(not is_reference)
        if s['pair_id']:
            r=ranges.setdefault(s['pair_id'],dict(first_sec=elapsed,last_sec=elapsed,batch_id=s['batch_id']))
            r['last_sec']=elapsed
        elapsed+=cost
    for r in ranges.values():r['span_sec']=r['last_sec']-r['first_sec']
    return dict(schedule_mode='pair_batches',primary_trial_count=primary,reference_trial_count=refs,
        formal_trial_count=primary+refs,formal_nominal_duration_sec=elapsed+cfg['pre_spontaneous_sec']+
        cfg['post_spontaneous_sec']+cfg['recording_tail_sec'],pair_time_ranges=ranges,
        stimulation_schedule_duration_sec=elapsed,stages=[dict(stage=k,trials=v) for k,v in Counter(
            s['stage'] for s in steps if s['action']=='trial').items()])


def run_pair_batches(runner):
    """Reuse the existing per-trial guard, routing pool and three-segment recorder."""
    from pathlib import Path
    from .utils.persistence import write_csv,atomic_json
    cfg=runner.stimulation['formal']
    rounds=cfg['trials_per_condition']
    if not runner.backend.realtime and runner.system['mock'].get('trials_per_condition_override'):
        rounds=runner.system['mock']['trials_per_condition_override']
    steps=build_batch_schedule(runner.formal_units,cfg,rounds=rounds)
    total=sum(s['action']=='trial' for s in steps);runner.global_total=runner.completed_trials+total
    block='spatiotemporal';directory=runner.run_dir/'raw_data'/block;directory.mkdir(parents=True,exist_ok=True)
    write_csv(directory/'condition_manifest.csv',condition_manifest(steps))
    write_csv(directory/'schedule_manifest.csv',schedule_manifest(steps))
    atomic_json(directory/'batch_schedule.json',batch_plan(steps,runner.system,runner.stimulation))
    runner.backend.route([e for u in runner.formal_units for e in u['electrodes']])
    runner.spontaneous(directory/'01_pre_spont',block,'01_pre_spont',cfg['pre_spontaneous_sec'])
    runner.start_segment(directory/'02_stim',block,'02_stim',total)
    deadline=None;count=0
    def drain():
        if runner.backend.realtime and deadline is not None:runner.reporter.wait_until(deadline)
        runner._formal_sequence=None
    for index,s in enumerate(steps,1):
        runner.reporter.check()
        if s['action']=='rest':
            drain()
            runner.backend.disconnect_active() if runner.backend.realtime else runner.backend.switch(())
            runner.reporter.wait(s['seconds'],'批间休息并继续录制',realtime=runner.backend.realtime)
            if not runner.backend.realtime:runner.backend.advance(s['seconds'])
            deadline=None
            continue
        runner._trial_context={k:s[k] for k in ('batch_id','pair_id','checkpoint')}
        runner._trial_context['schedule_index']=index
        count+=1;c=s['condition']
        period=next_onset_interval(c,cfg,pulse_duration_sec(runner.stimulation['protocol']))
        deadline=runner.one_trial(c,s['round'],period,count,total,s['stage'],deadline)
    drain();runner._trial_context={}
    runner.backend.disconnect_active() if runner.backend.realtime else runner.backend.switch(())
    runner.reporter.wait(cfg['recording_tail_sec'],'刺激段尾部录制',realtime=runner.backend.realtime)
    if not runner.backend.realtime:runner.backend.advance(cfg['recording_tail_sec'])
    runner.finish_segment(total)
    runner.spontaneous(directory/'03_post_spont',block,'03_post_spont',cfg['post_spontaneous_sec'])
    return batch_plan(steps,runner.system,runner.stimulation)
