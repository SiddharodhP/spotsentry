"""Object stores: S3 (and MinIO) for real runs, the filesystem for tests."""

from __future__ import annotations

from spotsentry.config import StoreConfig
from spotsentry.store.base import Store
from spotsentry.store.local import LocalStore


def make_store(cfg: StoreConfig) -> Store:
    """Build the store described by the config."""
    if cfg.type == "local":
        return LocalStore(cfg.root, bandwidth_limit_mbps=cfg.bandwidth_limit_mbps)
    from spotsentry.store.s3 import S3Store

    return S3Store(
        cfg.bucket,
        endpoint_url=cfg.endpoint_url,
        region=cfg.region,
        bandwidth_limit_mbps=cfg.bandwidth_limit_mbps,
    )


__all__ = ["Store", "LocalStore", "make_store"]
