"""Prepare a separate CFG with the original nearest-neighbour sites, without stimulation."""
from datetime import datetime
import importlib
from pathlib import Path
import sys
import argparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import ROOT, load_configuration
from python.prepare_probe_routing import prepare_candidate


def main():
    parser = argparse.ArgumentParser(description="另存交替Probe候选CFG；仅软件路由，不初始化/下载/刺激")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    args = parser.parse_args()
    system, _ = load_configuration(args.config_dir)
    parent = system["maxwell"].get("api_parent")
    if parent:
        sys.path.insert(0, parent)
    mx = importlib.import_module("maxlab")
    output = Path(system["data"]["root"]) / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_routing")
    prepare_candidate(system, output, mx)


if __name__ == "__main__":
    main()
