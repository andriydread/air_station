"""Wi-Fi recovery ladder tests: escalation order, resets, failure handling."""

import subprocess

from airmonitor.wifi_recovery import WifiRecovery


class StubEvents:
    def __init__(self):
        self.entries = []

    def log(self, level, source, event_type, message, details=None):
        self.entries.append((event_type, message))

    def types(self):
        return [event_type for (event_type, _m) in self.entries]


class RecordingRunner:
    def __init__(self, returncode=0):
        self.commands = []
        self.returncode = returncode

    def __call__(self, command):
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode, stdout="", stderr="")


def _which_with(available):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def make_recovery(events, runner, tools=("nmcli", "systemctl"), after=3):
    return WifiRecovery(
        "wlan0", events, after_failures=after, runner=runner, which=_which_with(tools)
    )


def test_no_action_before_threshold():
    runner = RecordingRunner()
    recovery = make_recovery(StubEvents(), runner, after=3)
    recovery.record_probe(False)
    recovery.record_probe(False)
    assert runner.commands == []


def test_escalation_ladder_nmcli():
    events = StubEvents()
    runner = RecordingRunner()
    recovery = make_recovery(events, runner, after=3)
    for _ in range(3):
        recovery.record_probe(False)
    # action 1: bounce via nmcli
    assert runner.commands == [
        ["sudo", "-n", "/usr/bin/nmcli", "radio", "wifi", "off"],
        ["sudo", "-n", "/usr/bin/nmcli", "radio", "wifi", "on"],
    ]
    for _ in range(3):
        recovery.record_probe(False)
    assert len(runner.commands) == 4  # action 2: second bounce
    for _ in range(3):
        recovery.record_probe(False)
    # action 3: service restart
    assert runner.commands[-1] == ["sudo", "-n", "/usr/bin/systemctl", "restart", "NetworkManager"]
    assert events.types().count("recovery_action") == 3


def test_falls_back_to_ip_link_without_nmcli():
    runner = RecordingRunner()
    recovery = make_recovery(StubEvents(), runner, tools=("ip", "systemctl"), after=2)
    recovery.record_probe(False)
    recovery.record_probe(False)
    assert runner.commands == [
        ["sudo", "-n", "/usr/bin/ip", "link", "set", "wlan0", "down"],
        ["sudo", "-n", "/usr/bin/ip", "link", "set", "wlan0", "up"],
    ]
    # non-NetworkManager stack restarts wpa_supplicant + dhcpcd
    recovery.actions_taken = 2
    recovery.record_probe(False)
    recovery.record_probe(False)
    assert runner.commands[-1] == [
        "sudo", "-n", "/usr/bin/systemctl", "restart", "wpa_supplicant", "dhcpcd",
    ]


def test_healthy_probe_resets_ladder_and_logs_recovery():
    events = StubEvents()
    runner = RecordingRunner()
    recovery = make_recovery(events, runner, after=2)
    recovery.record_probe(False)
    recovery.record_probe(False)  # action 1
    recovery.record_probe(True)
    assert "recovery_succeeded" in events.types()
    assert recovery.consecutive_failures == 0
    assert recovery.actions_taken == 0
    # the ladder starts from the bottom again
    recovery.record_probe(False)
    recovery.record_probe(False)
    assert runner.commands[-2:] == [
        ["sudo", "-n", "/usr/bin/nmcli", "radio", "wifi", "off"],
        ["sudo", "-n", "/usr/bin/nmcli", "radio", "wifi", "on"],
    ]


def test_command_failure_logged_not_raised():
    events = StubEvents()
    runner = RecordingRunner(returncode=1)  # sudoers missing, for example
    recovery = make_recovery(events, runner, after=2)
    recovery.record_probe(False)
    recovery.record_probe(False)
    assert "recovery_failed" in events.types()
    assert len(runner.commands) == 1  # stops after the first failing command


def test_disabled_when_threshold_zero():
    runner = RecordingRunner()
    recovery = make_recovery(StubEvents(), runner, after=0)
    for _ in range(10):
        recovery.record_probe(False)
    assert runner.commands == []


def test_no_tools_available_logs_and_moves_on():
    events = StubEvents()
    runner = RecordingRunner()
    recovery = make_recovery(events, runner, tools=(), after=1)
    recovery.record_probe(False)
    assert runner.commands == []
    assert "recovery_failed" in events.types()


def test_partial_bounce_failure_is_retried_next_escalation():
    """off succeeds, on fails -> the radio is down; the NEXT escalation must
    re-issue the full bounce or the blip becomes a permanent outage."""
    from airmonitor.wifi_recovery import WifiRecovery

    class Events:
        def __init__(self):
            self.entries = []

        def log(self, _lvl, _src, event_type, message, *args):
            self.entries.append((event_type, message))

    commands = []

    def runner(command):
        commands.append(command)
        ok = "off" not in command  # every "on" succeeds, "off" too — flip below
        class R:
            returncode = 0 if "on" not in command else 1
            stdout = ""
            stderr = "radio stuck"
        return R()

    events = Events()
    recovery = WifiRecovery(
        "wlan0", events, after_failures=2, runner=runner, which=lambda name: f"/usr/bin/{name}"
    )
    for _ in range(2):
        recovery.record_probe(False)
    assert any(e == "recovery_failed" for e, _ in events.entries)
    first_round = list(commands)
    assert any("off" in " ".join(c) for c in first_round)

    commands.clear()
    for _ in range(2):
        recovery.record_probe(False)
    # Second escalation runs the bounce again from the start (off then on).
    joined = [" ".join(c) for c in commands]
    assert any("off" in c for c in joined)


class _RecordingRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()


class _QuietEvents:
    def __init__(self):
        self.entries = []

    def log(self, _lvl, _src, event_type, message, *args):
        self.entries.append((event_type, message))


def test_service_restarts_back_off_during_a_long_outage():
    runner = _RecordingRunner()
    events = _QuietEvents()
    recovery = WifiRecovery(
        "wlan0", events, after_failures=6, runner=runner,
        which=lambda name: f"/usr/bin/{name}",
    )
    for _ in range(120):  # a one-hour outage at 30s probes
        recovery.record_probe(False)

    restarts = [c for c in runner.commands if "restart" in c]
    # bounces at 6 and 12; restart at 18; gate doubles -> 36, then 72.
    assert len(restarts) == 3
    assert recovery.consecutive_failures == 120


def test_isp_only_outage_stops_after_one_service_restart():
    runner = _RecordingRunner()
    events = _QuietEvents()
    recovery = WifiRecovery(
        "wlan0", events, after_failures=6, runner=runner,
        which=lambda name: f"/usr/bin/{name}",
    )
    for _ in range(120):
        recovery.record_probe(False, link_ok=True)  # link fine, upstream dead

    restarts = [c for c in runner.commands if "restart" in c]
    assert len(restarts) == 1  # one try, then hold — LAN access survives
    assert any(e == "recovery_hold" for e, _ in events.entries)
    # ...and the hold is reported once, not per probe.
    assert sum(1 for e, _ in events.entries if e == "recovery_hold") == 1

    # Real Wi-Fi trouble later (link down) escalates again.
    recovery.record_probe(True)  # brief recovery resets the ladder
    for _ in range(20):
        recovery.record_probe(False, link_ok=False)
    assert any("restart" in c for c in runner.commands[len(restarts) + 4:])
