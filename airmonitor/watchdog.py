"""systemd sd_notify integration (no external dependencies).

With ``Type=notify`` + ``WatchdogSec=`` in the unit file, systemd restarts
the collector if the main loop stops sending heartbeats — a *wedged*
process gets recovered, not just a crashed one. Outside systemd (tests,
manual runs) ``NOTIFY_SOCKET`` is unset and every call is a no-op.
"""

import logging
import os
import socket
from typing import Optional

LOGGER = logging.getLogger("airmonitor")


class SystemdNotifier:
    def __init__(self, address: Optional[str] = None):
        self._address: Optional[str] = None
        self._socket: Optional[socket.socket] = None
        raw = address if address is not None else os.environ.get("NOTIFY_SOCKET", "")
        if not raw:
            return
        # A leading '@' means an abstract-namespace socket.
        self._address = "\0" + raw[1:] if raw.startswith("@") else raw
        try:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        except OSError:
            LOGGER.exception("Failed to create sd_notify socket")
            self._socket = None

    @property
    def enabled(self) -> bool:
        return self._socket is not None

    def _send(self, message: str) -> None:
        if self._socket is None or self._address is None:
            return
        try:
            self._socket.sendto(message.encode("utf-8"), self._address)
        except OSError:
            LOGGER.warning("sd_notify send failed", exc_info=True)

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
