"""Infrastructure code checks that create nothing on AWS."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

from infra import deploy

ROOT = Path(__file__).resolve().parent.parent.parent


class CfnLoader(yaml.SafeLoader):
    pass


def _any(loader, suffix, node):
    return {suffix: None}


CfnLoader.add_multi_constructor("!", _any)


def template() -> dict:
    return yaml.load((ROOT / "infra" / "spotsentry.yaml").read_text("utf-8"), Loader=CfnLoader)


def test_asg_matches_spec():
    props = template()["Resources"]["AutoScalingGroup"]["Properties"]
    assert props["MinSize"] == "0"
    assert props["MaxSize"] == "1"
    assert props["DesiredCapacity"] == "1"
    assert props["CapacityRebalance"] is True
    policy = props["MixedInstancesPolicy"]["InstancesDistribution"]
    assert policy["OnDemandPercentageAboveBaseCapacity"] == 0
    assert policy["SpotAllocationStrategy"] == "price-capacity-optimized"


def test_imdsv2_is_required():
    data = template()["Resources"]["LaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert data["MetadataOptions"]["HttpTokens"] == "required"


def test_bucket_blocks_public_access_and_expires():
    props = template()["Resources"]["CheckpointBucket"]["Properties"]
    assert all(props["PublicAccessBlockConfiguration"].values())
    assert props["LifecycleConfiguration"]["Rules"][0]["ExpirationInDays"] == 30


def test_bootstrap_fits_in_a_parameter():
    assert len(deploy.bootstrap_parameter()) <= deploy.PARAMETER_LIMIT_BYTES
    decoded = base64.b64decode(deploy.bootstrap_parameter()).decode("utf-8")
    assert "spotsentry run" in decoded


def test_bootstrap_has_unix_line_endings():
    script = base64.b64decode(deploy.bootstrap_parameter())
    assert b"\r" not in script
    assert script.startswith(b"#!")


def test_instance_types_must_be_three():
    assert deploy.validate_types("a.large, b.large,c.large") == ["a.large", "b.large", "c.large"]
    with pytest.raises(SystemExit):
        deploy.validate_types("a.large,b.large")
    with pytest.raises(SystemExit):
        deploy.validate_types("a.large,a.large,a.large")


def test_update_job_file_changes_only_the_bucket(tmp_path):
    job = tmp_path / "job.yaml"
    job.write_text("job_id: x\nstore:\n  type: s3\n  bucket: old\n  prefix: jobs/\n", "utf-8")
    deploy.update_job_file("new-bucket", job)
    data = yaml.safe_load(job.read_text("utf-8"))
    assert data["store"] == {"type": "s3", "bucket": "new-bucket", "prefix": "jobs/"}


def test_update_job_file_sets_region(tmp_path):
    job = tmp_path / "job.yaml"
    job.write_text("store:\n  type: s3\n  bucket: old\n  region: ap-south-1\n", "utf-8")
    deploy.update_job_file("b", job, region="ap-southeast-2")
    assert yaml.safe_load(job.read_text("utf-8"))["store"]["region"] == "ap-southeast-2"


def test_deploy_region_matches_job_file():
    from spotsentry.config import load_config

    config = load_config(ROOT / "jobs" / "cifar_aws.yaml", environ={})
    assert deploy.parse_args(["--repo-url", "x"]).region == config.store.region


def test_planner_data_files_are_tracked_by_git():
    # A broad "data/" ignore rule once kept these out of the repository, so EC2 never had them.
    import subprocess

    data = ROOT / "spotsentry" / "planner" / "data"
    for name in ("on_demand_prices.yaml", "relative_speed.yaml", "spot-advisor-snapshot.json"):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(data / name)], cwd=ROOT, check=False
        ).returncode == 0
        assert not ignored, f"{name} is git-ignored"


def test_plan_text_mentions_cost_and_resources():
    args = deploy.parse_args(["--repo-url", "https://example.invalid/r.git"])
    assert args.yes is False
    text = deploy.describe_plan(args, ["c5.large", "m5.large", "c6i.large"], "b", "subnet-1")
    assert "$" in text
    assert "Auto Scaling group" in text
    assert "teardown" in text


def test_aws_job_config_is_valid():
    from spotsentry.config import load_config

    config = load_config(ROOT / "jobs" / "cifar_aws.yaml", environ={})
    assert config.store.endpoint_url is None
    assert config.metadata.base_url == "http://169.254.169.254"


def test_dataset_cache_round_trip(tmp_path, monkeypatch):
    import boto3
    from moto import mock_aws

    from infra import dataset_cache

    monkeypatch.setattr(dataset_cache, "SIZE_BYTES", 5)
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-southeast-2")
        s3.create_bucket(
            Bucket="cache-test", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"}
        )
        fresh = tmp_path / "fresh" / dataset_cache.FILE
        dataset_cache.fetch(s3, "cache-test", fresh)  # nothing in the bucket yet: no file, no error
        assert not fresh.exists()
        seeded = tmp_path / "seeded" / dataset_cache.FILE
        seeded.parent.mkdir()
        seeded.write_bytes(b"12345")
        dataset_cache.seed(s3, "cache-test", seeded, wait_sec=0)
        dataset_cache.fetch(s3, "cache-test", fresh)
        assert fresh.read_bytes() == b"12345"


def test_bootstrap_fetches_dataset_before_running():
    script = base64.b64decode(deploy.bootstrap_parameter()).decode("utf-8")
    assert script.index("dataset_cache.py fetch") < script.index("spotsentry run")
