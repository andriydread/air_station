"""TracedI2C: every transaction one debug line, behaviour untouched, errors re-raised."""

import pytest

from collector.i2c_trace import TracedI2C
from shared.events import Log


class FakeBus:
    """The three transaction methods of busio.I2C, scripted."""

    def __init__(self):
        self.calls = []
        self.reply = b""
        self.raise_on_write = None

    def try_lock(self):
        return True

    def unlock(self):
        pass

    def writeto(self, address, buffer, *, start=0, end=None):
        self.calls.append(("w", address, bytes(buffer[start:end])))
        if self.raise_on_write is not None:
            raise self.raise_on_write

    def readfrom_into(self, address, buffer, *, start=0, end=None):
        self.calls.append(("r", address, end))
        buffer[start:start + len(self.reply)] = self.reply

    def writeto_then_readfrom(self, address, out_buffer, in_buffer, *, out_start=0, out_end=None,
                              in_start=0, in_end=None, stop=False):
        self.calls.append(("wr", address, bytes(out_buffer[out_start:out_end])))
        in_buffer[in_start:in_start + len(self.reply)] = self.reply


@pytest.fixture
def rig(tmp_config, db):
    log = Log("collector", tmp_config, db=db, strict=True)
    bus = FakeBus()
    yield bus, TracedI2C(bus, log, monotonic=iter(range(0, 10_000)).__next__), log, tmp_config
    log.close()


def _lines(config):
    return [line for line in (config.paths.logs / "collector.log").read_text().splitlines() if " i2c " in line]


def test_write_read_and_write_then_read_are_one_line_each(rig):
    bus, i2c, log, config = rig
    bus.reply = b"\x01\xe1\xb8"
    i2c.writeto(0x62, b"\xec\x05")
    buffer = bytearray(3)
    i2c.readfrom_into(0x62, buffer)
    i2c.writeto_then_readfrom(0x44, b"\xfd", buffer)
    assert bus.calls == [("w", 0x62, b"\xec\x05"), ("r", 0x62, None), ("wr", 0x44, b"\xfd")]
    assert bytes(buffer) == b"\x01\xe1\xb8"
    lines = _lines(config)
    assert len(lines) == 3 and i2c.transactions == 3 and i2c.errors == 0
    assert "i2c tx addr=0x62 w=ec05 ms=" in lines[0]
    assert "i2c tx addr=0x62 r=01e1b8 ms=" in lines[1]  # a pure read has no w=
    assert "i2c tx addr=0x44 w=fd r=01e1b8 ms=" in lines[2]


def test_an_error_is_logged_with_errno_and_re_raised(rig):
    bus, i2c, log, config = rig
    bus.raise_on_write = OSError(121, "Remote I/O error")
    with pytest.raises(OSError):
        i2c.writeto(0x69, b"\x03\x00")
    line = _lines(config)[0]
    assert "i2c error addr=0x69 w=0300 errno=121" in line and "Remote I/O error" in line
    assert i2c.errors == 1 and i2c.transactions == 0


def test_lock_and_everything_else_pass_through(rig):
    bus, i2c, log, config = rig
    assert i2c.try_lock() is True and i2c.unlock() is None
    assert i2c.calls is bus.calls  # any other attribute is the bus's own
    assert _lines(config) == []


def test_silent_below_debug(tmp_config, db):
    import dataclasses

    config = dataclasses.replace(tmp_config, logging=dataclasses.replace(tmp_config.logging, level="info"))
    log = Log("collector", config, db=db, strict=True)
    try:
        bus = FakeBus()
        TracedI2C(bus, log).writeto(0x62, b"\x21\x9d")
        assert bus.calls and _lines(config) == []
    finally:
        log.close()
