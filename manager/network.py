"""Wi-Fi watch: probe the router and the internet every 30 s; bounce the radio
only when the router itself stops answering.

Router down for six probes in a row (three minutes) → ``nmcli radio wifi
off`` / ``on`` through sudo — the whole recovery. Internet-only failures are
logged and light the glyph; a bounce would not help and would only drop the
dashboard, which lives on the LAN.
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
DOWN_AFTER = 2           # failed probes in a row before "down" is declared
BOUNCE_AFTER = 6         # failed router probes in a row before the radio bounce
BOUNCE_COOLDOWN = 600.0  # seconds between two bounces
BOUNCE_PAUSE = 2.0
BOUNCE_OFF = ["sudo", "nmcli", "radio", "wifi", "off"]
BOUNCE_ON = ["sudo", "nmcli", "radio", "wifi", "on"]
ROUTE_PATH = "/proc/net/route"


def default_gateway(path: str = ROUTE_PATH, interface: Optional[str] = None) -> Optional[str]:
    """The default route's gateway as dotted text, from /proc/net/route."""
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
    """Round-trip of a TCP connect in ms, or None when it failed."""
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
        self.history: List[bool] = []  # last router results, newest last
        self.probes = 0

    # --- one probe round ------------------------------------------------------------------

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
        if failures >= DOWN_AFTER and state is not False:
            what = "router" if which == "router" else "internet"
            self.log.event("warning", "wifi", down_type, f"{what} not answering ({failures} probes)",
                           failures=failures, gateway=self.gateway)
            setattr(self, state_attr, False)

    def bounce(self, now: float) -> bool:
        """Radio off, two seconds, radio on. True when both commands returned 0."""
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
                       results=results, count=self.bounces)
        return ok

    # --- for the frame and the status document ------------------------------------------------

    def glyph(self) -> bool:
        return len(self.history) >= DOWN_AFTER and not any(self.history[-DOWN_AFTER:])

    def status(self) -> Dict[str, Any]:
        return {
            "router_ok": self.router_ok, "internet_ok": self.internet_ok,
            "gateway": self.gateway, "lan_ms": self.last_lan_ms, "wan_ms": self.last_wan_ms,
            "router_failures": self.router_failures, "wan_failures": self.wan_failures,
            "last_bounce_at": self.last_bounce_at, "bounces": self.bounces,
        }
