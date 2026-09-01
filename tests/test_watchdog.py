"""SystemdNotifier tests: real datagrams to a scratch socket, safe no-ops."""

import socket

from airmonitor.watchdog import SystemdNotifier


def test_disabled_without_notify_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notifier = SystemdNotifier()
    assert notifier.enabled is False
    notifier.ready()  # all no-ops, nothing raises
    notifier.heartbeat()
    notifier.stopping()
    notifier.close()


def test_sends_expected_datagrams(tmp_path):
    address = str(tmp_path / "notify.sock")
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(address)
    receiver.settimeout(2)
    try:
        notifier = SystemdNotifier(address=address)
        assert notifier.enabled is True
        notifier.ready()
        notifier.heartbeat()
        notifier.stopping()
        received = [receiver.recv(64) for _ in range(3)]
        assert received == [b"READY=1", b"WATCHDOG=1", b"STOPPING=1"]
        notifier.close()
        assert notifier.enabled is False
    finally:
        receiver.close()


def test_send_failure_is_swallowed(tmp_path):
    notifier = SystemdNotifier(address=str(tmp_path / "nobody-listens.sock"))
    notifier.heartbeat()  # ECONNREFUSED/ENOENT must not propagate
    notifier.close()


def test_abstract_socket_address_is_translated():
    from airmonitor.watchdog import SystemdNotifier

    notifier = SystemdNotifier(address="@test-notify")
    try:
        assert notifier.enabled
        assert notifier._address == "\0test-notify"
        notifier.heartbeat()  # no receiver: must not raise
    finally:
        notifier.close()


def test_socket_creation_failure_disables_quietly(monkeypatch):
    import airmonitor.watchdog as watchdog_module
    from airmonitor.watchdog import SystemdNotifier

    def boom(*_a, **_k):
        raise OSError("no unix sockets here")

    monkeypatch.setattr(watchdog_module.socket, "socket", boom)
    notifier = SystemdNotifier(address="/run/systemd/notify")
    assert notifier.enabled is False
    notifier.ready()  # every call is a no-op, never a crash
