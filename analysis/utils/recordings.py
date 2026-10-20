"""File-independent spike-recording summaries and event-aligned trial analysis.

No runner, hardware SDK, culture ID, directory convention or acquisition config.
All stimulus onsets come from Session.events, never annotation timestamps.
"""
from __future__ import annotations

from collections import defaultdict
import csv
import json
import math
import numpy as np

from .io import BOUNDARY_IDS, _unique_event_properties
from .detection.burst import detect_burst_intervals, merge_intervals


DEFAULT_PARAMETERS = dict(
    population_bin_sec=1., baseline_window_ms=[-50., -10.], response_window_ms=[10., 50.],
    response_ratio=1.5, artifact_window_ms=[0., 3.], artifact_diagnostic_threshold=200.,
    psth_window_ms=[-50., 200.], psth_bin_ms=2., raster_max_spikes=200000,
    memory_budget_gb=4., burst=dict(bin_ms=1., smooth_ms=50., threshold_z=6.,
        min_duration_ms=30., merge_gap_ms=30., min_spikes=5,density_fallback=False))


def validate_parameters(p):
    for key in ('population_bin_sec','response_ratio','psth_bin_ms','memory_budget_gb'):
        if isinstance(p[key], bool) or not math.isfinite(float(p[key])) or float(p[key]) <= 0:
            raise ValueError(f'{key}必须为有限正数')
    for key in ('baseline_window_ms','response_window_ms','artifact_window_ms','psth_window_ms'):
        v = np.asarray(p[key], dtype=float)
        if v.shape != (2,) or not np.isfinite(v).all() or v[0] >= v[1]:
            raise ValueError(f'{key}必须为递增的有限数值窗口')
    if p['baseline_window_ms'][1] > 0 or p['response_window_ms'][0] < p['artifact_window_ms'][1]:
        raise ValueError('背景窗须在第一发前，响应窗须在最后一发伪迹窗之后')
    if not np.isclose(np.diff(p['baseline_window_ms'])[0],np.diff(p['response_window_ms'])[0]):
        raise ValueError('配对计数的前后窗口必须等宽')
    if int(p['raster_max_spikes']) != p['raster_max_spikes'] or p['raster_max_spikes'] < 1:
        raise ValueError('raster_max_spikes必须为正整数')
    if list(p['artifact_window_ms']) != [0.,3.]:
        raise ValueError('本入口的伪迹诊断窗固定为[0,3)ms，另统计[0,2)ms')
    if (not math.isfinite(float(p['artifact_diagnostic_threshold']))
            or p['artifact_diagnostic_threshold'] < 0):
        raise ValueError('伪迹诊断门槛必须为非负有限数值')
    burst = p['burst']
    for key in ('bin_ms','smooth_ms','threshold_z','min_duration_ms','min_spikes'):
        if isinstance(burst[key],bool) or not math.isfinite(float(burst[key])) or burst[key] <= 0:
            raise ValueError(f'burst.{key}必须为有限正数')
    if burst['bin_ms'] < 1 or burst['merge_gap_ms'] < 0 or not math.isfinite(burst['merge_gap_ms']):
        raise ValueError('burst bin至少1ms，merge_gap必须非负且有限')
    if not isinstance(burst['density_fallback'],bool): raise ValueError('density_fallback必须为布尔值')


def _counts(times, starts, ends):
    return np.searchsorted(times,ends,side='left')-np.searchsorted(times,starts,side='left')


def summarize_activity(session, parameters):
    """Use full duration and every mapped electrode, including silent channels."""
    duration = float(session.duration_sec)
    if not math.isfinite(duration) or duration <= 0 or not session.mapping:
        raise ValueError('录制时长或记录映射无效')
    if (len({r['channel'] for r in session.mapping})!=len(session.mapping)
            or len({r['electrode'] for r in session.mapping})!=len(session.mapping)):
        raise ValueError('H5记录映射中的通道或电极重复')
    if len(session.times) and (session.times[0]<0 or session.times[-1]>=duration):
        raise ValueError('分析spikes超出明确的录制区间，须先显式标记并排除区间外数据')
    channels, counts = np.unique(session.channels,return_counts=True)
    count_by_channel = dict(zip(channels.tolist(),counts.tolist()))
    electrodes = [dict(electrode=r['electrode'],channel=r['channel'],x_um=r['x'],y_um=r['y'],
        spike_count=count_by_channel.get(r['channel'],0),
        firing_rate_hz=count_by_channel.get(r['channel'],0)/duration) for r in session.mapping]
    bursts, diagnostics = detect_burst_intervals({0:session.times},**parameters['burst'])
    burst_rows = [dict(burst_id=i,start_sec=a,end_sec=b,duration_ms=(b-a)*1000,
        spike_count=int(np.searchsorted(session.times,b,side='right')-
                        np.searchsorted(session.times,a,side='left'))) for i,(a,b) in enumerate(bursts,1)]
    width = float(parameters['population_bin_sec'])
    edges = np.r_[np.arange(0,duration,width),duration]
    if len(edges) < 2: edges = np.array([0.,duration])
    population = [dict(start_sec=float(a),end_sec=float(b),
        spike_count=int(n),population_rate_hz=float(n/(b-a)))
        for a,b,n in zip(edges[:-1],edges[1:],_counts(session.times,edges[:-1],edges[1:]))]
    summary = dict(duration_sec=duration,sampling_hz=float(session.sampling_hz),
        mapped_electrodes=len(electrodes),active_electrodes=int(len(channels)),
        spike_count=int(len(session.times)),population_rate_hz=len(session.times)/duration,
        mean_electrode_rate_hz=len(session.times)/duration/len(electrodes),
        active_fraction=len(channels)/len(electrodes),burst_count=len(bursts),
        burst_rate_per_min=len(bursts)*60/duration,
        median_burst_duration_ms=float(np.median([r['duration_ms'] for r in burst_rows])) if bursts else None,
        burst_spike_fraction=sum(r['spike_count'] for r in burst_rows)/len(session.times) if len(session.times) else 0.,
        burst_high_threshold=float(diagnostics['high_threshold']) if diagnostics else None,
        burst_threshold_scope='adaptive per recording; same detector parameters, descriptive comparison',
        time_origin_source=session.time_origin_source,frame_origin=int(session.frame_origin),
        unmapped_spike_count=int(session.unmapped_spike_count))
    return summary,electrodes,burst_rows,population


def _properties(message):
    message = str(message).strip()
    if message.startswith('{'):
        value = json.loads(message,object_pairs_hook=_unique_event_properties)
        if not isinstance(value,dict): raise ValueError('事件属性不是JSON对象')
        return value
    words = message.split()
    if len(words)%2: raise ValueError('事件属性不是键值对')
    if len(set(words[::2])) != len(words[::2]): raise ValueError('事件属性键重复')
    return dict(zip(words[::2],words[1::2]))


def stimulus_events(session):
    """Boundary markers are annotations, never stimulation pulses."""
    return sorted([e for e in session.events if e['event_id'] not in BOUNDARY_IDS.values()],
                  key=lambda e:e['time_sec'])


def _int_list(value, label):
    if value in ('',None): return []
    data = json.loads(value) if isinstance(value,str) else value
    if isinstance(data,int) and not isinstance(data,bool): data = [data]
    if not isinstance(data,list) or any(isinstance(x,bool) or not isinstance(x,int) for x in data):
        raise ValueError(f'{label}必须为整数JSON列表')
    return data


def resolve_trials(session, table=None):
    """Optional table supplies labels/IDs only; timing is always re-read from H5."""
    events = stimulus_events(session)
    lookup = defaultdict(list)
    for event in events: lookup[int(event['event_id'])].append(event)
    if any(len(v)!=1 for v in lookup.values()):
        raise ValueError('刺激event_id在H5不唯一，禁止猜测配对')
    rows = []
    if table:
        with open(table,encoding='utf-8-sig',newline='') as handle:
            supplied = list(csv.DictReader(handle))
        if not supplied: raise ValueError('trial表为空')
        for row in supplied:
            rows.append(dict(trial_id=row.get('trial_id'),condition=row.get('condition'),kind=row.get('kind'),
                dt_ms=row.get('dt_ms',0) or 0,event_ids=_int_list(row.get('event_ids'),'event_ids'),
                electrodes=_int_list(row.get('electrodes'),'electrodes'),stage=row.get('stage',''),
                relation=row.get('relation',''),round=row.get('round','')))
        source = 'explicit_trial_table_with_h5_timing'
    else:
        i = 0
        while i < len(events):
            event = events[i]
            props = _properties(event['eventmessage'])
            kind = props.get('kind',props.get('type'))
            condition = props.get('condition')
            if not condition or kind not in ('single','synchronous','temporal'):
                raise ValueError('H5缺少condition/kind标签；请提供trial表，不能从路径猜条件')
            pulse = int(props.get('pulse',1))
            if pulse != 1: raise ValueError('H5中trial的第一发标记不是pulse=1')
            ids = [int(event['event_id'])]
            dt = float(props.get('dt_ms',0))
            if kind == 'temporal':
                if 'pulse' not in props or 'dt_ms' not in props or i+1 >= len(events):
                    raise ValueError('双脉冲缺少pulse/dt_ms或第二发，不能自动配对')
                second = _properties(events[i+1]['eventmessage'])
                if (second.get('condition') != condition or second.get('kind',second.get('type')) != kind
                        or int(second.get('pulse',0)) != 2 or float(second.get('dt_ms',-1)) != dt):
                    raise ValueError('双脉冲的两个H5事件不匹配')
                ids.append(int(events[i+1]['event_id'])); i += 1
            rows.append(dict(trial_id=len(rows)+1,condition=condition,kind=kind,dt_ms=dt,
                             event_ids=ids,electrodes=[],stage='',relation='',round=''))
            i += 1
        source = 'h5_event_properties; stimulation electrode IDs unavailable'
    used,trial_ids = set(),set()
    for row in rows:
        if row['condition'] is None or not str(row['condition']).strip(): raise ValueError('trial缺少condition')
        if row['trial_id'] in (None,'') or str(row['trial_id']) in trial_ids: raise ValueError('trial_id缺失或重复')
        trial_ids.add(str(row['trial_id']))
        kind,ids = row['kind'],row['event_ids']
        if kind not in ('single','synchronous','temporal') or len(ids) != (2 if kind=='temporal' else 1):
            raise ValueError('trial类型或事件数量无效；同步双电极使用一个共同时间事件')
        if used.intersection(ids) or len(set(ids)) != len(ids): raise ValueError('event_id被多个trial重复引用')
        if any(e not in lookup for e in ids): raise ValueError('trial引用的event_id在H5缺失；禁止使用主机估计时间')
        used.update(ids)
        onsets = [float(lookup[e][0]['time_sec']) for e in ids]
        if min(onsets)<0 or max(onsets)>=session.duration_sec: raise ValueError('刺激事件在录制区间外')
        dt = float(row['dt_ms'])
        if not math.isfinite(dt) or dt<0: raise ValueError('trial dt无效')
        if kind!='temporal' and dt!=0: raise ValueError('单脉冲/同步条件的dt必须为0')
        if kind=='temporal' and (onsets[1]<=onsets[0] or
                abs((onsets[1]-onsets[0])*1000-dt)>max(.05,1000/session.sampling_hz)+1e-6):
            raise ValueError('H5实际双脉冲间隔与标注dt不一致')
        if table:
            for pulse_index,event_id in enumerate(ids,1):
                try: properties = _properties(lookup[event_id][0]['eventmessage'])
                except (ValueError,TypeError): properties = {}
                if properties.get('condition',row['condition']) != row['condition']:
                    raise ValueError('trial表的condition与H5事件标签冲突')
                if properties.get('kind',properties.get('type',kind)) != kind:
                    raise ValueError('trial表的kind与H5事件标签冲突')
                if 'pulse' in properties and int(properties['pulse']) != pulse_index:
                    raise ValueError('trial表的事件顺序与H5 pulse标签冲突')
                if 'dt_ms' in properties and not np.isclose(float(properties['dt_ms']),dt):
                    raise ValueError('trial表的dt与H5事件标签冲突')
        row.update(onsets_sec=onsets,dt_ms=dt,condition=str(row['condition']))
    rows.sort(key=lambda row:row['onsets_sec'][0])
    return rows,dict(trial_source=source,trial_count=len(rows),stimulus_event_count=len(events),
                     unreferenced_events=len(set(lookup)-used))


def quiet_background(session, bursts, onsets, width=.197):
    """Same-width windows outside detected bursts and every pulse's first 200 ms."""
    if session.duration_sec < width: return dict(windows=0,mean_count=None,window_sec=width)
    intervals = merge_intervals([(r['start_sec'],r['end_sec']) for r in bursts]+
                               [(float(t),float(t)+.2) for t in onsets],0.)
    starts = np.arange(0,max(0.,session.duration_sec-width)+1e-10,width)
    if len(starts)==0: return dict(windows=0,mean_count=None,window_sec=width)
    if intervals:
        a,b = np.array(intervals).T
        ix = np.searchsorted(b,starts,side='right')
        valid = (ix==len(a)) | (a[np.minimum(ix,len(a)-1)]>=starts+width)
        starts = starts[valid]
    counts = _counts(session.times,starts,starts+width)
    return dict(windows=int(len(counts)),mean_count=float(counts.mean()) if len(counts)>=20 else None,
                window_sec=width)


def analyze_responses(session, trials, parameters, bursts, progress=None):
    """Pair-level response: pre FIRST pulse versus post LAST pulse; all mapped spikes."""
    all_onsets = np.array(sorted({e['time_sec'] for e in stimulus_events(session)}),float)
    background = quiet_background(session,bursts,all_onsets)
    before = np.asarray(parameters['baseline_window_ms'])/1000
    after = np.asarray(parameters['response_window_ms'])/1000
    artifact_end = parameters['artifact_window_ms'][1]/1000
    metrics = []
    groups = {}
    for i,trial in enumerate(trials,1):
        first,last = trial['onsets_sec'][0],trial['onsets_sec'][-1]
        pre,post = first+before,last+after
        complete = pre[0]>=0 and post[1]<=session.duration_sec
        unrelated = [t for t in all_onsets[np.searchsorted(all_onsets,pre[0]):
                                          np.searchsorted(all_onsets,post[1])]
                     if t not in trial['onsets_sec'] and
                     (pre[0]<=t<pre[1] or post[0]<=t<post[1])]
        valid = complete and not unrelated
        npre,npost = (int(_counts(session.times,*pre)),int(_counts(session.times,*post))) if valid else (None,None)
        artifact = [int(_counts(session.times,t,t+artifact_end))
                    if t+artifact_end<=session.duration_sec else None for t in trial['onsets_sec']]
        artifact2 = [int(_counts(session.times,t,t+.002))
                     if t+.002<=session.duration_sec else None for t in trial['onsets_sec']]
        extended_clear = not np.any((all_onsets>=last+.003)&(all_onsets<last+.2))
        extended = int(_counts(session.times,last+.003,last+.2)) if last+.2<=session.duration_sec and extended_clear else None
        row = dict(trial,first_onset_sec=first,last_onset_sec=last,
            status='complete' if valid else 'incomplete_window' if not complete else 'other_trial_pulse_in_window',
            baseline_count=npre,response_count=npost,net_count=npost-npre if valid else None,
            success=bool(npost>parameters['response_ratio']*npre) if valid else None,
            artifact_0_3ms_mean=float(np.mean(artifact)) if None not in artifact else None,
            artifact_0_2ms_mean=float(np.mean(artifact2)) if None not in artifact2 else None,
            response_3_200ms_count=extended,
            response_3_200ms_above_nonburst=bool(extended>background['mean_count'])
                if extended is not None and background['mean_count'] is not None else None)
        key = (trial['condition'],trial['kind'],trial['dt_ms'],tuple(trial['electrodes']))
        if key not in groups: groups[key] = dict(group_id=f'c{len(groups)+1:03d}',trials=[],metrics=[])
        groups[key]['trials'].append(trial); groups[key]['metrics'].append(row)
        row['group_id'] = groups[key]['group_id']; metrics.append(row)
        if progress and (i%50==0 or i==len(trials)): progress(i,len(trials),'逐trial计数')
    summaries,psths = [],[]
    lo,hi = parameters['psth_window_ms']; step = parameters['psth_bin_ms']
    edges = np.arange(lo,hi,step)
    edges = np.r_[edges,hi]/1000
    for key,group in groups.items():
        good = [m for m in group['metrics'] if m['status']=='complete']
        def mean(field):
            values = [m[field] for m in group['metrics'] if m[field] is not None]
            return float(np.mean(values)) if values else None
        summary = dict(group_id=group['group_id'],condition=key[0],kind=key[1],dt_ms=key[2],
            relation=group['trials'][0].get('relation',''),
            electrodes=list(key[3]),trials=len(group['metrics']),complete_trials=len(good),
            success_trials=sum(m['success'] for m in good),
            success_fraction=sum(m['success'] for m in good)/len(good) if good else None,
            response_count_mean=mean('response_count'),baseline_count_mean=mean('baseline_count'),
            net_count_mean=mean('net_count'),artifact_0_2ms_mean=mean('artifact_0_2ms_mean'),
            artifact_0_3ms_mean=mean('artifact_0_3ms_mean'),response_3_200ms_mean=mean('response_3_200ms_count'))
        summary['artifact_review'] = (summary['artifact_0_2ms_mean']<parameters['artifact_diagnostic_threshold']
            if summary['artifact_0_2ms_mean'] is not None else None)
        summary['above_nonburst_3_200ms'] = (summary['response_3_200ms_mean']>background['mean_count']
            if summary['response_3_200ms_mean'] is not None and background['mean_count'] is not None else None)
        summary['review_required'] = bool(summary['artifact_review'] is True or
            summary['above_nonburst_3_200ms'] is False or len(good)!=len(group['metrics']))
        summaries.append(summary)
        total = np.zeros(len(edges)-1); exposed = np.zeros(len(edges)-1,int)
        for trial in group['trials']:
            anchor = trial['onsets_sec'][-1]
            absolute = anchor+edges
            valid = (absolute[:-1]>=0)&(absolute[1:]<=session.duration_sec)
            near = all_onsets[np.searchsorted(all_onsets,absolute[0]-artifact_end):
                              np.searchsorted(all_onsets,absolute[-1])]
            for onset in near:
                valid &= ~((absolute[:-1]<onset+artifact_end)&(absolute[1:]>onset))
            total += _counts(session.times,absolute[:-1],absolute[1:])*valid
            exposed += valid
        for a,b,n,coverage in zip(edges[:-1],edges[1:],total,exposed):
            psths.append(dict(group_id=group['group_id'],condition=key[0],start_ms=float(a*1000),
                end_ms=float(b*1000),available_trials=int(coverage),
                population_rate_hz=float(n/coverage/(b-a)) if coverage else None))
    return metrics,summaries,psths,background


def compare_pre_post(pre,post,pre_electrodes,post_electrodes):
    """Descriptive comparison only; no electrodes-as-biological-replicates p-values."""
    for field in ('sampling_hz','gain','spike_threshold'):
        if pre.get(field) is None or post.get(field) is None or not np.isclose(pre[field],post[field],rtol=1e-6,atol=1e-6):
            return dict(status='blocked',reason=f'{field}缺失或不同'),[]
    for field in ('chip_id','well'):
        if pre.get(field) is not None and post.get(field) is not None and pre[field]!=post[field]:
            return dict(status='blocked',reason=f'H5 {field}不同，不作为同一对象前后比较'),[]
    a,b = {r['electrode']:r for r in pre_electrodes},{r['electrode']:r for r in post_electrodes}
    if set(a)!=set(b) or any(a[e]['channel']!=b[e]['channel'] or
            not np.allclose([a[e]['x_um'],a[e]['y_um']],[b[e]['x_um'],b[e]['y_um']],atol=.1,rtol=0) for e in a):
        return dict(status='blocked',reason='前后记录电极映射不同，不比较不同群体'),[]
    fields = ('population_rate_hz','mean_electrode_rate_hz','active_fraction',
              'burst_rate_per_min','median_burst_duration_ms','burst_spike_fraction')
    changes = {field:dict(pre=pre[field],post=post[field],
        delta=post[field]-pre[field] if pre[field] is not None and post[field] is not None else None) for field in fields}
    rows = [dict(electrode=e,pre_rate_hz=a[e]['firing_rate_hz'],post_rate_hz=b[e]['firing_rate_hz'],
                 delta_hz=b[e]['firing_rate_hz']-a[e]['firing_rate_hz']) for e in sorted(a)]
    return dict(status='descriptive',changes=changes,
                identity_from_h5=pre.get('chip_id') is not None and post.get('chip_id') is not None,
                note='同参数逐段检测burst；单个前后录制对，不作培养物层面显著性或因果推断'),rows
