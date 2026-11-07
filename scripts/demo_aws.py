"""Follow a job on real AWS, start a fresh run, or trigger a real spot interruption.

    python scripts/demo_aws.py                      # show state and tail events
    python scripts/demo_aws.py --start              # clear the finished job, launch one instance
    python scripts/demo_aws.py --interrupt          # interrupt the instance using AWS FIS
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spotsentry.config import load_config  # noqa: E402
from spotsentry.events import read_events  # noqa: E402
from spotsentry.live import describe  # noqa: E402
from spotsentry.store import make_store  # noqa: E402

STATE_FILE = ROOT / "infra" / ".deploy-state.json"
CONSOLE_STEPS = """
To interrupt from the console (what the live demo shows):
  1. EC2 console > Spot Requests
  2. Select the request for the running instance
  3. Actions > Initiate interruption
This uses AWS Fault Injection Service, which needs an account on the Paid plan.
"""


def running_instances(ec2) -> list[dict]:
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:spotsentry", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ]
    )
    return [i for r in response["Reservations"] for i in r["Instances"]]


KEEP_ON_START = ("plan.json",)


def clear_job(s3, bucket: str, root: str) -> int:
    """Delete the job's status, lease, events and checkpoints, so the next worker starts at epoch 0.

    A completed job's workers exit at once, so a new demo run needs this. plan.json is kept, and
    nothing outside the job's folder (such as the dataset copy) is touched.
    """
    deleted = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=root):
        keys = [
            {"Key": o["Key"]}
            for o in page.get("Contents", [])
            if o["Key"][len(root):] not in KEEP_ON_START
        ]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            deleted += len(keys)
    return deleted


def start_fresh(session, bucket: str, root: str, asg_name: str, running: list[dict]) -> bool:
    if running:
        print("An instance is already running this job. Wait for it to finish, or tear down first.")
        return False
    count = clear_job(session.client("s3"), bucket, root)
    print(f"Cleared {count} objects under s3://{bucket}/{root} (kept {', '.join(KEEP_ON_START)}).")
    session.client("autoscaling").set_desired_capacity(
        AutoScalingGroupName=asg_name, DesiredCapacity=1, HonorCooldown=False
    )
    print(f"Asked {asg_name} for one instance. Training starts about 2 minutes after launch.")
    print("It costs money until the job finishes or you run: python infra/teardown.py")
    return True


def interrupt_with_fis(session, role_arn: str) -> None:
    """Create an FIS experiment template and start it: a real two-minute spot notice."""
    fis = session.client("fis")
    template = fis.create_experiment_template(
        clientToken=uuid.uuid4().hex,
        description="SpotSentry: send a spot interruption to the tagged instance",
        roleArn=role_arn,
        stopConditions=[{"source": "none"}],
        targets={
            "SpotInstances": {
                "resourceType": "aws:ec2:spot-instance",
                "resourceTags": {"spotsentry": "true"},
                "filters": [{"path": "State.Name", "values": ["running"]}],
                "selectionMode": "ALL",
            }
        },
        actions={
            "interrupt": {
                "actionId": "aws:ec2:send-spot-instance-interruptions",
                "parameters": {"durationBeforeInterruption": "PT2M"},
                "targets": {"SpotInstances": "SpotInstances"},
            }
        },
        tags={"spotsentry": "true"},
    )["experimentTemplate"]
    experiment = fis.start_experiment(
        clientToken=uuid.uuid4().hex, experimentTemplateId=template["id"]
    )["experiment"]
    print(f"Started FIS experiment {experiment['id']} (template {template['id']}).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job", default=str(ROOT / "jobs" / "cifar_aws.yaml"))
    parser.add_argument("--start", action="store_true",
                        help="clear the job's previous run from the bucket, launch one instance")
    parser.add_argument("--interrupt", action="store_true")
    parser.add_argument("--no-follow", action="store_true", help="print once and exit")
    args = parser.parse_args()

    import boto3

    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    config = load_config(args.job)
    if state.get("BucketName"):
        config.store.bucket = state["BucketName"]
    region = state.get("region") or config.store.region
    session = boto3.Session(region_name=region)
    ec2 = session.client("ec2")
    instances = running_instances(ec2)
    if instances:
        for instance in instances:
            print(
                f"Instance: {instance['InstanceId']} {instance['InstanceType']} in "
                f"{instance['Placement']['AvailabilityZone']}, {instance['State']['Name']}"
            )
    else:
        print("No running instance tagged spotsentry=true.")

    store = make_store(config.store)
    root = f"{config.store.prefix}{config.job_id}/"
    if args.start:
        if not state.get("AsgName"):
            print("infra/.deploy-state.json has no AsgName. Was this deployed with deploy.py?")
            return 1
        if not start_fresh(session, config.store.bucket, root, state["AsgName"], instances):
            return 1

    if args.interrupt:
        if not state.get("FisRoleArn"):
            print("infra/.deploy-state.json has no FisRoleArn. Was this deployed with deploy.py?")
            return 1
        interrupt_with_fis(session, state["FisRoleArn"])
    else:
        print(CONSOLE_STEPS)

    seen = 0
    print("Events (Ctrl+C to stop):")
    try:
        while True:
            events = read_events(store, root)
            for event in events[seen:]:
                if event.get("type") != "job_progress":
                    print(
                        f"{event['ts'][11:19]}  {event.get('instance_id', '')[:19]:<20}"
                        f"{event['type']:<22}{describe(event)}"
                    )
            seen = len(events)
            if args.no_follow:
                return 0
            time.sleep(5)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
