"""Verify a three-second spikes/events H5 without any voltage or routing commands."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import importlib
import logging
from pathlib import Path
import runpy
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import check_environment, load_configuration
from analysis.utils.io import BOUNDARY_IDS, parse_cfg, read_session, select_segment
from python.maxwell_setup import MaxLabBackend, require_ok
from python.utils.persistence import atomic_json, sha256
from python.utils.progress import Reporter
from tools.inspect_recording_settings import read_settings


def check_recording(system, directory, mx):
    """Use current hardware acquisition state; only create a software Array and save."""
    import h5py

    system = copy.deepcopy(system)
    system['maxwell']['raw_recording'] = False
    cfg = Path(system['electrode_map']['cfg_path'])
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    backend = MaxLabBackend(system, Reporter())
    backend.mx = mx
    backend.mapping = parse_cfg(cfg)
    backend.array = mx.Array('recording_check_' + uuid.uuid4().hex)
    try:
        require_ok(backend.array.load_config(str(cfg)), '读取软件CFG（不下载）')
        backend.start_segment(directory, 'storage_check')
        time.sleep(3.)
        path = backend.finish_segment()
    finally:
        try:
            if backend.recording or backend.file_open:
                backend.abort_segment()
        finally:
            backend.array.close()
    session = read_session(path, cfg, system['maxwell']['well'])
    if session.time_origin_source != 'h5_boundary_events' or session.duration_sec < 2.5:
        raise ValueError('短录制没有完整起止标记或有效区间不足2.5秒')
    if len(session.mapping) != len(backend.mapping):
        raise ValueError('短录制H5与现用CFG的映射数量不一致')
    if any(e['event_id'] not in BOUNDARY_IDS.values() for e in session.events):
        raise ValueError('短录制包含其他事件，请核对是否有其他程序同时运行')
    with h5py.File(path, 'r') as handle:
        segment = select_segment(handle, system['maxwell']['well'])
        if any('raw' in group for group in segment.get('groups', {}).values()):
            raise ValueError('短录制仍包含raw电压数据')
    result = dict(status='recording_check_passed', h5=str(path), cfg=str(cfg),
        cfg_sha256=sha256(cfg), culture_label=system['culture'], raw_recording=False,
        stimulus_pulses_sent=0, time_origin_source=session.time_origin_source,
        frame_origin=session.frame_origin, analysis_duration_sec=session.duration_sec,
        mapped_channels=len(session.mapping), spike_count=len(session.times),
        acquisition_settings=read_settings(path, system['maxwell']['well']),
        note='只验证当前采集状态的存储和时间标记；不初始化、不下载、不修改阈值/gain、不执行正式实验')
    atomic_json(directory/'recording_check.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description='读取run_formal.py的CONFIG_DIR；仅短录制，不发刺激')
    parser.add_argument('--enable-hardware', action='store_true', help='显式允许三秒真实录制')
    args = parser.parse_args()
    entry = runpy.run_path(str(ROOT/'run_formal.py'))
    directory = Path(entry['CONFIG_DIR']).expanduser()
    system, _ = load_configuration(directory if directory.is_absolute() else ROOT/directory)
    print(f"读取正式配置：培养物标签={system['culture']}；CFG={system['electrode_map']['cfg_path']}")
    if not args.enable_hardware:
        print('未连接设备。三秒真实录制检查：python -B tools/check_formal_recording.py --enable-hardware')
        return
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    check_environment(hardware=True, api_parent=system['maxwell'].get('api_parent'))
    mx = importlib.import_module('maxlab')
    output = Path(system['data']['root'])/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_recording_check')
    print('开始三秒录制：无DAC命令、不下载路由，不会进入正式刺激', flush=True)
    result = check_recording(system, output, mx)
    print(f"存储检查通过：{result['mapped_channels']}通道；区间{result['analysis_duration_sec']:.3f}s；"
          f"时间来源={result['time_origin_source']}；无raw电压")
    print('实际采集设置：', result['acquisition_settings'])
    print('检查报告：', output/'recording_check.json')
    print('检查H5：', result['h5'])
    print('本次未发送刺激；检查结束，不自动接续正式实验')


if __name__ == '__main__':
    main()
