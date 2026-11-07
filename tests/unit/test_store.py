from __future__ import annotations

import threading
import time

import boto3
import pytest
from moto import mock_aws

from spotsentry.errors import ObjectNotFound, PreconditionFailed, UploadAborted
from spotsentry.store import LocalStore
from spotsentry.store.s3 import S3Store
from spotsentry.store.throttle import ThrottledReader, TokenBucket, mbps_to_bytes_per_sec


@pytest.fixture
def s3_store(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield S3Store("test-bucket", client=client)


@pytest.fixture(params=["local", "s3"])
def any_store(request, tmp_path, s3_store):
    return LocalStore(tmp_path / "s") if request.param == "local" else s3_store


def test_put_get_delete(any_store):
    any_store.put_bytes("a/b.txt", b"hello")
    data, etag = any_store.get("a/b.txt")
    assert data == b"hello"
    assert etag
    any_store.delete("a/b.txt")
    with pytest.raises(ObjectNotFound):
        any_store.get("a/b.txt")


def test_delete_missing_is_fine(any_store):
    any_store.delete("nothing/here")


def test_list_keys_by_prefix(any_store):
    any_store.put_bytes("x/1", b"1")
    any_store.put_bytes("x/2", b"2")
    any_store.put_bytes("y/3", b"3")
    assert sorted(any_store.list_keys("x/")) == ["x/1", "x/2"]


def test_upload_large_object(any_store):
    data = b"z" * (9 * 1024 * 1024)  # above the multipart threshold
    any_store.upload("big/object", data)
    assert any_store.get_bytes("big/object") == data


def test_aborted_upload_leaves_nothing(any_store):
    calls = {"n": 0}

    def abort():
        calls["n"] += 1
        if calls["n"] > 2:
            raise UploadAborted("stop")

    with pytest.raises(UploadAborted):
        any_store.upload("gone/object", b"z" * (9 * 1024 * 1024), abort_check=abort)
    assert not any_store.exists("gone/object")


def test_if_none_match(any_store):
    any_store.put_bytes("c/lease", b"1", if_none_match=True)
    with pytest.raises(PreconditionFailed):
        any_store.put_bytes("c/lease", b"2", if_none_match=True)
    assert any_store.get_bytes("c/lease") == b"1"


def test_if_match(any_store):
    etag = any_store.put_bytes("c/lease", b"1")
    new_etag = any_store.put_bytes("c/lease", b"2", if_match=etag)
    with pytest.raises(PreconditionFailed):
        any_store.put_bytes("c/lease", b"3", if_match=etag)
    any_store.put_bytes("c/lease", b"4", if_match=new_etag)
    assert any_store.get_bytes("c/lease") == b"4"


def test_local_conditional_writes_only_one_wins(tmp_path):
    store = LocalStore(tmp_path / "s")
    wins = []

    def attempt(i):
        try:
            store.put_bytes("race", str(i).encode(), if_none_match=True)
            wins.append(i)
        except PreconditionFailed:
            pass

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 1


def test_mbps_conversion():
    assert mbps_to_bytes_per_sec(8) == 1_000_000


@pytest.mark.parametrize("mbps", [40, 160])
def test_throttle_accuracy_within_10_percent(mbps):
    rate = mbps_to_bytes_per_sec(mbps)
    size = int(rate * 1.0)  # about one second of data
    reader = ThrottledReader(b"x" * size, TokenBucket(rate))
    started = time.monotonic()
    while reader.read(64 * 1024):
        pass
    elapsed = time.monotonic() - started
    assert elapsed == pytest.approx(1.0, rel=0.10)


def test_throttle_applies_to_local_upload(tmp_path):
    store = LocalStore(tmp_path / "s", bandwidth_limit_mbps=40)
    started = time.monotonic()
    store.upload("k", b"x" * 2_500_000)
    assert time.monotonic() - started == pytest.approx(0.5, rel=0.25)


def test_token_bucket_shared_by_threads():
    bucket = TokenBucket(1_000_000)
    started = time.monotonic()
    threads = [threading.Thread(target=bucket.consume, args=(250_000,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert time.monotonic() - started == pytest.approx(1.0, rel=0.2)
