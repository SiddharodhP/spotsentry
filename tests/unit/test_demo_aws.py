"""scripts/demo_aws.py --start: clears only the job's previous run, and never launches twice."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("demo_aws", ROOT / "scripts" / "demo_aws.py")
demo_aws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo_aws)


@pytest.fixture
def s3(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="demo-bucket")
        yield client


def keys(s3) -> set[str]:
    return {o["Key"] for o in s3.list_objects_v2(Bucket="demo-bucket").get("Contents", [])}


def test_clear_job_keeps_plan_dataset_and_other_jobs(s3):
    for key in [
        "jobs/cifar-aws/status.json",
        "jobs/cifar-aws/lease.json",
        "jobs/cifar-aws/plan.json",
        "jobs/cifar-aws/events/i-1.jsonl",
        "jobs/cifar-aws/checkpoints/000001_full_e0_s1.ckpt",
        "jobs/cifar-aws-2/status.json",
        "datasets/cifar-10-python.tar.gz",
    ]:
        s3.put_object(Bucket="demo-bucket", Key=key, Body=b"x")
    assert demo_aws.clear_job(s3, "demo-bucket", "jobs/cifar-aws/") == 4
    assert keys(s3) == {
        "jobs/cifar-aws/plan.json",
        "jobs/cifar-aws-2/status.json",
        "datasets/cifar-10-python.tar.gz",
    }


class FakeSession:
    def __init__(self, s3):
        self.s3, self.capacity = s3, []

    def client(self, name):
        if name == "s3":
            return self.s3
        session = self

        class Autoscaling:
            def set_desired_capacity(self, **kwargs):
                session.capacity.append(kwargs)

        return Autoscaling()


def test_start_refuses_while_an_instance_runs(s3):
    s3.put_object(Bucket="demo-bucket", Key="jobs/j/status.json", Body=b"x")
    session = FakeSession(s3)
    assert not demo_aws.start_fresh(session, "demo-bucket", "jobs/j/", "asg", [{"InstanceId": "i"}])
    assert session.capacity == []
    assert "jobs/j/status.json" in keys(s3)


def test_start_clears_then_asks_for_one_instance(s3):
    s3.put_object(Bucket="demo-bucket", Key="jobs/j/status.json", Body=b"x")
    session = FakeSession(s3)
    assert demo_aws.start_fresh(session, "demo-bucket", "jobs/j/", "asg", [])
    assert keys(s3) == set()
    assert session.capacity == [
        {"AutoScalingGroupName": "asg", "DesiredCapacity": 1, "HonorCooldown": False}
    ]


def test_event_times_are_shown_in_local_time():
    from datetime import UTC, datetime

    expected = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC).astimezone().strftime("%H:%M:%S")
    assert demo_aws.local_time("2026-01-01T12:00:05.123456Z") == expected
