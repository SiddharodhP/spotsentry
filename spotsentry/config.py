"""Job configuration: YAML validated with pydantic, overridable from the environment.

An environment variable such as ``SPOTSENTRY_STORE__ENDPOINT_URL=http://localhost:9000``
overrides ``store.endpoint_url``. Values are parsed as YAML, so numbers and ``null`` work.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spotsentry.errors import ConfigError

ENV_PREFIX = "SPOTSENTRY_"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WorkloadConfig(_Section):
    class_path: str = Field(alias="class")
    params: dict[str, Any] = Field(default_factory=dict)


class StoreConfig(_Section):
    type: Literal["s3", "local"] = "s3"
    bucket: str = "spotsentry-checkpoints"
    prefix: str = "jobs/"
    endpoint_url: str | None = None
    region: str | None = "ap-south-1"
    bandwidth_limit_mbps: float | None = None
    root: str = ".spotsentry-store"


class MetadataConfig(_Section):
    enabled: bool = True
    base_url: str = "http://169.254.169.254"
    imdsv2: bool = True
    poll_interval_sec: float = 2.0
    timeout_sec: float = 1.0


class CheckpointConfig(_Section):
    strategy: Literal["none", "periodic", "reactive", "hybrid"] = "hybrid"
    interval: Literal["auto"] | float = "auto"
    min_interval_sec: float = 30.0
    max_interval_sec: float = 1800.0
    mtbi_seconds_override: float | None = None
    mtbi_seconds_prior: float | None = None
    async_: bool = Field(default=True, alias="async")
    keep_last: int = Field(default=3, ge=1)
    tiers: list[Literal["full", "light", "tiny"]] = ["full", "light", "tiny"]
    compress: bool = False
    rebalance_min_gap_sec: float = 30.0


class DeadlineConfig(_Section):
    mode: Literal["aware", "naive"] = "aware"
    safety_margin_sec: float = 15.0
    speed_safety_factor: float = 1.3
    override_seconds: float | None = None
    hard_kill: bool = False
    ewma_alpha: float = 0.3


class LeaseConfig(_Section):
    enabled: bool = True
    ttl_sec: float = 60.0
    heartbeat_sec: float = 15.0
    poll_sec: float = 5.0


class AwsConfig(_Section):
    scale_to_zero_on_complete: bool = True
    asg_name: str | None = None


class LoggingConfig(_Section):
    progress_every_steps: int = Field(default=50, ge=1)
    flush_interval_sec: float = 5.0
    stdout: bool = True


class SpotSentryConfig(_Section):
    job_id: str
    workload: WorkloadConfig | None = None
    store: StoreConfig = Field(default_factory=StoreConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    checkpoint: CheckpointConfig = Field(default_factory=CheckpointConfig)
    deadline: DeadlineConfig = Field(default_factory=DeadlineConfig)
    lease: LeaseConfig = Field(default_factory=LeaseConfig)
    aws: AwsConfig = Field(default_factory=AwsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE lines from a .env file without overriding the real environment."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    fields = set(SpotSentryConfig.model_fields)
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [part.lower() for part in key[len(ENV_PREFIX) :].split("__")]
        if path[0] not in fields:
            continue
        node = data
        for part in path[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[path[-1]] = yaml.safe_load(raw)
    return data


def parse_config(data: dict[str, Any], environ: dict[str, str] | None = None) -> SpotSentryConfig:
    """Validate a config dict after applying environment overrides."""
    env = dict(os.environ) if environ is None else environ
    try:
        return SpotSentryConfig.model_validate(_apply_env_overrides(data, env))
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> SpotSentryConfig:
    """Load and validate a YAML job file."""
    load_dotenv()
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return parse_config(data, environ)
