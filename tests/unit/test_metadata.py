from __future__ import annotations

import time
from datetime import UTC, datetime

from spotsentry.metadata import MetadataClient, parse_time
from spotsentry.testing.fake_imds import FakeIMDS
from spotsentry.watcher import Watcher


def test_no_notice_returns_none(imds: FakeIMDS):
    client = MetadataClient(imds.base_url)
    assert client.get_interruption_notice() is None
    assert client.get_rebalance() is None


def test_notice_is_parsed(imds: FakeIMDS):
    imds.schedule_notice(0, lead_sec=120)
    notice = MetadataClient(imds.base_url).get_interruption_notice()
    assert notice is not None
    assert notice.action == "terminate"
    assert 110 < (notice.time - datetime.now(UTC)).total_seconds() <= 121


def test_rebalance_is_parsed(imds: FakeIMDS):
    imds.schedule_rebalance(0)
    assert MetadataClient(imds.base_url).get_rebalance() is not None


def test_parse_time_utc():
    parsed = parse_time("2026-12-01T10:32:00Z")
    assert parsed == datetime(2026, 12, 1, 10, 32, tzinfo=UTC)


def test_token_is_cached(imds: FakeIMDS):
    client = MetadataClient(imds.base_url)
    client.get_interruption_notice()
    client.get_interruption_notice()
    assert imds.token_requests == 1


def test_401_refreshes_token(imds: FakeIMDS):
    client = MetadataClient(imds.base_url)
    client.get_interruption_notice()
    imds.expire_tokens()
    imds.schedule_notice(0)
    assert client.get_interruption_notice() is not None
    assert imds.token_requests == 2


def test_imdsv1_mode_sends_no_token():
    with FakeIMDS(imdsv2=False) as server:
        client = MetadataClient(server.base_url, imdsv2=False)
        server.schedule_notice(0)
        assert client.get_interruption_notice() is not None
        assert server.token_requests == 0


def test_network_error_is_no_notice():
    client = MetadataClient("http://127.0.0.1:9", timeout=0.2)
    assert client.get_interruption_notice() is None
    assert client.get_rebalance() is None
    assert client.instance_identity().instance_id == "local"


def test_server_error_is_no_notice(imds: FakeIMDS):
    imds.fail_with = 500
    assert MetadataClient(imds.base_url).get_interruption_notice() is None


def test_identity(imds: FakeIMDS):
    identity = MetadataClient(imds.base_url).instance_identity()
    assert identity.instance_id == "i-fake"
    assert identity.instance_type == "c5.large"
    assert identity.az == "ap-south-1a"


def test_watcher_sets_flags(imds: FakeIMDS):
    watcher = Watcher(MetadataClient(imds.base_url), None, poll_interval=0.02)
    watcher.start()
    assert not watcher.notice_event.is_set()
    imds.schedule_rebalance(0)
    assert watcher.rebalance_event.wait(2)
    imds.schedule_notice(0, lead_sec=60)
    assert watcher.notice_event.wait(2)
    assert watcher.notice is not None
    watcher.stop()


def test_watcher_stops(imds: FakeIMDS):
    watcher = Watcher(MetadataClient(imds.base_url), None, poll_interval=0.02)
    watcher.start()
    time.sleep(0.05)
    watcher.stop()
    assert not watcher._thread.is_alive()
