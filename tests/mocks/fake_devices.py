"""Scriptable stand-ins for the hardware and the clock.

Each device fake mirrors exactly the attribute/method surface the wrappers
use, and lets a test script both good and bad behaviour: value sequences,
exceptions, stuck states, rejected commands. ``FakeClock``, ``FakeRunner``
and ``FakePanel`` stand in for time, subprocess and the e-paper.
"""

from typing import Any, Dict, List, Optional


class FakeScd41Device:
    """Stands in for ``adafruit_scd4x.SCD4X``."""

    def __init__(self):
        self.co2_values: List[float] = []  # popped per read; empty -> default_co2
        self.default_co2 = 600.0
        self._data_ready = True
        self.raise_on_data_ready: Optional[Exception] = None
        self.temperature = 23.0
        self.relative_humidity = 40.0
        self.altitude = 0
        self.temperature_offset = 4.0
        self.self_calibration_enabled = False
        self.serial_number = (0x11, 0x22, 0x33)
        self.raise_on_read: Optional[Exception] = None
        self.calibration_result = 12  # 0xFFFF simulates a rejected calibration
        self.ambient_pressures: List[float] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.reinit_calls = 0
        self.persist_calls = 0
        self.single_shots = 0
        self.self_tests = 0
        self.self_test_error: Optional[Exception] = None  # RuntimeError("Self test failed") like the driver
        self.power_downs = 0
        self.wake_ups = 0

    @property
    def data_ready(self) -> bool:
        if self.raise_on_data_ready is not None:
            raise self.raise_on_data_ready
        return self._data_ready

    @data_ready.setter
    def data_ready(self, value: bool) -> None:
        self._data_ready = value

    @property
    def CO2(self) -> float:
        if self.raise_on_read is not None:
            raise self.raise_on_read
        if self.co2_values:
            return self.co2_values.pop(0)
        return self.default_co2

    def start_periodic_measurement(self):
        self.start_calls += 1

    def measure_single_shot(self):
        # the real driver sleeps 5 s here; the fake clock makes that free
        self.single_shots += 1

    def self_test(self):
        self.self_tests += 1  # the real driver sleeps 10 s here
        if self.self_test_error is not None:
            raise self.self_test_error

    def power_down(self):
        self.power_downs += 1

    def wake_up(self):
        self.wake_ups += 1

    def stop_periodic_measurement(self):
        self.stop_calls += 1

    def reinit(self):
        self.reinit_calls += 1

    def force_calibration(self, _target_co2: int) -> int:
        return self.calibration_result

    def persist_settings(self):
        self.persist_calls += 1

    def set_ambient_pressure(self, hpa: float) -> None:
        self.ambient_pressures.append(hpa)

    @property
    def ambient_pressure(self) -> int:
        return int(self.ambient_pressures[-1]) if self.ambient_pressures else 0

    sensor_variant_name = "SCD41"


class FakeSht41Device:
    """Stands in for ``adafruit_sht4x.SHT4x``."""

    def __init__(self, temperature: float = 22.5, humidity: float = 45.0):
        self._temperature = temperature
        self._humidity = humidity
        self.mode = None
        self.serial_number = 0xABCD
        self.raise_on_read: Optional[Exception] = None

    @property
    def temperature(self) -> float:
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return self._temperature

    @property
    def relative_humidity(self) -> float:
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return self._humidity


class FakeSps30Device:
    """Stands in for ``drivers.sps30_i2c.SPS30`` as used by the wrapper.

    ``read()`` uses the driver's own key names (nc10 = 1.0 µm, nc25 = 2.5 µm,
    nc40 = 4 µm, nc100 = 10 µm); the wrapper maps them to the row columns.
    """

    def __init__(self):
        self.values: Dict[str, float] = {
            "pm1": 1.1, "pm25": 2.5, "pm4": 3.0, "pm10": 4.2, "tps": 0.6,
            "nc05": 7.5, "nc10": 8.6, "nc25": 8.8, "nc40": 8.9, "nc100": 8.9,
        }
        self._data_ready = True
        self.raise_on_data_ready: Optional[Exception] = None
        self.raise_on_read: Optional[Exception] = None
        self._auto_cleaning_interval = 604800
        self.interval_writes: List[int] = []
        self.firmware_version = (2, 2)
        # Device Status Register as the driver decodes it; tests flip bits.
        self.status = {"raw": 0, "speed_warning": False, "laser_error": False, "fan_error": False}
        self.raise_on_status: Optional[Exception] = None
        self.wakeup_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.sleep_calls = 0
        self.clean_calls = 0

    def wakeup(self):
        self.wakeup_calls += 1

    def start_measurement(self):
        self.start_calls += 1

    def stop_measurement(self):
        self.stop_calls += 1

    def sleep(self):
        self.sleep_calls += 1

    def force_clean(self):
        self.clean_calls += 1

    @property
    def auto_cleaning_interval(self) -> int:
        return self._auto_cleaning_interval

    @auto_cleaning_interval.setter
    def auto_cleaning_interval(self, seconds: int) -> None:
        self.interval_writes.append(seconds)
        self._auto_cleaning_interval = seconds

    def set_auto_cleaning_interval(self, seconds: int) -> None:
        self.auto_cleaning_interval = seconds

    @property
    def data_ready(self) -> bool:
        if self.raise_on_data_ready is not None:
            raise self.raise_on_data_ready
        return self._data_ready

    @data_ready.setter
    def data_ready(self, value: bool) -> None:
        self._data_ready = value

    def read(self) -> Dict[str, float]:
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return dict(self.values)

    def read_firmware(self):
        return self.firmware_version

    def read_device_status(self) -> Dict[str, object]:
        if self.raise_on_status is not None:
            raise self.raise_on_status
        return dict(self.status)


class ScriptedI2CDevice:
    """Plays back queued response frames; records written commands.

    Failure injection: `raise_on_write` / `raise_on_read` hold exceptions
    consumed one write/read at a time (list) or raised every time (single
    exception) — enough to express "NAK the first wakeup, ACK the second"
    or an I2C error mid-transaction.
    """

    def __init__(self):
        self.responses: List[bytes] = []
        self.written: List[bytes] = []
        self.raise_on_write = None
        self.raise_on_read = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def _maybe_raise(self, source):
        if source is None:
            return None
        if isinstance(source, list):
            if source:
                exc = source.pop(0)
                if exc is not None:
                    raise exc
            return None
        raise source

    def write(self, buffer, end=None):
        self._maybe_raise(self.raise_on_write)
        self.written.append(bytes(buffer[: end if end is not None else len(buffer)]))

    def readinto(self, buffer, end=None):
        self._maybe_raise(self.raise_on_read)
        response = self.responses.pop(0)
        length = end if end is not None else len(buffer)
        assert len(response) == length, "test scripted the wrong response size"
        buffer[:length] = response


class FakeClock:
    """Wall clock and monotonic clock under test control.

    Both advance together by default; ``jump_wall`` moves only the wall clock
    (an NTP correction) so clock-jump detection can be exercised.
    """

    def __init__(self, start: float = 1_756_900_800.0):
        self._wall = float(start)
        self._mono = 1000.0
        self.sleeps: List[float] = []

    def now(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._wall += seconds
        self._mono += seconds

    def jump_wall(self, seconds: float) -> None:
        self._wall += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakeRunner:
    """Stands in for ``subprocess.run``: records argv, answers from a script.

    ``results`` maps the first argv element (or the whole argv as a tuple) to
    a ``CompletedProcess``-like object or an exception to raise; unmatched
    commands succeed with empty output.
    """

    class Completed:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def __init__(self):
        self.calls: List[List[str]] = []
        self.results: Dict[Any, Any] = {}

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)
        result = self.results.get(tuple(argv), self.results.get(argv[0]))
        if isinstance(result, BaseException):
            raise result
        if result is None:
            return self.Completed()
        if callable(result):
            return result(argv)
        return result


class PngPanelDriver:
    """A UC8253C stand-in for ``--fake``: every frame becomes a PNG on disk."""

    MODE_FULL = "full"
    MODE_PARTIAL = "partial"

    def __init__(self, out_dir):
        from pathlib import Path

        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.frames = 0
        self.modes: List[str] = []

    def display_image(self, image, mode="partial", auto_sleep=True):
        self.frames += 1
        self.modes.append(mode)
        image.save(self.out_dir / "panel.png")
        image.save(self.out_dir / f"panel-{self.frames:04d}-{mode}.png")

    def sleep(self):
        pass

    def close(self):
        pass


class FakePanel:
    """Stands in for the e-paper: records every frame and its refresh mode."""

    def __init__(self):
        self.frames: List[Any] = []
        self.modes: List[str] = []
        self.raise_on_show: Optional[Exception] = None
        self.sleep_calls = 0
        self.closed = False

    def show(self, image, full: bool) -> None:
        if self.raise_on_show is not None:
            raise self.raise_on_show
        self.frames.append(image)
        self.modes.append("full" if full else "partial")

    def sleep(self) -> None:
        self.sleep_calls += 1

    def close(self) -> None:
        self.closed = True
