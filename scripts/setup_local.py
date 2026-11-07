"""One-time local setup: create .env, start MinIO, create the bucket.

    python scripts/setup_local.py
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
COMPOSE = ROOT / "local" / "docker-compose.yml"
ENV_FILE = ROOT / ".env"


def ensure_env() -> None:
    if ENV_FILE.exists():
        print(".env already exists, keeping it")
        return
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    ENV_FILE.write_text(
        template.replace("change-me-local-only", secrets.token_urlsafe(18)), encoding="utf-8"
    )
    print("Created .env with a random MinIO password")


def main() -> int:
    ensure_env()
    (ROOT / "experiments" / "results").mkdir(parents=True, exist_ok=True)
    if not shutil.which("docker"):
        print("Docker is not installed. Install Docker Desktop, or use --backend process.")
        return 1
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("The Docker engine is not running. Start Docker Desktop and run this again.")
        return 1
    base = ["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE)]
    subprocess.run([*base, "up", "-d", "minio"], check=True)
    from local.orchestrator import BUCKET, ensure_bucket

    ensure_bucket("http://127.0.0.1:9000", BUCKET)
    print("\nMinIO is running. API: http://localhost:9000  Console: http://localhost:9001")
    print("Bucket spotsentry-checkpoints is ready. Next: python scripts/demo_local.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
