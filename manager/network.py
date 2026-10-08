"""Wi-Fi watch: probe the router and the internet every 30 s.

The radio is bounced only when the router stops answering. Internet-only
outages are just logged, since a bounce wouldn't fix them and would cut off
the dashboard on the LAN.
"""

import socket
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

PROBE_EVERY = 30
ROUTER_TIMEOUT = 2.0
ROUTER_PORTS = (53, 80)
WAN_TARGET = ("1.1.1.1", 53)
WAN_TIMEOUT = 3.0
DOWN_AFTER = 2           # consecutive failures before we call it down
BOUNCE_AFTER = 6         # consecutive router failures before bouncing the radio
BOUNCE_COOLDOWN = 600.0
BOUNCE_PAUSE = 2.0
BOUNCE_OFF = ["sudo", "nmcli", "radio", "wifi", "off"]
BOUNCE_ON = ["sudo", "nmcli", "radio", "wifi", "on"]
# Captured right before a bounce for diagnostics (neither needs sudo).
NM_STATUS = ["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status"]
IW_LINK = ["/usr/sbin/iw", "dev", "wlan0", "link"]
SNAPSHOT_MAX = 120         # max characters per snapshot in the event
ROUTE_PATH = "/proc/net/route"


def default_gateway(path: str = ROUTE_PATH, interface: Optional[str] = None) -> Optional[str]:
    """Default gateway as a dotted IPv4 string, read from /proc/net/route."""
    try:
        lines = open(path).read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        iface, destination, gateway = parts[0], parts[1], parts[2]
        if destination != "00000000":
            continue
        if interface is not None and iface != interface:
            continue
        try:
            raw = int(gateway, 16)
        except ValueError:
            continue
        return ".".join(str((raw >> shift) & 0xFF) for shift in (0, 8, 16, 24))
    return None


def probe(host: str, port: int, timeout: float, connector: Callable = socket.create_connection,
          monotonic: Callable[[], float] = time.monotonic) -> Optional[float]:
    """Time a TCP connect in ms; None on failure."""
    started = monotonic()
    try:
        with connector((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    return round((monotonic() - started) * 1000, 1)


class WifiWatch:
    def __init__(self, log, runner: Callable = subprocess.run, connector: Callable = socket.create_connection,
                 route_path: str = ROUTE_PATH, sleeper: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.log = log
        self.runner = runner
        self.connector = connector
        self.route_path = route_path
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.gateway: Optional[str] = None
        self.router_port: Optional[int] = None
        self.router_failures = 0
        self.wan_failures = 0
        self.router_ok: Optional[bool] = None
        self.internet_ok: Optional[bool] = None
        self.last_lan_ms: Optional[float] = None
        self.last_wan_ms: Optional[float] = None
        self.last_bounce_at: Optional[int] = None
        self.bounces = 0
        self.history: List[bool] = []  # recent router results, newest last
        self.probes = 0

    def tick(self, now: float) -> Dict[str, Any]:
        self.probes += 1
        lan_ms = self._probe_router()
        wan_ms = probe(WAN_TARGET[0], WAN_TARGET[1], WAN_TIMEOUT, self.connector, self.monotonic)
        self.last_lan_ms, self.last_wan_ms = lan_ms, wan_ms
        self.history = (self.history + [lan_ms is not None])[-DOWN_AFTER:]
        self._streak("router", lan_ms is not None, "wifi_down", "wifi_up", now)
        self._streak("wan", wan_ms is not None, "internet_down", "internet_up", now)
        bounced = False
        if self.router_failures >= BOUNCE_AFTER and (
                self.last_bounce_at is None or now - self.last_bounce_at >= BOUNCE_COOLDOWN):
            bounced = self.bounce(now)
        self.log.debug("wifi", "probe", gateway=self.gateway, port=self.router_port,
                       lan_ms=lan_ms, wan_ms=wan_ms, router_failures=self.router_failures,
                       wan_failures=self.wan_failures, bounced=bounced)
        return {"lan_ms": lan_ms, "wan_ms": wan_ms, "bounced": bounced}

    def _probe_router(self) -> Optional[float]:
        gateway = default_gateway(self.route_path)
        if gateway != self.gateway:
            self.gateway, self.router_port = gateway, None
        if gateway is None:
            return None
        ports = (self.router_port,) if self.router_port else ROUTER_PORTS
        for port in ports:
            ms = probe(gateway, port, ROUTER_TIMEOUT, self.connector, self.monotonic)
            if ms is not None:
                self.router_port = port
                return ms
        return None

    def _streak(self, which: str, ok: bool, down_type: str, up_type: str, now: float) -> None:
        attr = "router_failures" if which == "router" else "wan_failures"
        state_attr = "router_ok" if which == "router" else "internet_ok"
        state = getattr(self, state_attr)
        if ok:
            setattr(self, attr, 0)
            if state is False:
                self.log.event("info", "wifi", up_type, f"{which} reachable again")
            setattr(self, state_attr, True)
            return
        failures = getattr(self, attr) + 1
        setattr(self, attr, failures)
        if failures == 1:  # log only the first failure; the rest show up in the debug line
            self.log.info("wifi", "probe_failed", which=which, gateway=self.gateway)
        if failures >= DOWN_AFTER and state is not False:
            what = "router" if which == "router" else "internet"
            self.log.event("warning", "wifi", down_type, f"{what} not answering ({failures} probes)",
                           failures=failures, gateway=self.gateway)
            setattr(self, state_attr, False)

    def snapshot(self, argv) -> str:
        """Short summary of a diagnostic command's output (wlan0 lines preferred). Never raises."""
        try:
            result = self.runner(argv, capture_output=True, text=True, timeout=10, check=False)
            lines = [line.strip() for line in str(getattr(result, "stdout", "") or "").splitlines() if line.strip()]
            wlan = [line for line in lines if line.startswith("wlan0")]
            text = " | ".join((wlan or lines)[:2]) or f"rc={getattr(result, 'returncode', '?')}"
        except Exception as exc:
            text = f"{exc.__class__.__name__}: {exc}"
        return text[:SNAPSHOT_MAX]

    def bounce(self, now: float) -> bool:
        """Turn the radio off and on again. Returns True if both commands succeeded.

        The event includes the NetworkManager and iw state from just before the
        bounce, to help work out why the link dropped.
        """
        nm_state, iw_link = self.snapshot(NM_STATUS), self.snapshot(IW_LINK)
        results = []
        for argv in (BOUNCE_OFF, BOUNCE_ON):
            try:
                result = self.runner(argv, capture_output=True, text=True, timeout=30, check=False)
                results.append(int(getattr(result, "returncode", 1)))
            except Exception as exc:
                results.append(f"{exc.__class__.__name__}: {exc}")
            if argv is BOUNCE_OFF:
                self.sleeper(BOUNCE_PAUSE)
        ok = results == [0, 0]
        self.bounces += 1
        self.last_bounce_at = int(now)
        self.router_failures = 0
        self.log.event("warning" if ok else "error", "wifi", "wifi_bounce",
                       "wi-fi radio bounced" if ok else "wi-fi radio bounce failed",
                       results=results, count=self.bounces, nm_state=nm_state, iw_link=iw_link)
        return ok

    def glyph(self) -> bool:
        return len(self.history) >= DOWN_AFTER and not any(self.history[-DOWN_AFTER:])

    def status(self) -> Dict[str, Any]:
        return {
            "router_ok": self.router_ok, "internet_ok": self.internet_ok,
            "gateway": self.gateway, "lan_ms": self.last_lan_ms, "wan_ms": self.last_wan_ms,
            "router_failures": self.router_failures, "wan_failures": self.wan_failures,
            "last_bounce_at": self.last_bounce_at, "bounces": self.bounces,
        }
