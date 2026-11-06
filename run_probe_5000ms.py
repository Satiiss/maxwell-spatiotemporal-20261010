"""Independent slow single-site Probe; reuse run_probe.py inputs without editing it."""
from __future__ import annotations

import copy
from datetime import datetime
import logging
import math
from numbers import Integral
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent

# Edit only this section. Acquisition inputs come from your existing run_probe.py.
INTERVAL_MS = 5000.0  # Pulse onset to onset; 5000 ms = 0.2 Hz.
EXTRA_CENTER_ELECTRODES = []  # Append untested centers after the original roster; preserve its IDs.
ELECTRODES = []  # Explicit IDs only, e.g. the reference site and one weak site.
# Empty is an error: it never silently repeats the full candidate roster.


def configure_slow_probe(system, stimulation, centers, electrodes, *, interval_ms=5000., extra_centers=(), **kwargs):
    from run_probe import configure_probe
    if (isinstance(interval_ms, bool) or not isinstance(interval_ms, (int, float))
            or not math.isfinite(interval_ms) or interval_ms < 500):
        raise ValueError('INTERVAL_MS必须为有限数且至少500 ms')
    sites = list(electrodes)
    if (not sites or any(isinstance(e, bool) or not isinstance(e, Integral) or e < 0 for e in sites)
            or len(set(sites)) != len(sites)):
        raise ValueError('请显式填写不同的非负整数ELECTRODES；空列表不会自动测全部位点')
    original_centers, added = list(centers), list(extra_centers)
    combined = original_centers + added
    if (not combined or any(isinstance(e, bool) or not isinstance(e, Integral) or e < 0 for e in combined)
            or len(set(combined)) != len(combined)):
        raise ValueError('原中心与追加中心必须为不同的非负整数，不能重复追加或重排原候选')
    system, stimulation = configure_probe(system, stimulation, combined, **kwargs)
    system['manual_inputs']['slow_probe_extra_centers'] = [int(e) for e in added]
    system['manual_inputs']['slow_probe_electrodes'] = [int(e) for e in sites]
    stimulation['probe'].update(frequency_hz=1000./interval_ms, interval_ms=float(interval_ms),
        recording_tail_sec=max(1., interval_ms/1000. + .01))
    return system, stimulation


def select_sites(units, electrodes):
    positions = {e: u for u in units for e in u['electrodes']}
    if any(e not in positions for e in electrodes):
        raise ValueError('ELECTRODES包含当前候选清单以外的点；不会自动补邻居或替换')
    # Preserve original candidate IDs and requested first-site order.
    chosen = {}
    for e in electrodes:
        source = positions[e]
        name = source['unit_id']
        if name not in chosen:
            chosen[name] = copy.deepcopy(source)
            chosen[name]['original_unit_electrodes'] = list(source['electrodes'])
            chosen[name]['electrodes'] = []
            chosen[name]['distances_um'] = []
        chosen[name]['electrodes'].append(e)
        chosen[name]['distances_um'].append(source['distances_um'][source['electrodes'].index(e)])
    return list(chosen.values())


def preflight_slow(system, stimulation):
    from run_probe import preflight
    from analysis.utils.io import parse_cfg
    plan = preflight(system, stimulation)
    sites = system['manual_inputs']['slow_probe_electrodes']
    units = select_sites(plan['candidate_units'], sites)
    mapping = parse_cfg(system['electrode_map']['cfg_path'])
    if not set(sites).issubset({r['electrode'] for r in mapping}):
        raise ValueError('复测电极不属于当前CFG')
    interval = stimulation['probe']['interval_ms']/1000.
    plan.update(candidate_units=units, probe_trial_count=len(sites)*10,
        explicit_probe_electrodes=list(sites), probe_mode='slow_single_site',
        extra_center_electrodes=list(system['manual_inputs'].get('slow_probe_extra_centers', [])),
        interval_ms=stimulation['probe']['interval_ms'], frequency_hz=1./interval,
        automatically_starts_formal=False,
        minimum_recording_duration_sec=len(sites)*(.1+9*interval+stimulation['probe']['recording_tail_sec']))
    return plan


def run_slow(system, stimulation, *, dry_run=True, mode='hardware', parameter_source=None):
    """Preview remains read-only to hardware; every run has independent output."""
    import json
    import yaml
    from main import create_run, check_environment, validate
    from python.utils.persistence import atomic_json, sha256
    from python.utils.progress import Reporter
    from python.experiment_runner import ExperimentRunner
    from python.figures import unit_map, raster
    from analysis.utils.io import parse_cfg
    from analysis.utils.probe import evaluate_probe
    from python.utils.persistence import write_csv
    from python.maxwell_setup import ElectrodeUnroutable

    system, stimulation = copy.deepcopy(system), copy.deepcopy(stimulation)
    plan = preflight_slow(system, stimulation)
    if mode not in ('hardware', 'mock'):
        raise ValueError('mode必须为hardware或mock')
    check_environment(hardware=mode=='hardware' and not dry_run,
                      api_parent=system['maxwell'].get('api_parent'))
    validate(system, stimulation, hardware=mode=='hardware' and not dry_run, probe_only=True)
    run = create_run(system)
    handler = logging.FileHandler(run/'log.txt', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger = logging.getLogger()
    logger.addHandler(handler)
    try:
        atomic_json(run/'experiment_plan.json', plan)
        shutil.copy2(__file__, run/'slow_probe_entry_source.py')
        if parameter_source is not None:
            shutil.copy2(parameter_source, run/'probe_parameter_source.py')
        if dry_run:
            folder = run/'config_snapshot'
            folder.mkdir()
            for name, config in (('system', system), ('stimulation', stimulation)):
                (folder/f'{name}.yaml').write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
            cfg = Path(system['electrode_map']['cfg_path'])
            shutil.copy2(cfg, run/datetime.now().strftime('%Hh%Mm%Ss.cfg'))
            pre = run/'pre_experiment'
            pre.mkdir()
            write_csv(pre/'candidate_sites.csv', [dict(unit_id=u['unit_id'], center=u['center'],
                electrodes=';'.join(map(str, u['electrodes']))) for u in plan['candidate_units']])
            unit_map(parse_cfg(cfg), plan['candidate_units'], pre/'candidate_units')
            atomic_json(run/'dry_run.json', dict(hardware_contacted=False, **plan))
            return run

        class SlowRunner(ExperimentRunner):
            def initialize(self):
                cfg = self.run_dir/datetime.now().strftime('%Hh%Mm%Ss.cfg')
                if self.backend.realtime:
                    shutil.copy2(self.system['electrode_map']['cfg_path'], cfg)
                self.backend.initialize(cfg)
                self.cfg = cfg
                self.units = copy.deepcopy(plan['candidate_units'])
                actual = {r['electrode']: r for r in self.backend.mapping}
                for row in parse_cfg(self.system['electrode_map']['cfg_path']):
                    got = actual.get(row['electrode'])
                    if got is None or any(got[k] != row[k] for k in ('channel','x','y')):
                        raise ValueError('设备实际记录映射与本轮CFG不一致')
                pre = self.run_dir/'pre_experiment'
                pre.mkdir(exist_ok=True)
                write_csv(pre/'candidate_sites.csv', [dict(unit_id=u['unit_id'], center=u['center'],
                    electrodes=';'.join(map(str, u['electrodes']))) for u in self.units])
                atomic_json(pre/'explicit_probe_selection.json', plan['candidate_units'])
                unit_map(self.backend.mapping, self.units, pre/'candidate_units')
                self.snapshot()
                atomic_json(self.run_dir/'source_inputs.json', dict(cfg=str(cfg), cfg_sha256=sha256(cfg),
                    supplied_baseline=self.system['manual_inputs'].get('baseline_h5'),
                    manual_centers=self.system['manual_inputs']['candidate_centers'],
                    explicit_probe_electrodes=plan['explicit_probe_electrodes'],
                    synthetic=not self.backend.realtime, analysis_pipeline_deferred=True))

            def probes(self):
                import numpy as np
                from python.schedule import Condition
                pre = self.run_dir/'pre_experiment'
                cfg = self.stimulation['probe']
                self.global_total = plan['probe_trial_count']
                self.state('RUN_SLOW_PROBE', interval_ms=cfg['interval_ms'])
                summaries = []
                for unit in self.units:
                    for e in unit['electrodes']:
                        directory = pre/'02_probe'/unit['unit_id']/f'ele_{e}_attempt_01'
                        try:
                            pool = self.backend.route([e])
                        except ElectrodeUnroutable as exc:
                            if exc.electrode != e:
                                raise
                            directory.mkdir(parents=True)
                            atomic_json(directory/'routing.json', dict(mode='single_connect',
                                status='unroutable', error=str(exc), stimulation_sent=False))
                            summaries.append(dict(unit_id=unit['unit_id'], electrode=e, center=unit['center'],
                                status='unroutable', eligible=False, success_trials=0, all_trials=0,
                                success_fraction=0., artifact_0_3ms_mean=None, response_10_50ms_mean=None))
                            write_csv(pre/'probe_summary.csv', summaries)
                            continue
                        condition = Condition(f"{unit['unit_id']}_ele{e}", 'probe', (e,), (str(e),))
                        self.start_segment(directory, unit['unit_id']+f'_ele{e}', '02_stim', 10)
                        self.wait_probe_baseline()
                        atomic_json(directory/'routing.json', dict(mode='single_connect', pool=pool))
                        deadline = None
                        for trial in range(1, 11):
                            self.reporter.check()
                            deadline = self.one_trial(condition, trial, 1./cfg['frequency_hz'],
                                trial, 10, '慢频单点Probe', deadline)
                        # Finish the full last inter-stimulus interval before changing sites.
                        tail = cfg['recording_tail_sec']
                        self.reporter.wait(tail, '慢频Probe尾部录制', realtime=self.backend.realtime)
                        if not self.backend.realtime:
                            self.backend.advance(tail)
                        session, manifest = self.finish_segment(10)
                        times = [r['onsets_sec'][0] for r in manifest]
                        raster(session, times, directory/'raster')
                        trials, summary = evaluate_probe(session, times, self.background, cfg)
                        responses = np.asarray([r['response_10_50ms'] for r in trials], float)
                        gaps = np.diff(times)
                        summary.update(unit_id=unit['unit_id'], center=unit['center'], electrode=e,
                            status='complete', attempt=1, stimulation_unit=pool[e], routing_mode='single_connect',
                            h5=str(session.path), interval_ms=cfg['interval_ms'],
                            response_10_50ms_median=float(np.median(responses)),
                            largest_trial_response_fraction=float(responses.max()/responses.sum()) if responses.sum() else 0.,
                            actual_interval_min_sec=float(gaps.min()), actual_interval_median_sec=float(np.median(gaps)),
                            actual_interval_max_sec=float(gaps.max()), recording_tail_sec=tail)
                        write_csv(directory/'trial_metrics.csv', trials)
                        atomic_json(directory/'probe_metrics.json', summary)
                        summaries.append(summary)
                        write_csv(pre/'probe_summary.csv', summaries)
                        self.backend.disconnect_active() if self.backend.realtime else self.backend.switch(())
                        pause = cfg['inter_electrode_pause_sec']
                        self.reporter.wait(pause, '位点间等待', realtime=self.backend.realtime)
                        if not self.backend.realtime:
                            self.backend.advance(pause)
                self.finish_probe_selection(summaries)

        reporter = Reporter()
        if mode == 'hardware':
            from python.maxwell_setup import MaxLabBackend
            backend = MaxLabBackend(system, reporter)
        else:
            from python.mock_backend import MockBackend
            backend = MockBackend(system, reporter)
        runner = SlowRunner(system, stimulation, run, backend, reporter, probe_only=True)
        runner.run()
        result = json.loads((run/'experiment_result.json').read_text(encoding='utf-8'))
        result.update(probe_mode='slow_single_site', interval_ms=stimulation['probe']['interval_ms'],
                      explicit_probe_electrodes=plan['explicit_probe_electrodes'])
        atomic_json(run/'experiment_result.json', result)
        return run
    except BaseException as exc:
        atomic_json(run/'failure.json', dict(error=str(exc), error_type=type(exc).__name__))
        raise
    finally:
        logger.removeHandler(handler)
        handler.close()


def main():
    import argparse
    import run_probe as original
    from main import load_configuration
    parser = argparse.ArgumentParser(description='独立慢频单点Probe；默认仅预演')
    parser.add_argument('--enable-hardware', action='store_true', help='显式启动真实慢频刺激')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('fontTools').setLevel(logging.WARNING)
    path = Path(original.CONFIG_DIR).expanduser()
    system, stimulation = load_configuration(path if path.is_absolute() else ROOT/path)
    system, stimulation = configure_slow_probe(system, stimulation, original.CENTER_ELECTRODES, ELECTRODES,
        interval_ms=INTERVAL_MS, extra_centers=EXTRA_CENTER_ELECTRODES, minimum=original.MIN_SUCCESS_TRIALS,
        culture_id=original.CULTURE_ID, culture_div=original.CULTURE_DIV,
        baseline_h5=original.BASELINE_H5, cfg_path=original.CFG_PATH,
        event_threshold=original.EVENT_THRESHOLD if original.EVENT_THRESHOLD is not None else 5.,
        amplifier_gain=original.AMPLIFIER_GAIN if original.AMPLIFIER_GAIN is not None else 512)
    plan = preflight_slow(system, stimulation)
    print('仅预演，不连接设备' if not args.enable_hardware else '真实慢频单点Probe；完成后停止')
    print(f"培养物={system['culture']}；间隔={INTERVAL_MS:g}ms；频率={plan['frequency_hz']:g}Hz")
    for unit in plan['candidate_units']:
        print(f"{unit['unit_id']} 原中心={unit['center']} 本次仅测={unit['electrodes']}")
    print(f"总计{len(ELECTRODES)}点，每点10次，计划{plan['probe_trial_count']}次；不启动正式实验")
    run = run_slow(system, stimulation, dry_run=not args.enable_hardware, parameter_source=original.__file__)
    print(f'结果目录: {run}')


if __name__ == '__main__':
    main()
