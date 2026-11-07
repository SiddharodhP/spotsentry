"""Interruption risk: AWS Spot Advisor ranges and optional placement scores.

The rate model is a heuristic. The advisor's upper bound is treated as the chance of an
interruption within a 730-hour month. Real rates vary with capacity at the time.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

ADVISOR_URL = "https://spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json"
CACHE_FILE = Path.home() / ".spotsentry" / "spot-advisor-data.json"
SNAPSHOT_FILE = Path(__file__).resolve().parent / "data" / "spot-advisor-snapshot.json"
CACHE_MAX_AGE_SEC = 24 * 3600
HOURS_PER_MONTH = 730.0

# Upper bound per advisor range index. The file's own "max" values are 5, 11, 16, 22 and 100, but
# its labels say <5, 5-10, 10-15, 15-20, >20. We use the labels. ">20%" has no upper bound; 100%
# would make the rate infinite, so we assume 30% and document that assumption.
RANGE_UPPER_BOUND = {0: 0.05, 1: 0.10, 2: 0.15, 3: 0.20, 4: 0.30}
RANGE_LABEL = {0: "<5%", 1: "5-10%", 2: "10-15%", 3: "15-20%", 4: ">20%"}


@dataclass
class AdvisorEntry:
    """One instance type's advisor record."""

    range_index: int
    savings_percent: int

    @property
    def label(self) -> str:
        return RANGE_LABEL[self.range_index]

    @property
    def mtbi_hours(self) -> float:
        return mtbi_hours_from_range(self.range_index)


def mtbi_hours_from_range(range_index: int) -> float:
    """M = 1 / lambda, with lambda = -ln(1 - p) / 730 per hour."""
    p = RANGE_UPPER_BOUND[range_index]
    return HOURS_PER_MONTH / -math.log(1.0 - p)


class AdvisorData:
    """The Spot Advisor file, from the network, a 24-hour cache, or the shipped snapshot."""

    def __init__(self, data: dict[str, Any], source: str) -> None:
        self.data = data
        self.source = source

    @classmethod
    def load(cls, offline: bool = False, timeout: float = 15.0) -> AdvisorData:
        if CACHE_FILE.is_file() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_MAX_AGE_SEC:
            return cls(json.loads(CACHE_FILE.read_text(encoding="utf-8")), "cache")
        if not offline:
            try:
                response = requests.get(ADVISOR_URL, timeout=timeout)
                response.raise_for_status()
                data = response.json()
                CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
                return cls(data, "live")
            except (requests.RequestException, ValueError, OSError):
                pass
        return cls(json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8")), "snapshot")

    def entry(self, region: str, instance_type: str) -> AdvisorEntry | None:
        record = (
            self.data.get("spot_advisor", {}).get(region, {}).get("Linux", {}).get(instance_type)
        )
        if record is None:
            return None
        return AdvisorEntry(range_index=int(record["r"]), savings_percent=int(record["s"]))


def placement_scores(region: str, instance_type: str, session=None) -> dict[str, int] | None:
    """Spot placement score per zone name, or None if unavailable.

    This measures how likely capacity is available now, so it is only a secondary signal. AWS
    reports zone ids, which are translated to names to match the price data.
    """
    try:
        import boto3

        ec2 = (session or boto3).client("ec2", region_name=region)
        response = ec2.get_spot_placement_scores(
            InstanceTypes=[instance_type],
            TargetCapacity=1,
            SingleAvailabilityZone=True,
            RegionNames=[region],
        )
        zones = ec2.describe_availability_zones()["AvailabilityZones"]
        names = {z["ZoneId"]: z["ZoneName"] for z in zones}
        scores = {}
        for item in response.get("SpotPlacementScores", []):
            zone_id = item.get("AvailabilityZoneId")
            if zone_id in names:
                scores[names[zone_id]] = int(item["Score"])
        return scores or None
    except Exception:  # noqa: BLE001
        return None
