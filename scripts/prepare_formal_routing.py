"""Edit the sites below; compute and check a CFG on the server without touching the chip."""
from __future__ import annotations

from datetime import datetime
import importlib
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Edit this section. Choose explicit physical sites from your current recording CFG.
CFG_PATH = ""
STIMULATION_ELECTRODES = []
MAX_ATTEMPTS = 4
OUTPUT_ROOT = ""  # Empty: ROOT/data. Always creates a new timestamped directory.
API_PARENT = None  # Optional directory containing the official maxlab package.
# End editable section. No H5, culture label, old Probe run or YAML is required.


def main():
    from python.prepare_probe_routing import prepare_stimulation_candidate, validate_stimulation_request

    def path(value):
        result = Path(value).expanduser()
        return (result if result.is_absolute() else ROOT / result).resolve()

    # Validate all local inputs before importing the SDK or creating any server Array.
    source, _, sites = validate_stimulation_request(path(CFG_PATH), STIMULATION_ELECTRODES, MAX_ATTEMPTS)
    destination = path(OUTPUT_ROOT) if OUTPUT_ROOT else ROOT / "data"
    output = destination / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_formal_routing')
    output.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print("仅SDK服务器软件路由；不初始化芯片、不下载、不录制、不刺激", flush=True)
    print(f"来源CFG：{source}\n指定电极：{sites}\n结果目录：{output}", flush=True)
    if API_PARENT:
        sys.path.insert(0, str(path(API_PARENT)))
    try:
        mx = importlib.import_module('maxlab')
    except ImportError as exc:
        raise RuntimeError("请激活安装官方SDK的maxlab环境；此工具只计算软件路由") from exc
    output.mkdir(exist_ok=False)
    handler = logging.FileHandler(output / 'routing.log', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().addHandler(handler)
    # Core function requires a fresh directory; reserve only a parent log directory here.
    results = output / 'candidate'
    def emit(message, **kwargs):
        logging.info('%s', message)
    try:
        report = prepare_stimulation_candidate(source, sites, results, mx,
                                                max_attempts=MAX_ATTEMPTS, emit=emit)
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    print(f"状态：{report['status']}\n报告：{results / 'routing_preparation.json'}", flush=True)
    if report['candidate_cfg']:
        print(f"候选CFG：{report['candidate_cfg']}", flush=True)
        print(f"记录映射完全一致：{report['baseline_mapping_reusable']}；需要新CFG下的Probe核验。", flush=True)
    print("不会修改run_probe.py、run_formal.py、来源CFG或任何旧数据，也不会自动进入实验。", flush=True)
    return 0 if report['status'] in ('candidate_for_hardware_routing_check', 'original_cfg_already_independent') else 2


if __name__ == '__main__':
    raise SystemExit(main())
