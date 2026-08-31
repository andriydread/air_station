"""Escalating Wi-Fi self-healing.

The operator's recurring failure: "Wi-Fi hangs, I can't reach the Pi,
only a power-cycle fixes it." When the connectivity probe fails several
times in a row this ladder acts instead of waiting for hands:

    failures 1..N-1   wait (transient blips resolve themselves)
    every Nth failure escalate one step:
      step 1, 2       bounce the interface (nmcli radio off/on, or ip link)
      step 3 onward   restart the networking service

A healthy probe resets everything. There is deliberately no self-reboot
here: a true system freeze is the hardware watchdog's job; rebooting for
Wi-Fi alone would also kill data collection, which works fine offline.

All commands run through ``sudo -n`` and must be allowed in
/etc/sudoers.d/airmonitor (systemd/airmonitor-sudoers, installed by
``make deploy-full`` / ``make install``).
"""

import logging
import shutil
import subprocess
from typing import Callable, List, Optional

LOGGER = logging.getLogger("airmonitor")

_BOUNCE_STEPS = 2  # interface bounces before escalating to a service restart


def _default_runner(command: List[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


class WifiRecovery:
    def __init__(
        self,
        interface: str,
        events,
        after_failures: int = 6,
        runner: Callable = _default_runner,
        which: Callable = shutil.which,
    ):
        self.interface = interface
        self.events = events
        self.after_failures = after_failures
        self.runner = runner
        self.which = which
        self.consecutive_failures = 0
        self.actions_taken = 0

    @property
    def enabled(self) -> bool:
        return self.after_failures > 0

    def record_probe(self, healthy: bool) -> None:
        if not self.enabled:
            return
        if healthy:
            if self.actions_taken:
                self.events.log(
                    logging.INFO, "network", "recovery_succeeded",
                    f"Wi-Fi recovered after {self.actions_taken} recovery action(s)",
                )
            self.consecutive_failures = 0
            self.actions_taken = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures % self.after_failures == 0:
            self._escalate()

    # --- Escalation ---------------------------------------------------------

    def _escalate(self) -> None:
        if self.actions_taken < _BOUNCE_STEPS:
            description = f"bounce interface {self.interface}"
            commands = self._bounce_commands()
        else:
            description = "restart networking service"
            commands = self._service_restart_commands()
        self.actions_taken += 1
        self.events.log(
            logging.WARNING, "network", "recovery_action",
            f"Wi-Fi unhealthy for {self.consecutive_failures} probes; "
            f"action {self.actions_taken}: {description}",
        )
        for command in commands:
            if not self._run(command):
                return  # failure already logged; wait for the next escalation

    def _run(self, command: List[str]) -> bool:
        try:
            result = self.runner(command)
        except Exception as exc:  # noqa: BLE001 - recovery must never crash the loop
            LOGGER.exception("Recovery command failed to launch: %s", command)
            self.events.log(
                logging.ERROR, "network", "recovery_failed",
                f"Recovery command failed to launch: {' '.join(command)}: {exc}",
            )
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            self.events.log(
                logging.ERROR, "network", "recovery_failed",
                f"Recovery command exited {result.returncode}: {' '.join(command)}"
                + (f" — {detail}" if detail else ""),
            )
            return False
        return True

    # --- Command construction (NetworkManager preferred, raw ip fallback) ---

    def _sudo(self, executable: str, *args: str) -> Optional[List[str]]:
        path = self.which(executable)
        if path is None:
            return None
        return ["sudo", "-n", path, *args]

    def _bounce_commands(self) -> List[List[str]]:
        nmcli_off = self._sudo("nmcli", "radio", "wifi", "off")
        if nmcli_off is not None:
            return [nmcli_off, self._sudo("nmcli", "radio", "wifi", "on")]
        ip_down = self._sudo("ip", "link", "set", self.interface, "down")
        if ip_down is not None:
            return [ip_down, self._sudo("ip", "link", "set", self.interface, "up")]
        self.events.log(
            logging.ERROR, "network", "recovery_failed",
            "Neither nmcli nor ip found; cannot bounce the interface",
        )
        return []

    def _service_restart_commands(self) -> List[List[str]]:
        systemctl = self.which("systemctl")
        if systemctl is None:
            self.events.log(
                logging.ERROR, "network", "recovery_failed",
                "systemctl not found; cannot restart networking",
            )
            return []
        if self.which("nmcli") is not None:
            return [["sudo", "-n", systemctl, "restart", "NetworkManager"]]
        return [["sudo", "-n", systemctl, "restart", "wpa_supplicant", "dhcpcd"]]
