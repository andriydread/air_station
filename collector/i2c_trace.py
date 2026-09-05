"""Every I2C transaction as one debug line — the whole conversation with the sensors.

``TracedI2C`` wraps the ``board.I2C()`` object, the one seam all three drivers
go through (Adafruit's ``I2CDevice`` calls ``writeto`` / ``readfrom_into`` /
``writeto_then_readfrom`` under ``try_lock`` / ``unlock``). Behaviour is
unchanged; each call becomes a line like::

    DEBUG collector i2c tx addr=0x62 w=ec05 r=01e1b8664b7bd2a3 ms=1.2
    DEBUG collector i2c error addr=0x62 w=ec05 errno=121 error="[Errno 121] Remote I/O error" ms=0.3

Switched by ``logging.i2c_trace`` in ``config.toml``; only meaningful at
``level = "debug"``. About 30 lines a minute on the 30 s beat.
"""

import time
from typing import Any, Callable, Optional


def _hex(buffer: Any, start: int = 0, end: Optional[int] = None) -> str:
    try:
        return bytes(memoryview(buffer)[start:end]).hex()
    except Exception:
        return "?"


class TracedI2C:
    def __init__(self, bus: Any, log, monotonic: Callable[[], float] = time.monotonic):
        self._bus = bus
        self._log = log
        self._monotonic = monotonic
        self.transactions = 0
        self.errors = 0

    # --- the three transactions ---------------------------------------------------------

    def writeto(self, address: int, buffer: Any, *, start: int = 0, end: Optional[int] = None) -> None:
        self._traced(address, _hex(buffer, start, end), None,
                     lambda: self._bus.writeto(address, buffer, start=start, end=end))

    def readfrom_into(self, address: int, buffer: Any, *, start: int = 0, end: Optional[int] = None) -> None:
        self._traced(address, "", lambda: _hex(buffer, start, end),
                     lambda: self._bus.readfrom_into(address, buffer, start=start, end=end))

    def writeto_then_readfrom(self, address: int, out_buffer: Any, in_buffer: Any, *,
                              out_start: int = 0, out_end: Optional[int] = None,
                              in_start: int = 0, in_end: Optional[int] = None, **kwargs: Any) -> None:
        self._traced(address, _hex(out_buffer, out_start, out_end), lambda: _hex(in_buffer, in_start, in_end),
                     lambda: self._bus.writeto_then_readfrom(
                         address, out_buffer, in_buffer, out_start=out_start, out_end=out_end,
                         in_start=in_start, in_end=in_end, **kwargs))

    def _traced(self, address: int, written: str, read_after: Optional[Callable[[], str]],
                call: Callable[[], None]) -> None:
        started = self._monotonic()
        try:
            call()
        except Exception as exc:
            self.errors += 1
            self._log.debug("i2c", "error", addr=f"0x{address:02x}", w=written,
                            errno=getattr(exc, "errno", None), error=str(exc),
                            ms=round((self._monotonic() - started) * 1000, 1))
            raise
        self.transactions += 1
        kv = {"addr": f"0x{address:02x}"}
        if written:
            kv["w"] = written
        if read_after is not None:
            kv["r"] = read_after()
        kv["ms"] = round((self._monotonic() - started) * 1000, 1)
        self._log.debug("i2c", "tx", **kv)

    # --- everything else is the bus itself --------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bus, name)
