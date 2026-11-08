from types import SimpleNamespace
from unittest.mock import Mock

from apps.slack.main import SocketHealth


def test_socket_health_waits_for_reconnection_and_resets_after_recovery(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("apps.slack.main.time.monotonic", lambda: now[0])
    monkeypatch.setattr("apps.slack.main.time.time", lambda: now[0])
    client = Mock()
    client.current_session = None
    client.is_connected.return_value = False
    health = SocketHealth(timeout=120)

    assert not health.failed(client)
    now[0] += 119
    assert not health.failed(client)
    client.current_session = SimpleNamespace(last_ping_pong_time=now[0])
    client.is_connected.return_value = True
    assert not health.failed(client)
    client.is_connected.return_value = False
    client.current_session = None
    assert not health.failed(client)
    now[0] += 120
    assert health.failed(client)


def test_socket_health_detects_missing_or_stale_pongs_and_new_session(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("apps.slack.main.time.monotonic", lambda: now[0])
    monkeypatch.setattr("apps.slack.main.time.time", lambda: now[0])
    client = Mock()
    client.is_connected.return_value = True
    client.current_session = SimpleNamespace(last_ping_pong_time=None)
    health = SocketHealth(timeout=120)

    assert not health.failed(client)
    now[0] += 120
    assert health.failed(client)
    client.current_session = SimpleNamespace(last_ping_pong_time=None)
    assert not health.failed(client)
    client.current_session.last_ping_pong_time = now[0]
    now[0] += 119
    assert not health.failed(client)
    now[0] += 1
    assert health.failed(client)
