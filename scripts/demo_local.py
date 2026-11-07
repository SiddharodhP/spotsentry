"""The acceptance demo: two simulated interruptions, then the job completes.

    python scripts/demo_local.py                     # Docker if it is running, else processes
    python scripts/demo_local.py --backend docker
    python scripts/demo_local.py --scenario local/scenarios/tight_deadline.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from local.orchestrator import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
