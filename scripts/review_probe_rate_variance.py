"""Re-score saved Probe counts in Hz without loading SDK or running stimulation."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from python.probe_rate_variance import review_rate_variance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读原文件，另存发放率方差离线对照，不进行刺激")
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    review_rate_variance(args.run)
