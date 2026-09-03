"""Router + internet probes, down/up events, the radio bounce, the glyph."""

import pytest

from manager.network import BOUNCE_AFTER, BOUNCE_OFF, BOUNCE_ON, WifiWatch, default_gateway
from tests.mocks.fake_devices import FakeRunner

ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0
"""


class Net:
    """Scripted reachability per (host, port)."""

    def __init__(self):
        self.up = {("192.168.1.1", 53): True, ("1.1.1.1", 53): True}
        self.attempts = []

    def connect(self, address, timeout):
        self.attempts.append(address)
        if not self.up.get(address, False):
            raise OSError("unreachable")

        class _Conn:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_exc):
                return False

        return _Conn()


@pytest.fixture
def watch(log, tmp_path):
    (tmp_path / "route").write_text(ROUTE)
    net = Net()
    runner = FakeRunner()
    sleeps = []
    w = WifiWatch(log, runner=runner, connector=net.connect, route_path=str(tmp_path / "route"),
                  sleeper=sleeps.append, monotonic=lambda: 0.0)
    w.net, w.runner_, w.sleeps = net, runner, sleeps
    return w


def test_gateway_parsing(tmp_path):
    (tmp_path / "route").write_text(ROUTE)
    assert default_gateway(str(tmp_path / "route")) == "192.168.1.1"
    assert default_gateway(str(tmp_path / "route"), interface="eth0") is None
    assert default_gateway(str(tmp_path / "missing")) is None


def test_healthy_round_remembers_the_router_port(watch):
    result = watch.tick(now=0)
    assert result == {"lan_ms": 0.0, "wan_ms": 0.0, "bounced": False}
    assert watch.gateway == "192.168.1.1" and watch.router_port == 53
    assert watch.router_ok is True and watch.internet_ok is True and watch.glyph() is False
    assert watch.status()["router_failures"] == 0


def test_port_80_fallback(watch):
    watch.net.up[("192.168.1.1", 53)] = False
    watch.net.up[("192.168.1.1", 80)] = True
    watch.tick(now=0)
    assert watch.router_port == 80 and watch.router_ok is True


def test_internet_only_failure_never_bounces(watch, db):
    watch.net.up[("1.1.1.1", 53)] = False
    for i in range(BOUNCE_AFTER + 2):
        watch.tick(now=i * 30)
    types = [e["type"] for e in db.recent_events()]
    assert types.count("internet_down") == 1 and "wifi_bounce" not in types and "wifi_down" not in types
    assert watch.glyph() is False and watch.bounces == 0
    watch.net.up[("1.1.1.1", 53)] = True
    watch.tick(now=1000)
    assert [e["type"] for e in db.recent_events()][0] == "internet_up"


def test_router_failures_declare_down_then_bounce_at_six(watch, db):
    watch.net.up[("192.168.1.1", 53)] = False
    watch.net.up[("1.1.1.1", 53)] = False  # nothing is reachable through a dead router
    for i in range(BOUNCE_AFTER):
        result = watch.tick(now=i * 30)
    assert result["bounced"] is True and watch.bounces == 1 and watch.last_bounce_at == 150
    assert watch.runner_.calls == [BOUNCE_OFF, BOUNCE_ON] and watch.sleeps == [2.0]
    types = [e["type"] for e in db.recent_events()]
    assert types.count("wifi_down") == 1 and types.count("wifi_bounce") == 1
    assert types.count("internet_down") == 1  # the WAN is unreachable through a dead router too
    assert watch.router_failures == 0 and watch.glyph() is True
    # the cooldown prevents a second bounce right away
    for i in range(BOUNCE_AFTER):
        watch.tick(now=180 + i * 30)
    assert watch.bounces == 1
    watch.tick(now=150 + 600 + 30 * BOUNCE_AFTER)
    for i in range(BOUNCE_AFTER):
        watch.tick(now=1000 + i * 30)
    assert watch.bounces == 2


def test_recovery_after_a_bounce_logs_up_events(watch, db):
    watch.net.up[("192.168.1.1", 53)] = False
    watch.net.up[("1.1.1.1", 53)] = False
    for i in range(BOUNCE_AFTER):
        watch.tick(now=i * 30)
    watch.net.up[("192.168.1.1", 53)] = True
    watch.net.up[("1.1.1.1", 53)] = True
    watch.tick(now=200)
    types = [e["type"] for e in db.recent_events()]
    assert types[:2] == ["internet_up", "wifi_up"] or types[:2] == ["wifi_up", "internet_up"]
    assert watch.glyph() is False


def test_bounce_failure_is_an_error_event(watch, db):
    watch.runner_.results[tuple(BOUNCE_ON)] = FakeRunner.Completed(returncode=1)
    assert watch.bounce(now=0) is False
    event = db.recent_events()[0]
    assert event["type"] == "wifi_bounce" and event["level"] == "error" and event["details"]["results"] == [0, 1]


def test_no_gateway_counts_as_router_failure(watch, tmp_path):
    (tmp_path / "route").write_text("Iface\tDestination\tGateway\n")
    watch.tick(now=0)
    watch.tick(now=30)
    assert watch.router_ok is False and watch.gateway is None and watch.glyph() is True
