"""Plot existing Probe H5 in milliseconds, without stimulation or input edits."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import logging
from pathlib import Path
import sys

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.utils.io import read_session
from analysis.utils.probe import count_windows
from python.probe_raster_review import align_saved_events, plot_site
from python.utils.persistence import atomic_json, sha256, utc_now, write_csv
from scripts.inspect_probe_results import read_trials


def plot_run(run, electrodes=None, window_ms=(-10., 50.)):
    if len(window_ms) != 2 or not np.isfinite(window_ms).all() or not window_ms[0] < 0 < window_ms[1]:
        raise ValueError('绘图范围必须为有限的负起点和正终点')
    if not np.isclose(window_ms[1] - window_ms[0], round(window_ms[1] - window_ms[0])):
        raise ValueError('绘图范围长度必须为1 ms的整数倍')
    run = Path(run).expanduser().resolve()
    summary = run / 'pre_experiment/probe_summary.csv'
    system_path = run / 'config_snapshot/system.yaml'
    stimulation_path = run / 'config_snapshot/stimulation.yaml'
    with summary.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    system = yaml.safe_load(system_path.read_text(encoding='utf-8'))
    stimulation = yaml.safe_load(stimulation_path.read_text(encoding='utf-8'))
    completed = [r for r in rows if r.get('status') == 'complete' and r.get('h5') and
                 (electrodes is None or int(r['electrode']) in electrodes)]
    if not completed:
        raise ValueError('没有可绘图的已完成Probe位点')
    if electrodes is not None and set(electrodes) != {int(r['electrode']) for r in completed}:
        raise ValueError('指定电极包含未完成或没有H5的位点')
    data_root = (ROOT / 'data').resolve()
    if not data_root.is_relative_to(ROOT.resolve()):
        raise ValueError('本项目data指向其他目录，停止写入')
    output = data_root / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_probe_raster_review')
    output.mkdir(parents=True, exist_ok=False)
    logger = logging.getLogger('plot_probe_rasters')
    logger.setLevel(logging.INFO)
    handlers = [logging.StreamHandler(), logging.FileHandler(output/'analysis.log', encoding='utf-8')]
    for handler in handlers:
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
    source_hashes = {str(p): sha256(p) for p in [summary, system_path, stimulation_path]}
    settings = dict(source_run=str(run), electrodes=electrodes, window_ms=list(window_ms),
                    artifact_zoom_ms=[-5, 10], psth_bin_ms=1., dpi=600,
                    hardware_contacted=False, raw_voltage_read=False,
                    stimulation_snapshot=stimulation, started_utc=utc_now())
    (output/'config.yaml').write_text(yaml.safe_dump(settings, allow_unicode=True, sort_keys=False), encoding='utf-8')
    sites = []
    try:
        logger.info('结果目录: %s；只读H5 spikes与events，不连接设备、不刺激', output)
        for index, row in enumerate(completed, 1):
            electrode = int(row['electrode'])
            h5 = Path(row['h5'])
            stat = h5.stat()
            logger.info('总进度 %d/%d | 当前run=%s 文件=%s 子任务=读取spikes', index - 1, len(completed), run.name, h5)
            session = read_session(h5, well=system['maxwell']['well'])
            trials = read_trials(row)
            trial_path = Path(row.get('trial_metrics_path') or h5.parent/'trial_metrics.csv')
            source_hashes[str(trial_path)] = sha256(trial_path)
            if len(trials) != int(row['all_trials']):
                raise ValueError(f'电极{electrode}trial数量与summary不一致')
            times, events = align_saved_events(session, trials)
            expected = np.asarray([int(t['response_10_50ms']) for t in trials])
            if not np.array_equal(count_windows(session.times, times+.010, .040), expected):
                raise ValueError(f'电极{electrode}的H5事件对齐计数与原metrics不一致')
            trial_ids = [int(t.get('trial', i + 1)) for i, t in enumerate(trials)]
            if len(set(trial_ids)) != len(trial_ids):
                raise ValueError('trial编号重复')
            directory = output / row['unit_id'] / f'ele_{electrode}'
            directory.mkdir(parents=True)
            total = len(trials) + 4
            done = 0
            def progress(name):
                nonlocal done
                done += 1
                a, b = int(index/len(completed)*20) if done == total else int((index-1)/len(completed)*20), int(done/total*20)
                logger.info('总进度[%s%s] %d/%d | 电极%d 子任务[%s%s] %d/%d %s',
                            '#'*a, '-'*(20-a), index if done == total else index-1, len(completed),
                            electrode, '#'*b, '-'*(20-b), done, total, name)
            psth = plot_site(session, times, trial_ids, directory, window_ms, progress)
            write_csv(directory/'psth.csv', psth)
            coverage = [dict(trial=trial_id, stim_time_sec=float(t), event_id=e['event_id'],
                             requested_window_start_ms=window_ms[0], requested_window_end_ms=window_ms[1],
                             complete_window=bool(t+window_ms[0]/1000 >= 0 and t+window_ms[1]/1000 <= session.duration_sec))
                        for trial_id, t, e in zip(trial_ids, times, events)]
            write_csv(directory/'trial_alignment.csv', coverage)
            write_csv(directory/'original_trial_metrics.csv', trials)
            item = dict(electrode=electrode, unit_id=row['unit_id'], h5=str(h5),
                        h5_size_bytes=stat.st_size, h5_mtime_ns=stat.st_mtime_ns,
                        figures_directory=str(directory), original_summary=row,
                        event_times_verified=True, saved_response_counts_verified=True,
                        incomplete_window_trials=[r['trial'] for r in coverage if not r['complete_window']],
                        formats=['png', 'pdf', 'svg'], figure_count=total)
            atomic_json(directory/'source.json', item)
            if (h5.stat().st_size, h5.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError('绘图期间源H5发生变化')
            sites.append(item)
        if any(sha256(p) != digest for p, digest in source_hashes.items()):
            raise ValueError('绘图期间输入配置或表格发生变化')
        atomic_json(output/'analysis_result.json', dict(status='OFFLINE_PROBE_RASTER_REVIEW',
                    finished_utc=utc_now(), settings=settings, source_sha256=source_hashes,
                    source_files_unchanged=True, sites=sites,
                    skipped=[dict(unit_id=r['unit_id'],electrode=r['electrode'],status=r.get('status'))
                             for r in rows if r.get('status') != 'complete']))
        logger.info('绘图完成；结果目录: %s', output)
        print(f'绘图完成：{len(sites)}个位点；结果目录: {output}')
        return output
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='从已有Probe H5重绘毫秒级栅格图；不刺激')
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--electrodes', type=int, nargs='+', help='默认所有已完成位点')
    parser.add_argument('--window-ms', type=float, nargs=2, default=[-10., 50.], metavar=('START', 'END'))
    args = parser.parse_args()
    plot_run(args.run, args.electrodes, args.window_ms)
