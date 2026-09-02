"""Scriptable stand-ins for the sensor devices.

Each fake mirrors exactly the attribute/method surface the wrappers in
``airmonitor/sensors.py`` use, and lets a test script both good and bad
behaviour: value sequences, exceptions, stuck states, rejected commands.
"""

from typing import Dict, List, Optional


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
        self.raise_on_read: Optional[Exception] = None
        self.calibration_result = 12  # 0xFFFF simulates a rejected calibration
        self.start_calls = 0
        self.stop_calls = 0
        self.reinit_calls = 0
        self.persist_calls = 0

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

    def stop_periodic_measurement(self):
        self.stop_calls += 1

    def reinit(self):
        self.reinit_calls += 1

    def force_calibration(self, _target_co2: int) -> int:
        return self.calibration_result

    def persist_settings(self):
        self.persist_calls += 1


class FakeSht41Device:
    """Stands in for ``adafruit_sht4x.SHT4x``."""

    def __init__(self, temperature: float = 22.5, humidity: float = 45.0):
        self._temperature = temperature
        self._humidity = humidity
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
    """Stands in for ``lib.sps30_i2c.SPS30`` as used by the wrapper."""

    def __init__(self):
        self.values: Dict[str, float] = {
            "pm1": 1.1, "pm25": 2.5, "pm4": 3.0, "pm10": 4.2, "tps": 0.6,
            "nc05": 0.0, "nc10": 0.0, "nc25": 0.0, "nc40": 0.0, "nc100": 0.0,
        }
        self._data_ready = True
        self.raise_on_data_ready: Optional[Exception] = None
        self.raise_on_read: Optional[Exception] = None
        self.auto_cleaning_interval = 604800
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
