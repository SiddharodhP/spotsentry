"""Run every experiment, then build the tables and plots. Takes several hours.

    python experiments/run_all.py                run all, in order, quickest first
    python experiments/run_all.py --only e1 e3   run just these
    python experiments/run_all.py --skip-done    skip experiments that already have a CSV

Close other programs first: the largest states need several GB of memory.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORDER = ["e1", "e6", "e5", "e4", "e3", "e2"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="+", default=ORDER, choices=ORDER)
    parser.add_argument("--skip-done", action="store_true")
    args = parser.parse_args()
    failures = []
    for name in [e for e in ORDER if e in args.only]:
        if args.skip_done and (ROOT / "results" / f"{name}.csv").exists():
            print(f"{name}: already done, skipping", flush=True)
            continue
        started = time.monotonic()
        print(f"{name}: starting", flush=True)
        config = ROOT / "configs" / f"{name}.yaml"
        code = subprocess.call([sys.executable, str(ROOT / "run.py"), "--config", str(config)])
        print(f"{name}: exit {code} after {(time.monotonic() - started) / 60:.1f} min", flush=True)
        if code != 0:
            failures.append(name)
    for script in ("plots.py", "analyze.py"):
        subprocess.call([sys.executable, str(ROOT / script)])
    print(f"Failed: {failures}" if failures else "All experiments finished.", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
