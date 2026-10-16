"""Read-only comparison: post 10–50 ms > 1.5 times paired pre -50–-10 ms."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import logging
from pathlib import Path
import sys

import numpy as np
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from analysis.utils.io import read_session
from analysis.utils.probe import evaluate_probe, count_windows
from python.utils.persistence import atomic_json, write_csv, sha256, utc_now
from tools.inspect_probe_results import read_trials
from tools.probe_raster_review import align_saved_events


def review_run(run):
    run=Path(run).resolve()
    sources=[run/'pre_experiment/probe_summary.csv',run/'config_snapshot/system.yaml',run/'config_snapshot/stimulation.yaml']
    hashes={str(p):sha256(p) for p in sources}
    with sources[0].open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    system=yaml.safe_load(sources[1].read_text(encoding='utf-8'))
    stimulation=yaml.safe_load(sources[2].read_text(encoding='utf-8'))
    cfg=dict(stimulation['probe'],threshold_rule='paired_pre_stim_ratio',
             response_window_ms=[10.,50.],paired_baseline_window_ms=[-50.,-10.],
             response_to_baseline_ratio=1.5,enforce_artifact_qc=False,success_fraction_threshold=.60)
    data_root=(ROOT/'data').resolve()
    if not data_root.is_relative_to(ROOT.resolve()):
        raise ValueError('data目录指向项目外，停止写入')
    output=data_root/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_paired_ratio_review')
    output.mkdir(parents=True,exist_ok=False)
    logger=logging.getLogger('paired_ratio_review')
    logger.setLevel(logging.INFO)
    handlers=[logging.StreamHandler(),logging.FileHandler(output/'analysis.log',encoding='utf-8')]
    for handler in handlers:
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
    summaries, metrics=[],[]
    started=utc_now()
    try:
        logger.info('只读旧Probe H5，不连接设备、不刺激；10–50ms > 1.5 × 同次−50至−10ms')
        for index,row in enumerate(rows,1):
            summary=dict(unit_id=row['unit_id'],electrode=int(row['electrode']),
                         source_status=row.get('status'),original_success_trials=int(row['success_trials']),
                         success_trials=0,all_trials=0,eligible=False,status='not_evaluated')
            if row.get('status')=='complete' and row.get('h5'):
                h5=Path(row['h5'])
                stamp=(h5.stat().st_size,h5.stat().st_mtime_ns)
                saved=read_trials(row)
                trial_path=Path(row.get('trial_metrics_path') or h5.parent/'trial_metrics.csv')
                hashes[str(trial_path)]=sha256(trial_path)
                session=read_session(h5,well=system['maxwell']['well'])
                onsets,_=align_saved_events(session,saved)
                if len(saved)!=int(row['all_trials']) or not np.array_equal(
                        count_windows(session.times,onsets+.010,.040),[int(t['response_10_50ms']) for t in saved]):
                    raise ValueError('原trial数量或响应计数与H5不一致')
                try:
                    trials,result=evaluate_probe(session,onsets,None,cfg)
                except ValueError as exc:
                    if '刺激前背景窗口超出' not in str(exc):
                        raise
                    summary.update(status='incomplete_pre_window',error=str(exc))
                else:
                    complete=len(trials)==int(cfg['trials_per_electrode'])
                    summary.update(result,status='reviewed',eligible=bool(result['eligible'] and complete))
                    for trial in trials:
                        metrics.append(dict(unit_id=row['unit_id'],electrode=int(row['electrode']),**trial))
                if stamp!=(h5.stat().st_size,h5.stat().st_mtime_ns):
                    raise ValueError('读取期间H5变化')
            summaries.append(summary)
            filled=int(index/len(rows)*20)
            logger.info('总进度[%s%s] %d/%d | unit=%s 电极=%s 子任务完成；有效=%s/%s',
                        '#'*filled,'-'*(20-filled),index,len(rows),row['unit_id'],row['electrode'],summary['success_trials'],summary['all_trials'])
        if any(sha256(path)!=digest for path,digest in hashes.items()):
            raise ValueError('输入配置或表格发生变化')
        config=dict(source_run=str(run),probe=cfg,aggregation='all_recorded_electrodes',
                    baseline='paired_pre_stimulus_no_burst_exclusion',source_sha256=hashes,
                    hardware_contacted=False,stimulation_sent=False)
        (output/'config.yaml').write_text(yaml.safe_dump(config,allow_unicode=True,sort_keys=False),encoding='utf-8')
        write_csv(output/'paired_trial_metrics.csv',metrics)
        write_csv(output/'paired_probe_summary.csv',summaries)
        units=[]
        for unit in dict.fromkeys(r['unit_id'] for r in summaries):
            valid=[r['electrode'] for r in summaries if r['unit_id']==unit and r['eligible']]
            units.append(dict(unit_id=unit,eligible_electrodes=';'.join(map(str,valid)),
                              eligible_count=len(valid),unit_eligible=len(valid)>=2))
        write_csv(output/'paired_unit_summary.csv',units)
        atomic_json(output/'analysis_result.json',dict(status='OFFLINE_PAIRED_RATIO_REVIEW',
                    started_utc=started,finished_utc=utc_now(),source_files_unchanged=True,
                    hardware_contacted=False,stimulation_sent=False,units_with_two_eligible_sites=sum(u['unit_eligible'] for u in units)))
        for row in summaries:
            print(f"电极{row['electrode']} 原={row['original_success_trials']}/10 新={row['success_trials']}/{row['all_trials']} 有效={row['eligible']} 状态={row['status']}")
        print(f'结果目录: {output}')
        return output
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='旧Probe离线复核：后40ms > 1.5×前40ms；不重新刺激')
    parser.add_argument('--run',required=True,type=Path)
    review_run(parser.parse_args().run)
