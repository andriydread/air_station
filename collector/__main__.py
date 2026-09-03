"""The collector program: ``python -m collector`` (``--fake`` on a machine without sensors).

Start everything, run the 10 s beat forever on one thread, stop cleanly.
Tasks: sample (10 s, wall-aligned), commands (2 s), status (30 s), weather
pressure into the SCD41 (30 min), the Sunday 04:00 fan clean (checked every
minute). Heartbeats and clock-jump detection come from the shared loop.
"""

import argparse
import subprocess
import sys
from typing import Any, Callable, Optional

from collector.commands import CommandRunner
from collector.sampling import SAMPLE_INTERVAL, Sampler
from collector.sensors import Scd41, Sht41, Sps30
from collector.status import build_status, debug_sample_lines
from shared import clock
from shared.config import Config
from shared.db import Database
from shared.events import Log
from shared.heartbeat import SystemdNotifier
from shared.loop import Loop, Task

APP = "collector"
COMMAND_POLL = 2
STATUS_EVERY = 30
PRESSURE_EVERY = 1800
FAN_CLEAN_DAY = 6      # Sunday (Monday = 0)
FAN_CLEAN_HOUR = 4
FAN_CLEAN_MINUTE = 0


class Collector:
    def __init__(self, config, db: Database, log: Log, i2c_factory: Callable[[], Any],
                 ntp_runner: Callable = subprocess.run, sps30_factory=None):
        self.config = config
        self.db = db
        self.log = log
        self.i2c_factory = i2c_factory
        self.ntp_runner = ntp_runner
        self.sps30_factory = sps30_factory
        self.started_at = clock.now()
        self.fan_clean_schedule = clock.LocalSchedule(FAN_CLEAN_HOUR, FAN_CLEAN_MINUTE, weekday=FAN_CLEAN_DAY)
        self.sampler: Optional[Sampler] = None
        self.commands: Optional[CommandRunner] = None
        self.ntp_synced: Optional[bool] = None

    # --- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        self.log.start_line(self.config)
        failed = self.db.fail_running(APP, "collector restarted")
        if failed:
            self.log.info("app", "stale_commands_failed", count=failed)
        self.ntp_synced = clock.wait_for_ntp(runner=self.ntp_runner)
        if not self.ntp_synced:
            self.log.event("warning", "app", "clock_unsynced",
                           "system time not confirmed by NTP; writing anyway")
        bus = self.i2c_factory()
        scd41 = Scd41(bus, self.config, self.log, sleep=clock.sleep, monotonic=clock.monotonic)
        sht41 = Sht41(bus, self.config, self.log)
        sps30 = Sps30(bus, self.config, self.log, device_factory=self.sps30_factory)
        self.sampler = Sampler(self.db, self.log, scd41, sht41, sps30, i2c_factory=self.i2c_factory,
                               monotonic=clock.monotonic)
        self.commands = CommandRunner(self.db, self.log, self.sampler, self.config, monotonic=clock.monotonic)
        self.log.event("info", "app", "started", "collector started",
                       ntp_synced=self.ntp_synced, sampling="beat", interval_s=SAMPLE_INTERVAL)
        for sensor in self.sampler.sensors:  # warm-up starts now, not at the first beat
            sensor.ensure(clock.now())
        self.publish_status()

    def stop(self, reason: str) -> None:
        if self.sampler is not None:
            for sensor in self.sampler.sensors:
                sensor.stop()
        self.log.event("info", "app", "shutdown", f"collector stopping: {reason}", reason=reason)
        try:
            self.publish_status()
        except Exception:
            pass

    def tasks(self):
        return [
            Task("sample", SAMPLE_INTERVAL, self.sample, aligned=True, first_run_immediately=False),
            Task("commands", COMMAND_POLL, self.process_commands),
            Task("status", STATUS_EVERY, self.publish_status),
            Task("pressure", PRESSURE_EVERY, self.apply_pressure),
            Task("fan_clean", 60, self.scheduled_fan_clean, first_run_immediately=False),
        ]

    # --- the jobs --------------------------------------------------------------------------

    def sample(self) -> None:
        record = self.sampler.beat(clock.now())
        if self.log.level == "debug":
            debug_sample_lines(self.log, record)

    def process_commands(self) -> None:
        self.commands.process(clock.now())

    def publish_status(self) -> None:
        if self.sampler is None:
            return
        self.db.set_state("collector_status", build_status(
            self.sampler, self.started_at, clock.now(), self.log.failures,
            self.commands.last_calibration if self.commands else None))

    def apply_pressure(self) -> None:
        doc = self.db.get_state("last_weather")
        hpa = (doc or {}).get("value", {}).get("pressure_hpa") if doc else None
        if not isinstance(hpa, (int, float)):
            return
        if self.sampler.scd41.set_ambient_pressure(float(hpa)):
            self.log.info("scd41", "pressure_applied", hpa=float(hpa))

    def scheduled_fan_clean(self) -> None:
        if not self.fan_clean_schedule.due(clock.now()):
            return
        sps30 = self.sampler.sps30
        if sps30.device is None:
            self.log.warning("sps30", "scheduled_clean_skipped", reason="sensor missing")
            return
        try:
            sps30.force_clean(clock.now(), manual=False)
        except Exception as exc:
            self.log.warning("sps30", "scheduled_clean_failed", error=str(exc))


def run(config, db: Database, log: Log, i2c_factory, notifier: Optional[SystemdNotifier] = None,
        max_passes: Optional[int] = None, **kwargs) -> str:
    collector = Collector(config, db, log, i2c_factory, **kwargs)
    collector.start()
    loop = Loop(log, notifier, collector.tasks())
    loop.install_signal_handlers()
    reason = "error"
    try:
        reason = loop.run(max_passes=max_passes)
    except BaseException as exc:  # a crash still stops the sensors and logs why
        reason = f"fatal: {exc!r}"
        raise
    finally:
        collector.stop(reason)
    return reason


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="collector")
    parser.add_argument("--fake", action="store_true", help="use fake hardware (dev server / demo)")
    parser.add_argument("--config", default=None, help="path to config.toml (default: repo root)")
    args = parser.parse_args(argv)
    if args.fake:
        from tests.mocks.fake_hardware import install
        install()
        from tests.mocks.generators import install_generated_devices
        sps30_factory = install_generated_devices()  # drifting, plausible values
    else:
        sps30_factory = None
    config = Config.load(args.config)
    db = Database(config.paths.database, now=clock.now)
    log = Log(APP, config, db=db)

    def i2c_factory():
        import board  # the Blinka library, present only on the Pi (or faked)
        return board.I2C()

    try:
        run(config, db, log, i2c_factory, SystemdNotifier(), sps30_factory=sps30_factory)
    except Exception:
        log.exception("app", "fatal")
        return 1
    finally:
        log.close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
