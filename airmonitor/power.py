"""Power-supply monitoring via ``vcgencmd get_throttled``.

The station runs behind a UPS whose health can't be read directly; the
SoC's throttle flags are the only visibility into brown-outs. Undervoltage
correlates with sensor glitches and Wi-Fi hangs, so a power problem should
become a logged event, not a guess.
"""

import logging
import subprocess
from typing import Any, Callable, Dict, Optional

from airmonitor.sensors import utc_now_iso

LOGGER = logging.getLogger("airmonitor")

# Bit positions from the Raspberry Pi firmware documentation.
_FLAG_BITS = {
    "undervoltage_now": 0,
    "freq_capped_now": 1,
    "throttled_now": 2,
    "soft_temp_limit_now": 3,
    "undervoltage_since_boot": 16,
    "freq_capped_since_boot": 17,
    "throttled_since_boot": 18,
    "soft_temp_limit_since_boot": 19,
}

_NOW_FLAGS = tuple(name for name in _FLAG_BITS if name.endswith("_now"))


def parse_throttled(text: str) -> Dict[str, bool]:
    """Parse ``throttled=0x50005`` into named boolean flags."""
    value_text = text.strip().split("=", 1)[-1]
    value = int(value_text, 16)
    return {name: bool(value & (1 << bit)) for name, bit in _FLAG_BITS.items()}


def read_throttled(runner: Callable = subprocess.run) -> Optional[Dict[str, bool]]:
    """Return the current throttle flags, or None when vcgencmd is unusable."""
    try:
        result = runner(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return parse_throttled(result.stdout)
    except Exception:
        LOGGER.debug("vcgencmd get_throttled unavailable", exc_info=True)
        return None


class PowerMonitor:
    """Periodic throttle-flag check; logs an event whenever flags change."""

    def __init__(self, events, runner: Callable = subprocess.run):
        self.events = events
        self.runner = runner
        self.state: Dict[str, Any] = {"available": None, "healthy": None}
        self._last_flags: Optional[Dict[str, bool]] = None
        self._unavailable_reported = False

    def check(self) -> None:
        flags = read_throttled(self.runner)
        if flags is None:
            self.state.update(available=False, healthy=None)
            if not self._unavailable_reported:
                self._unavailable_reported = True
                self.events.log(
                    logging.WARNING, "power", "monitor_unavailable",
                    "vcgencmd get_throttled is not available; power monitoring disabled",
                )
            return

        healthy = not any(flags[name] for name in _NOW_FLAGS)
        self.state.update(available=True, healthy=healthy, **flags)
        self.state["last_checked_at"] = utc_now_iso()

        if flags != self._last_flags:
            active = sorted(name for name, is_set in flags.items() if is_set)
            level = logging.WARNING if not healthy else logging.INFO
            message = (
                f"Power flags changed: {', '.join(active) if active else 'all clear'}"
            )
            self.events.log(level, "power", "throttle_flags", message, dict(flags))
        self._last_flags = flags
