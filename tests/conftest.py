"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from spotsentry.config import parse_config
from spotsentry.store import LocalStore
from spotsentry.testing.fake_imds import FakeIMDS


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "store")


@pytest.fixture
def imds():
    with FakeIMDS() as server:
        yield server


@pytest.fixture
def make_config(tmp_path: Path, imds: FakeIMDS):
    """Build a fast test config, pointing at a temp store and the fake metadata service."""

    def build(**overrides):
        data = {
            "job_id": "test-job",
            "store": {"type": "local", "root": str(tmp_path / "store")},
            "metadata": {"base_url": imds.base_url, "poll_interval_sec": 0.05},
            "checkpoint": {"strategy": "hybrid", "interval": 3600, "min_interval_sec": 0.01},
            "deadline": {"safety_margin_sec": 1},
            "lease": {"ttl_sec": 2, "heartbeat_sec": 0.2, "poll_sec": 0.05},
            "logging": {"stdout": False, "flush_interval_sec": 0.2},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        return parse_config(data, environ={})

    return build
