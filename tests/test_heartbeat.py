"""sd_notify messages and the 10 s heartbeat cadence."""

import socket

from shared.heartbeat import Heartbeat, SystemdNotifier


def test_inert_without_notify_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notifier = SystemdNotifier()
    assert notifier.enabled is False
    notifier.ready()
    notifier.heartbeat()
    notifier.stopping()
    assert notifier.sent == 0
    notifier.close()


def test_messages_reach_the_socket(tmp_path):
    path = tmp_path / "notify.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    server.settimeout(1.0)
    notifier = SystemdNotifier(address=str(path))
    assert notifier.enabled
    notifier.ready()
    notifier.heartbeat()
    notifier.stopping()
    received = [server.recv(64) for _ in range(3)]
    assert received == [b"READY=1", b"WATCHDOG=1", b"STOPPING=1"]
    assert notifier.sent == 3
    notifier.close()
    server.close()


def test_send_failure_is_swallowed(tmp_path):
    notifier = SystemdNotifier(address=str(tmp_path / "nobody-listens.sock"))
    notifier.heartbeat()  # ECONNREFUSED/ENOENT: no exception, nothing counted
    assert notifier.sent == 0
    notifier.close()


def test_heartbeat_cadence(monkeypatch):
    notifier = SystemdNotifier(address="")
    pings = []
    notifier.heartbeat = lambda: pings.append(1)
    clock = {"t": 100.0}
    beat = Heartbeat(notifier, interval=10, monotonic=lambda: clock["t"])
    assert beat.tick() is True          # first tick always pings
    clock["t"] += 4
    assert beat.tick() is False
    clock["t"] += 6
    assert beat.tick() is True
    clock["t"] += 25
    assert beat.tick() is True          # late is fine, one ping, not three
    assert len(pings) == 3
