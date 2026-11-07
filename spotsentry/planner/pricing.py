"""Spot and on-demand prices."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

ON_DEMAND_FILE = Path(__file__).resolve().parent / "data" / "on_demand_prices.yaml"


def on_demand_price(region: str, instance_type: str, path: Path = ON_DEMAND_FILE) -> float | None:
    """On-demand USD per hour from the shipped table, or None if the type is not listed."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("prices", {}).get(region, {}).get(instance_type)


def spot_prices_live(region: str, instance_type: str, session=None) -> dict[str, float] | None:
    """Latest spot price per availability zone, or None without AWS access."""
    try:
        import boto3

        ec2 = (session or boto3).client("ec2", region_name=region)
        response = ec2.describe_spot_price_history(
            InstanceTypes=[instance_type],
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(UTC) - timedelta(hours=1),
        )
    except Exception:  # noqa: BLE001
        return None
    latest: dict[str, tuple[datetime, float]] = {}
    for item in response.get("SpotPriceHistory", []):
        zone, stamp, price = item["AvailabilityZone"], item["Timestamp"], float(item["SpotPrice"])
        if zone not in latest or stamp > latest[zone][0]:
            latest[zone] = (stamp, price)
    return {zone: value[1] for zone, value in latest.items()} or None


def spot_price_estimate(on_demand: float, savings_percent: int) -> float:
    """Offline estimate from the advisor's typical savings."""
    return on_demand * (1.0 - savings_percent / 100.0)
