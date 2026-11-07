"""Deploy SpotSentry to AWS. Creates paid resources, so it needs --yes.

Without --yes it only prints what would be created and what it would cost:

    python infra/deploy.py --repo-url https://github.com/you/spotsentry.git
    python infra/deploy.py --repo-url https://github.com/you/spotsentry.git --yes
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "infra" / "spotsentry.yaml"
BOOTSTRAP = ROOT / "infra" / "userdata.sh.tpl"
JOB_FILE = ROOT / "jobs" / "cifar_aws.yaml"
STATE_FILE = ROOT / "infra" / ".deploy-state.json"
PARAMETER_LIMIT_BYTES = 4096

sys.path.insert(0, str(ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy SpotSentry to AWS.")
    parser.add_argument("--repo-url", required=True, help="Git URL instances clone from")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--job", default="jobs/cifar_aws.yaml")
    parser.add_argument("--region", default="ap-southeast-2")
    parser.add_argument("--stack-name", default="spotsentry")
    parser.add_argument("--instance-types", default="c5.large,m5.large,c6i.large",
                        help="exactly three types")
    parser.add_argument("--subnet-ids", default=None, help="default: the default VPC's subnets")
    parser.add_argument("--key-name", default="")
    parser.add_argument("--bucket", default=None, help="default: spotsentry-<account>-<region>")
    parser.add_argument("--yes", action="store_true", help="actually create the resources")
    return parser.parse_args(argv)


def bootstrap_parameter() -> str:
    """Base64 of the bootstrap script, checked against CloudFormation's parameter limit."""
    # Git on Windows may check the script out with CRLF endings, which bash on Linux rejects.
    script = BOOTSTRAP.read_bytes().replace(b"\r\n", b"\n")
    encoded = base64.b64encode(script).decode("ascii")
    if len(encoded) > PARAMETER_LIMIT_BYTES:
        raise SystemExit(
            f"{BOOTSTRAP.name} is too large: {len(encoded)} bytes after encoding, "
            f"limit {PARAMETER_LIMIT_BYTES}."
        )
    return encoded


def validate_types(raw: str) -> list[str]:
    types = [t.strip() for t in raw.split(",") if t.strip()]
    if len(types) != 3 or len(set(types)) != 3:
        raise SystemExit(
            f"--instance-types needs exactly three different types, got {types}. "
            "The template lists three overrides because CloudFormation cannot loop over a list."
        )
    return types


def estimate_hourly(region: str, types: list[str]) -> str:
    """A rough cost line, from the shipped price table and advisor savings."""
    from spotsentry.planner.pricing import on_demand_price, spot_price_estimate
    from spotsentry.planner.risk import AdvisorData

    advisor = AdvisorData.load(offline=True)
    lines = []
    for instance_type in types:
        on_demand = on_demand_price(region, instance_type)
        entry = advisor.entry(region, instance_type)
        if on_demand is None or entry is None:
            lines.append(f"    {instance_type}: no price data")
            continue
        spot = spot_price_estimate(on_demand, entry.savings_percent)
        lines.append(
            f"    {instance_type}: about ${spot:.3f}/h spot (on-demand ${on_demand:.3f}/h)"
        )
    return "\n".join(lines)


def describe_plan(args: argparse.Namespace, types: list[str], bucket: str, subnets: str) -> str:
    return f"""
This will create, in {args.region}, stack "{args.stack_name}":
  - S3 bucket {bucket} (public access blocked, objects expire after 30 days)
  - IAM role and instance profile, plus an IAM role for AWS FIS
  - Launch template: Ubuntu 22.04, 20 GB gp3 disk, IMDSv2 required
  - Auto Scaling group: spot only, ONE instance running, capacity rebalancing on
  - Subnets: {subnets}
  - Project cloned from: {args.repo_url} (branch {args.branch})

Estimated cost while one instance runs:
{estimate_hourly(args.region, types)}
    20 GB gp3 disk: about $0.002/h. S3: pennies for a few GB.
It keeps costing money until the job finishes or you run: python infra/teardown.py
Set an AWS Budget (for example $5) first, if you have not already.
"""


def update_job_file(bucket: str, path: Path = JOB_FILE, region: str | None = None) -> None:
    """Write the bucket name, and the region if given, into the AWS job file."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^(\s*bucket:\s*).*$", rf"\g<1>{bucket}", text, count=1)
    if region:
        text = re.sub(r"(?m)^(\s*region:\s*).*$", rf"\g<1>{region}", text, count=1)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    types = validate_types(args.instance_types)
    bootstrap = bootstrap_parameter()

    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    session = boto3.Session(region_name=args.region)
    try:
        account = session.client("sts").get_caller_identity()["Account"]
    except (NoCredentialsError, ClientError) as exc:
        print(f"AWS credentials are not working: {exc}\nRun: aws configure")
        return 1

    bucket = args.bucket or f"spotsentry-{account}-{args.region}"
    ec2 = session.client("ec2")
    subnets = args.subnet_ids
    if not subnets:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            print("This region has no default VPC. Pass --subnet-ids.")
            return 1
        found = ec2.describe_subnets(
            Filters=[
                {"Name": "vpc-id", "Values": [vpcs[0]["VpcId"]]},
                {"Name": "default-for-az", "Values": ["true"]},
            ]
        )["Subnets"]
        subnets = ",".join(s["SubnetId"] for s in found)
    print(describe_plan(args, types, bucket, subnets))
    if not args.yes:
        print("Nothing was created. To go ahead, run the same command with --yes.")
        return 0

    parameters = {
        "RepoUrl": args.repo_url,
        "RepoBranch": args.branch,
        "JobConfigPath": args.job,
        "InstanceTypes": ",".join(types),
        "SubnetIds": subnets,
        "KeyName": args.key_name,
        "BucketName": bucket,
        "BootstrapBase64": bootstrap,
    }
    cfn = session.client("cloudformation")
    body = TEMPLATE.read_text(encoding="utf-8")
    cfn.validate_template(TemplateBody=body)
    print("Template is valid. Creating the stack, this takes a few minutes...")
    cfn.create_stack(
        StackName=args.stack_name,
        TemplateBody=body,
        Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()],
        Capabilities=["CAPABILITY_IAM"],
        Tags=[{"Key": "spotsentry", "Value": "true"}],
        OnFailure="DELETE",
    )
    cfn.get_waiter("stack_create_complete").wait(
        StackName=args.stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 60}
    )
    stack = cfn.describe_stacks(StackName=args.stack_name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    print("Created:")
    for key, value in outputs.items():
        print(f"  {key}: {value}")
    update_job_file(outputs["BucketName"], region=args.region)
    STATE_FILE.write_text(
        json.dumps({"stack": args.stack_name, "region": args.region, **outputs}, indent=1),
        encoding="utf-8",
    )
    print(f"Updated {JOB_FILE.name} with the bucket and region. Commit and push it, or instances")
    print("will use the old name (the SPOTSENTRY_STORE__BUCKET variable covers them meanwhile).")
    print("\nNext: python scripts/demo_aws.py")
    print("When finished: python infra/teardown.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
