"""The test doubles themselves behave as the other tests assume."""

import pytest

from tests.mocks.fake_devices import (
    FakeClock,
    FakePanel,
    FakeRunner,
    FakeSps30Device,
    ScriptedI2CDevice,
)


def test_fake_clock_advances_both_clocks_and_jumps_wall_alone():
    clock = FakeClock(start=1000.0)
    wall, mono = clock.now(), clock.monotonic()
    clock.advance(10)
    assert clock.now() == wall + 10 and clock.monotonic() == mono + 10
    clock.jump_wall(7)
    assert clock.now() == wall + 17 and clock.monotonic() == mono + 10
    clock.sleep(0.2)
    assert clock.sleeps == [0.2]


def test_fake_sps30_read_has_the_ten_driver_keys():
    device = FakeSps30Device()
    assert set(device.read()) == {
        "pm1", "pm25", "pm4", "pm10", "tps", "nc05", "nc10", "nc25", "nc40", "nc100",
    }
    device.set_auto_cleaning_interval(0)
    assert device.auto_cleaning_interval == 0 and device.interval_writes == [0]


def test_scripted_i2c_device_raises_on_schedule():
    wire = ScriptedI2CDevice()
    wire.raise_on_write = [OSError("first"), None]
    with pytest.raises(OSError, match="first"):
        wire.write(b"\x00")
    wire.write(b"\x01")  # second write goes through
    assert wire.written == [b"\x01"]


def test_fake_runner_records_and_scripts():
    runner = FakeRunner()
    runner.results["vcgencmd"] = FakeRunner.Completed(stdout="throttled=0x0\n")
    runner.results[("sudo", "reboot")] = OSError("no sudo")
    assert runner(["vcgencmd", "get_throttled"]).stdout.startswith("throttled=")
    with pytest.raises(OSError):
        runner(["sudo", "reboot"])
    assert runner(["anything"]).returncode == 0
    assert runner.calls[0] == ["vcgencmd", "get_throttled"]


def test_fake_panel_records_modes():
    panel = FakePanel()
    panel.show("frame-a", full=False)
    panel.show("frame-b", full=True)
    assert panel.frames == ["frame-a", "frame-b"]
    assert panel.modes == ["partial", "full"]
