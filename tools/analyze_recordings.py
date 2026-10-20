"""Independent offline analysis: edit file paths below, then use one fixed command."""
from __future__ import annotations

import copy
import csv
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from analysis.utils.io import read_session, select_segment
from analysis.utils.recordings import (DEFAULT_PARAMETERS, validate_parameters, summarize_activity,
    resolve_trials, analyze_responses, stimulus_events, compare_pre_post)
from python.utils.persistence import atomic_json, write_csv
from tools.recording_figures import recording_figures, response_figures, quality_figure, comparison_figure

# Edit only this section. Any closed H5 may live anywhere; empty slots are ignored.
RECORDINGS = [
    dict(name='pre', role='pre', h5='', cfg='', trials=''),
    dict(name='stim', role='stim', h5='', cfg='', trials=''),
    dict(name='post', role='post', h5='', cfg='', trials=''),
]
CFG_PATH = ''  # Optional shared CFG; per-recording cfg overrides it. H5 mapping works without CFG.
OUTPUT_ROOT = ''  # Empty: project data/recording_analysis. Each run gets a NEW subdirectory.
PARAMETERS = copy.deepcopy(DEFAULT_PARAMETERS)
PARAMETERS.update(population_bin_sec=1.0, baseline_window_ms=[-50.,-10.],
    response_window_ms=[10.,50.], response_ratio=1.5, psth_window_ms=[-50.,200.],
    psth_bin_ms=2.0, raster_max_spikes=200000, memory_budget_gb=4.0)
PARAMETERS['burst'].update(bin_ms=1.0, smooth_ms=50.0, threshold_z=6.0,
    min_duration_ms=30.0, merge_gap_ms=30.0, min_spikes=5, density_fallback=False)
# End editable section. No culture ID, old run, runner, SDK or hardware flag is needed.


def _path(value):
    p = Path(value).expanduser()
    return (p if p.is_absolute() else ROOT/p).resolve()


@contextmanager
def _heartbeat(logger,label):
    done = threading.Event(); started = time.monotonic()
    def beat():
        while not done.wait(10):
            logger.info('当前文件/子任务 %s | 仍在处理，已用%.0fs',label,time.monotonic()-started)
    thread = threading.Thread(target=beat,daemon=True); thread.start()
    try: yield
    finally: done.set(); thread.join()


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda:handle.read(8*1024*1024),b''): digest.update(block)
    return digest.hexdigest()


def _write_table(path,rows,fields=None):
    converted = [{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v
                  for k,v in row.items()} for row in rows]
    write_csv(path,converted,fields=fields)


def _electrodes(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as handle: rows = list(csv.DictReader(handle))
    return [{k:int(v) if k in ('electrode','channel','spike_count') else float(v)
             for k,v in row.items()} for row in rows]


def _settings(path,well):
    import h5py
    import numpy as np
    result = {}
    with h5py.File(path,'r') as handle:
        segment = select_segment(handle,well)
        chip = handle.get('wellplate/id')
        chip_value = np.asarray(chip[()]).reshape(-1)[0] if chip is not None and chip.size==1 else None
        result['chip_id'] = chip_value.decode('utf-8').strip() if isinstance(chip_value,bytes) else str(chip_value) if chip_value is not None else None
        result['well'] = int(well)
        for field in ('gain','spike_threshold'):
            dataset = segment['settings'].get(field)
            values = np.asarray(dataset[()],dtype=float).reshape(-1) if dataset is not None and 0<dataset.size<=1024 else []
            result[field] = float(values[0]) if len(values) and np.isfinite(values).all() and np.unique(values).size==1 else None
        result['contains_raw'] = any('raw' in g for g in segment.get('groups',{}).values())
        result['spikes_on_disk'] = int(segment['spikes'].size)
    return result


def _cache_valid(directory,key):
    try:
        manifest = json.loads((directory/'cache_manifest.json').read_text(encoding='utf-8'))
        if directory.is_symlink() or manifest['fingerprint']!=key or not manifest['files']: return False
        for relative,expected in manifest['files'].items():
            p = Path(relative)
            if p.is_absolute() or '..' in p.parts: return False
            target = directory/p
            if target.is_symlink() or _hash(target)!=expected: return False
        return True
    except (OSError,ValueError,KeyError): return False


def _quality(name,role,trials,summaries,background,annotation):
    import numpy as np
    def mean(field):
        values = [r[field] for r in trials if r[field] is not None]
        return float(np.mean(values)) if values else None
    artifact,extended = mean('artifact_0_2ms_mean'),mean('response_3_200ms_count')
    quiet = background.get('mean_count')
    notes = []
    if annotation.get('unreferenced_events',0): notes.append('trial表未覆盖全部H5事件')
    if any(r['status']!='complete' for r in trials): notes.append('部分trial窗口不完整或包含其他trial脉冲')
    return dict(name=name,role=role,status='review' if notes else 'ok',notes=notes,
        artifact_0_2ms_mean=artifact,artifact_0_3ms_mean=mean('artifact_0_3ms_mean'),
        response_3_200ms_mean=extended,nonburst_3_200ms_mean=quiet,
        above_quiet_baseline=bool(extended>quiet) if extended is not None and quiet is not None else None)


def run_analysis(recordings,parameters,output_root,*,cfg_path='',render=True,use_cache=True):
    """Read explicit files, write a new output run, and reuse content-validated results."""
    import numpy as np
    parameters = copy.deepcopy(parameters); validate_parameters(parameters)
    inputs = [copy.deepcopy(r) for r in recordings if r.get('h5')]
    if not inputs: raise ValueError('请在tools/analyze_recordings.py顶部填写至少一个h5路径')
    names,paths = set(),set()
    for row in inputs:
        if not re.fullmatch(r'[A-Za-z0-9_-]+',str(row.get('name',''))):
            raise ValueError('name请用英文、数字、下划线或短横线，以便图中使用英文标签')
        if row.get('role') not in ('pre','stim','post','spontaneous'): raise ValueError('role须为pre/stim/post/spontaneous')
        row['h5'] = str(_path(row['h5'])); row['well'] = int(row.get('well',0))
        row['cfg'] = str(_path(row.get('cfg') or cfg_path)) if row.get('cfg') or cfg_path else ''
        row['trials'] = str(_path(row['trials'])) if row.get('trials') else ''
        if row['name'] in names or row['h5'] in paths: raise ValueError('name或H5路径重复，禁止把同一文件当作前后两段')
        names.add(row['name']); paths.add(row['h5'])
    output_root = _path(output_root)
    output = output_root/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_analysis')
    output.mkdir(parents=True,exist_ok=False)
    logger = logging.getLogger('independent_recording_analysis'); logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(output/'analysis.log',encoding='utf-8')
    console = logging.StreamHandler(); formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    for handler in (file_handler,console): handler.setFormatter(formatter); logger.addHandler(handler)
    logger.info('独立离线分析 | 新结果目录：%s | 不连接设备',output)
    started = time.monotonic(); qualities,results,sources = [],[],[]
    algorithm = {str(p.relative_to(ROOT)):_hash(p) for p in
        (Path(__file__),ROOT/'analysis/utils/recordings.py',ROOT/'analysis/utils/io.py',
         ROOT/'analysis/utils/detection/burst.py',ROOT/'tools/recording_figures.py')}
    config = dict(recordings=inputs,parameters=parameters,output_root=str(output_root),algorithm_hashes=algorithm,
        render=render,analysis_scope='spike activity/bursts/trial responses/descriptive pre-post; no network reconstruction or ESRC',
        interval_policy='pre FIRST pulse; post LAST pulse; left-closed/right-open; artifact is diagnostic only')
    import yaml
    (output/'config.yaml').write_text(yaml.safe_dump(config,allow_unicode=True,sort_keys=False),encoding='utf-8')
    try:
        for index,row in enumerate(inputs,1):
            name,role = row['name'],row['role']; destination = output/name
            session = None
            logger.info('总进度[%s] %d/%d | 当前文件 %s | %s','#'*(index-1)+'-'*(len(inputs)-index+1),index-1,len(inputs),name,row['h5'])
            try:
                with _heartbeat(logger,name+' 输入哈希/缓存核对'):
                    hashes = {key:_hash(row[key]) for key in ('h5','cfg','trials') if row.get(key)}
                sources.append(dict(**row,sha256=hashes))
                key = hashlib.sha256(json.dumps(dict(inputs=row,hashes=hashes,parameters=parameters,
                    algorithm=algorithm,render=render),sort_keys=True).encode()).hexdigest()
                cache = output_root/'.cache'/key
                with _heartbeat(logger,name+' 缓存完整性核对'):
                    hit = use_cache and _cache_valid(cache,key)
                if hit:
                    shutil.copytree(cache,destination,ignore=shutil.ignore_patterns('cache_manifest.json'))
                    summary = json.loads((destination/'summary.json').read_text(encoding='utf-8'))
                    quality = json.loads((destination/'quality.json').read_text(encoding='utf-8'))
                    quality.update(cache_hit=True,raster_path=str(destination/'activity.png') if render else '')
                    atomic_json(destination/'quality.json',quality)
                    qualities.append(quality); results.append(dict(input=row,summary=summary,directory=destination))
                    logger.info('当前文件 %s | 子任务[####################] 复用已验证缓存，不重复质检/计数/绘图',name)
                    continue
                destination.mkdir()
                settings = _settings(row['h5'],row['well'])
                estimated_gb = settings['spikes_on_disk']*96/1024**3
                logger.info('当前文件 %s | 子任务[###-----------------] 读取spikes/映射；%d spikes，内存估计约%.2fGiB',name,settings['spikes_on_disk'],estimated_gb)
                if estimated_gb>parameters['memory_budget_gb']:
                    raise ValueError(f"估计内存{estimated_gb:.2f}GiB超过预算；确认可用内存后调整memory_budget_gb")
                with _heartbeat(logger,name+' H5读取与排序'):
                    session = read_session(row['h5'],row['cfg'] or None,row['well'])
                inside = (session.times>=0)&(session.times<session.duration_sec)
                outside = int(np.count_nonzero(~inside))
                if outside:
                    from dataclasses import replace
                    session = replace(session,times=session.times[inside],channels=session.channels[inside],
                        amplitudes=session.amplitudes[inside] if session.amplitudes is not None else None)
                del inside
                logger.info('当前文件 %s | 子任务[#######-------------] 活动和burst；区间%.3fs，%d通道',name,session.duration_sec,len(session.mapping))
                with _heartbeat(logger,name+' 活动/burst计算'):
                    summary,electrodes,bursts,population = summarize_activity(session,parameters)
                summary.update(settings)
                summary['out_of_interval_spikes'] = outside
                summary.update(role=role,burst_interpretation='stim includes evoked/artifact activity' if role=='stim' else 'spontaneous segment')
                _write_table(destination/'electrode_metrics.csv',electrodes)
                _write_table(destination/'bursts.csv',bursts,['burst_id','start_sec','end_sec','duration_ms','spike_count'])
                _write_table(destination/'population_rate.csv',population)
                quality = dict(name=name,role=role,status='ok',notes=[],artifact_0_2ms_mean=None,
                    artifact_0_3ms_mean=None,response_3_200ms_mean=None,above_quiet_baseline=None)
                if role=='stim':
                    try:
                        logger.info('当前文件 %s | 子任务[##########----------] 从H5事件解析trial',name)
                        trials,annotation = resolve_trials(session,row['trials'] or None)
                        if not trials: raise ValueError('没有可用于分析的刺激事件；请提供事件标记或标注表')
                        def progress(done,total,label):
                            if done==total or done%500==0:
                                logger.info('当前文件 %s | %s [%s] %d/%d',name,label,'#'*int(20*done/total)+'-'*(20-int(20*done/total)),done,total)
                        with _heartbeat(logger,name+' 刺激响应/PSTH'):
                            metrics,conditions,psths,background = analyze_responses(session,trials,parameters,bursts,progress)
                        quality = _quality(name,role,metrics,conditions,background,annotation)
                        if quality['artifact_0_2ms_mean'] is not None and quality['artifact_0_2ms_mean']<parameters['artifact_diagnostic_threshold']:
                            quality['status']='review'; quality['notes'].append('低伪迹仅作诊断，不判定未送达、不排除trial')
                        if quality['above_quiet_baseline'] is False:
                            quality['status']='review'; quality['notes'].append('3–200ms均值未超过非burst背景，需复核')
                        if background['mean_count'] is None:
                            quality['status']='review'; quality['notes'].append('无足够非burst/非诱发背景窗口，宽窗比较不可用')
                        summary.update(annotation,conditions=len(conditions),quiet_background=background,
                                       response_reference='paired first-pre/last-post; not isolated second-pulse response')
                        _write_table(destination/'trial_responses.csv',metrics)
                        _write_table(destination/'condition_summary.csv',conditions)
                        _write_table(destination/'psth.csv',psths)
                        if render:
                            with _heartbeat(logger,name+' 条件PSTH/漂移绘图'):
                                response_figures(metrics,conditions,psths,destination,name)
                    except (ValueError,KeyError,TypeError,OSError) as exc:
                        quality.update(status='response_blocked',notes=[str(exc)])
                        summary['response_error']=str(exc)
                        logger.warning('当前文件 %s | 响应分析受阻：%s；保留基础活动结果',name,exc)
                if session.unmapped_spike_count:
                    quality['notes'].append(f'发现{session.unmapped_spike_count}个未映射spike')
                    if quality['status']=='ok': quality['status']='review'
                if outside:
                    quality['notes'].append(f'{outside}个spike在明确录制区间外；已排除，不改写输入')
                    if quality['status']=='ok': quality['status']='review'
                if render:
                    logger.info('当前文件 %s | 子任务[################----] 保存600dpi活动图',name)
                    with _heartbeat(logger,name+' 活动/空间绘图'):
                        recording_figures(session,population,electrodes,bursts,destination,name,parameters['raster_max_spikes'])
                del session
                quality.update(cache_hit=False,raster_path=str(destination/'activity.png') if render else '')
                atomic_json(destination/'summary.json',summary); atomic_json(destination/'quality.json',quality)
                qualities.append(quality); results.append(dict(input=row,summary=summary,directory=destination))
                if use_cache and quality['status']!='response_blocked' and not cache.exists():
                    cache.parent.mkdir(parents=True,exist_ok=True); shutil.copytree(destination,cache)
                    files = {str(p.relative_to(cache)):_hash(p) for p in cache.rglob('*') if p.is_file()}
                    atomic_json(cache/'cache_manifest.json',dict(fingerprint=key,files=files))
            except Exception as exc:
                logger.exception('当前文件 %s | 分析失败',name)
                qualities.append(dict(name=name,role=role,status='file_error',notes=[str(exc)],raster_path=''))
            finally:
                session = None
            logger.info('总进度[%s] %d/%d | 当前文件 %s 完成','#'*index+'-'*(len(inputs)-index),index,len(inputs),name)
        pre = [r for r in results if r['input']['role']=='pre']; post = [r for r in results if r['input']['role']=='post']
        comparison = dict(status='not_requested',reason='需要恰好一个pre和一个post文件才进行配对比较')
        if len(pre)==len(post)==1:
            comparison,paired = compare_pre_post(pre[0]['summary'],post[0]['summary'],
                _electrodes(pre[0]['directory']/'electrode_metrics.csv'),_electrodes(post[0]['directory']/'electrode_metrics.csv'))
            if paired:
                _write_table(output/'pre_post_electrodes.csv',paired)
                if render: comparison_figure(paired,output/'pre_post_rates')
        atomic_json(output/'pre_post_summary.json',comparison)
        _write_table(output/'data_check.csv',qualities)
        if render: quality_figure(qualities,output/'data_check')
        atomic_json(output/'source_inputs.json',dict(inputs=sources))
        status = 'partial' if any(r['status']=='file_error' for r in qualities) else 'complete_with_review' if (
            any(r['status']!='ok' for r in qualities) or comparison['status']=='blocked') else 'complete'
        result = dict(status=status,files=qualities,comparison=comparison,output=str(output),
            elapsed_sec=time.monotonic()-started,hardware_connected=False,esrc_performed=False)
        atomic_json(output/'analysis_result.json',result)
        logger.info('总进度[####################] %d/%d | 离线分析结束：%s；结果：%s',len(inputs),len(inputs),status,output)
        return output,result
    finally:
        for handler in (file_handler,console): logger.removeHandler(handler); handler.close()


def main():
    try:
        output,result = run_analysis(RECORDINGS,PARAMETERS,OUTPUT_ROOT or ROOT/'data/recording_analysis',cfg_path=CFG_PATH)
        print('结果目录：',output)
        return 1 if result['status']=='partial' else 0
    except Exception as exc:
        print('分析未完成：',exc,file=sys.stderr); return 1


if __name__=='__main__':
    raise SystemExit(main())
