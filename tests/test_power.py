"""Power monitoring tests: bitmask parsing and event-on-change behaviour."""

import subprocess

from airmonitor.power import PowerMonitor, parse_throttled


class StubEvents:
    def __init__(self):
        self.entries = []

    def log(self, level, source, event_type, message, details=None):
        self.entries.append((source, event_type, message))

    def types(self):
        return [event_type for (_s, event_type, _m) in self.entries]


def _runner_for(output):
    def runner(_cmd, **_kwargs):
        return subprocess.CompletedProcess(_cmd, 0, stdout=output, stderr="")

    return runner


def test_parse_all_clear():
    flags = parse_throttled("throttled=0x0")
    assert not any(flags.values())


def test_parse_undervoltage_and_history():
    # 0x50005: undervoltage+throttled now, and both since boot.
    flags = parse_throttled("throttled=0x50005")
    assert flags["undervoltage_now"] is True
    assert flags["throttled_now"] is True
    assert flags["freq_capped_now"] is False
    assert flags["undervoltage_since_boot"] is True
    assert flags["throttled_since_boot"] is True


def test_monitor_logs_only_on_change():
    events = StubEvents()
    monitor = PowerMonitor(events, runner=_runner_for("throttled=0x0\n"))
    monitor.check()
    monitor.check()
    assert events.types() == ["throttle_flags"]  # initial state logged once
    assert monitor.state["healthy"] is True

    monitor.runner = _runner_for("throttled=0x50005\n")
    monitor.check()
    monitor.check()
    assert events.types() == ["throttle_flags", "throttle_flags"]
    assert monitor.state["healthy"] is False
    assert monitor.state["undervoltage_now"] is True


def test_monitor_handles_missing_vcgencmd():
    events = StubEvents()

    def broken_runner(_cmd, **_kwargs):
        raise FileNotFoundError("vcgencmd")

    monitor = PowerMonitor(events, runner=broken_runner)
    monitor.check()
    monitor.check()  # unavailable is reported exactly once
    assert events.types() == ["monitor_unavailable"]
    assert monitor.state["available"] is False
