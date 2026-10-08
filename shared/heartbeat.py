"""sd_notify messages (READY, WATCHDOG, STOPPING) for the app's systemd unit.

The units use WatchdogSec=90, so systemd restarts an app that hangs. Without
NOTIFY_SOCKET (tests, the demo) all of this is a no-op.
"""

import os
import socket
import time
from typing import Callable, Optional

HEARTBEAT_SECONDS = 10.0


class SystemdNotifier:
    def __init__(self, address: Optional[str] = None):
        self._address: Optional[str] = None
        self._socket: Optional[socket.socket] = None
        self.sent: int = 0
        raw = address if address is not None else os.environ.get("NOTIFY_SOCKET", "")
        if not raw:
            return
        # A leading '@' means an abstract-namespace socket.
        self._address = "\0" + raw[1:] if raw.startswith("@") else raw
        try:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        except OSError:
            self._socket = None

    @property
    def enabled(self) -> bool:
        return self._socket is not None

    def _send(self, message: str) -> None:
        if self._socket is None or self._address is None:
            return
        try:
            self._socket.sendto(message.encode("utf-8"), self._address)
            self.sent += 1
        except OSError:
            pass  # systemd is gone or the socket is closed; nothing to do

    def ready(self) -> None:
        self._send("READY=1")

    def heartbeat(self) -> None:
        self._send("WATCHDOG=1")

    def stopping(self) -> None:
        self._send("STOPPING=1")

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class Heartbeat:
    """Sends WATCHDOG=1 at most once per interval (monotonic time)."""

    def __init__(self, notifier: SystemdNotifier, interval: float = HEARTBEAT_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic):
        self.notifier = notifier
        self.interval = interval
        self._monotonic = monotonic
        self._last: Optional[float] = None

    def tick(self) -> bool:
        now = self._monotonic()
        if self._last is not None and now - self._last < self.interval:
            return False
        self.notifier.heartbeat()
        self._last = now
        return True
