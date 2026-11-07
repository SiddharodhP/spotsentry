"""Keep a copy of CIFAR-10 in the job's bucket, so new instances do not download it from its home.

The dataset's own server is slow from AWS (about 85 KB/s to Sydney, 30 minutes for 170 MB), and
every replacement instance needs it before training can resume. From S3 in the same region it
takes seconds. The boot script runs:

    python infra/dataset_cache.py fetch          # before the job: copy from S3 if it is there
    python infra/dataset_cache.py seed --wait &  # upload once the job has downloaded it

and from a laptop, `python infra/dataset_cache.py seed --bucket NAME` uploads a local copy.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

FILE = "cifar-10-python.tar.gz"
KEY = f"datasets/{FILE}"
SIZE_BYTES = 170498071  # the official archive; anything else is partial or wrong


def client(region: str | None):
    import boto3

    return boto3.client("s3", region_name=region)


def in_bucket(s3, bucket: str) -> bool:
    try:
        return s3.head_object(Bucket=bucket, Key=KEY)["ContentLength"] == SIZE_BYTES
    except Exception:  # noqa: BLE001 - missing, or no permission: treat both as absent
        return False


def fetch(s3, bucket: str, local: Path) -> None:
    if local.exists() and local.stat().st_size == SIZE_BYTES:
        print(f"{local} already present")
        return
    if not in_bucket(s3, bucket):
        print(f"s3://{bucket}/{KEY} not there; the job will download it from its home")
        return
    local.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    partial = local.with_suffix(".part")
    s3.download_file(bucket, KEY, str(partial))
    partial.replace(local)
    print(f"copied s3://{bucket}/{KEY} in {time.monotonic() - started:.1f} s")


def seed(s3, bucket: str, local: Path, wait_sec: float) -> None:
    deadline = time.monotonic() + wait_sec
    while not (local.exists() and local.stat().st_size == SIZE_BYTES):
        if time.monotonic() > deadline:
            print(f"{local} did not appear; nothing uploaded")
            return
        time.sleep(30)
    if in_bucket(s3, bucket):
        print(f"s3://{bucket}/{KEY} already there")
        return
    s3.upload_file(str(local), bucket, KEY)
    print(f"uploaded {local} to s3://{bucket}/{KEY}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["fetch", "seed"])
    parser.add_argument("--bucket", default=os.environ.get("SPOTSENTRY_STORE__BUCKET"))
    parser.add_argument("--region", default=os.environ.get("SPOTSENTRY_STORE__REGION"))
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--wait", action="store_true", help="seed: wait up to 2 h for the file")
    args = parser.parse_args(argv)
    if not args.bucket:
        print("No bucket: pass --bucket or set SPOTSENTRY_STORE__BUCKET")
        return 1
    s3 = client(args.region)
    local = Path(args.data_dir) / FILE
    if args.action == "fetch":
        fetch(s3, args.bucket, local)
    else:
        seed(s3, args.bucket, local, 7200 if args.wait else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
