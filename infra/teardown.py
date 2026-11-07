"""Remove everything deploy.py created, and check nothing is left running.

    python infra/teardown.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / ".deploy-state.json"


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}


def running_instances(ec2) -> list[str]:
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:spotsentry", "Values": ["true"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping"]},
        ]
    )
    return [i["InstanceId"] for r in response["Reservations"] for i in r["Instances"]]


def empty_bucket(s3, bucket: str) -> int:
    count = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        items = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if items:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": items})
            count += len(items)
    return count


def main(argv: list[str] | None = None) -> int:
    state = load_state()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack-name", default=state.get("stack", "spotsentry"))
    parser.add_argument("--region", default=state.get("region", "ap-southeast-2"))
    parser.add_argument("--keep-data", action="store_true",
                        help="keep the bucket's contents. The stack delete then fails on the "
                             "bucket, leaving the bucket in place.")
    parser.add_argument("--yes", action="store_true", help="do not ask before emptying the bucket")
    args = parser.parse_args(argv)

    import boto3
    from botocore.exceptions import ClientError

    session = boto3.Session(region_name=args.region)
    cfn = session.client("cloudformation")
    autoscaling = session.client("autoscaling")
    ec2 = session.client("ec2")
    s3 = session.client("s3")
    checklist: dict[str, str] = {}

    try:
        stack = cfn.describe_stacks(StackName=args.stack_name)["Stacks"][0]
    except ClientError:
        stack = None
        print(f"No stack named {args.stack_name} in {args.region}.")
    outputs = {o["OutputKey"]: o["OutputValue"] for o in (stack or {}).get("Outputs", [])}
    asg = outputs.get("AsgName", f"{args.stack_name}-asg")
    bucket = outputs.get("BucketName") or state.get("BucketName")

    try:
        autoscaling.update_auto_scaling_group(
            AutoScalingGroupName=asg, MinSize=0, DesiredCapacity=0
        )
        print(f"Set {asg} desired capacity to 0.")
    except ClientError as exc:
        print(f"Could not scale {asg}: {exc}")

    deadline = time.time() + 300
    while running_instances(ec2) and time.time() < deadline:
        print("Waiting for instances to terminate...")
        time.sleep(15)

    if bucket and not args.keep_data:
        answer = "y"
        if not args.yes:
            answer = input(f"Delete ALL objects in s3://{bucket}? [y/N] ").strip().lower()
        if answer == "y":
            try:
                print(f"Deleted {empty_bucket(s3, bucket)} objects from {bucket}.")
                checklist["Bucket emptied"] = "yes"
            except ClientError as exc:
                print(f"Could not empty {bucket}: {exc}")
                checklist["Bucket emptied"] = "NO"
        else:
            checklist["Bucket emptied"] = "no (you chose to keep it)"

    if stack is not None:
        print("Deleting the stack...")
        cfn.delete_stack(StackName=args.stack_name)
        try:
            cfn.get_waiter("stack_delete_complete").wait(
                StackName=args.stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 60}
            )
            checklist["Stack deleted"] = "yes"
        except Exception as exc:  # noqa: BLE001
            checklist["Stack deleted"] = f"NO: {exc}. Check the CloudFormation console."

    leftovers = running_instances(ec2)
    checklist["Instances tagged spotsentry=true still running"] = (
        ", ".join(leftovers) if leftovers else "none"
    )
    try:
        groups = autoscaling.describe_auto_scaling_groups(AutoScalingGroupNames=[asg])
        found = groups["AutoScalingGroups"]
        checklist["Auto Scaling group"] = (
            f"desired capacity {found[0]['DesiredCapacity']}" if found else "gone"
        )
    except ClientError:
        checklist["Auto Scaling group"] = "gone"

    print("\nFinal checklist:")
    for item, value in checklist.items():
        print(f"  {item}: {value}")
    if STATE_FILE.exists() and checklist.get("Stack deleted") == "yes":
        STATE_FILE.unlink()
    print("\nConfirm in the AWS console that the EC2 instance list is empty.")
    return 1 if leftovers else 0


if __name__ == "__main__":
    sys.exit(main())
