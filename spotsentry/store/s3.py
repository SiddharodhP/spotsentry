"""Amazon S3 store. The same class talks to MinIO when ``endpoint_url`` is set."""

from __future__ import annotations

import os

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from spotsentry.errors import ObjectNotFound, PreconditionFailed
from spotsentry.store.base import AbortCheck, Store
from spotsentry.store.throttle import ThrottledReader, TokenBucket, mbps_to_bytes_per_sec

_NOT_FOUND = {"NoSuchKey", "404", "NotFound"}
_PRECONDITION = {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}
_UNSUPPORTED = {"NotImplemented", "501"}


class S3Store(Store):
    """Checkpoints go through boto3's managed transfer so large ones use multipart upload."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        bandwidth_limit_mbps: float | None = None,
        client=None,
    ) -> None:
        self.bucket = bucket
        if client is None:
            kwargs: dict = {}
            if endpoint_url:
                # Local MinIO: use its own keys so real AWS credentials are never involved.
                kwargs["endpoint_url"] = endpoint_url
                user = os.environ.get("MINIO_ROOT_USER")
                password = os.environ.get("MINIO_ROOT_PASSWORD")
                if user and password:
                    kwargs["aws_access_key_id"] = user
                    kwargs["aws_secret_access_key"] = password
            client = boto3.client(
                "s3",
                region_name=region,
                config=Config(
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=5,
                    read_timeout=60,
                    max_pool_connections=20,
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
                **kwargs,
            )
        self.client = client
        self._transfer = TransferConfig(
            multipart_threshold=8 * 1024 * 1024,
            multipart_chunksize=8 * 1024 * 1024,
            max_concurrency=10,
        )
        self._bucket_limit = (
            TokenBucket(mbps_to_bytes_per_sec(bandwidth_limit_mbps))
            if bandwidth_limit_mbps
            else None
        )

    def put_bytes(
        self, key: str, data: bytes, *, if_none_match: bool = False, if_match: str | None = None
    ) -> str:
        kwargs: dict = {}
        if self.supports_conditional:
            if if_none_match:
                kwargs["IfNoneMatch"] = "*"
            if if_match is not None:
                kwargs["IfMatch"] = if_match
        try:
            response = self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _PRECONDITION:
                raise PreconditionFailed(key) from exc
            if code in _UNSUPPORTED and kwargs:
                self.supports_conditional = False
                return self.put_bytes(key, data)
            raise
        return response["ETag"]

    def get(self, key: str) -> tuple[bytes, str]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in _NOT_FOUND:
                raise ObjectNotFound(key) from exc
            raise
        return response["Body"].read(), response["ETag"]

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def upload(self, key: str, data: bytes, abort_check: AbortCheck | None = None) -> None:
        reader = ThrottledReader(data, self._bucket_limit, abort_check)
        self.client.upload_fileobj(reader, self.bucket, key, Config=self._transfer)
