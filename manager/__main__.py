"""The manager program: ``python -m manager`` (``--fake`` on a machine without the panel).

One single-threaded loop: the minute frame (display_data → panel), commands
every 2 s, status every 30 s, weather every 30 min (retry in 2 min), router
and internet probes every 30 s, vitals and power every minute, the hourly
rollup at :00, the nightly job at 00:05 local, orphaned commands every 10
minutes, and the watch over the collector inside the minute job.
"""

import argparse
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Any, Callable, Dict, Optional

from manager import weather as weather_mod
from manager.commands import CommandRunner
from manager.display import Panel
from manager.frame import FrameBuilder
from manager.machine import Machine, Sources
from manager.maintenance import CollectorWatch, Hourly, Nightly, fail_unclaimed
from manager.network import ROUTE_PATH, PROBE_EVERY, WifiWatch
from manager.status import build_status, debug_frame_line, debug_weather_line
from shared import clock
from shared.config import Config
from shared.db import Database
from shared.events import Log
from shared.heartbeat import SystemdNotifier
from shared.loop import Loop, Task
from shared.render import render

APP = "manager"
MINUTE = 60
COMMAND_POLL = 2
STATUS_EVERY = 30
MACHINE_EVERY = 60
UNCLAIMED_EVERY = 600
FIRST_FRAME_DELAY = 5  # seconds: lets the collector publish its first status after a common boot


class Manager:
    def __init__(self, config, db: Database, log: Log, *, notifier: Optional[SystemdNotifier] = None,
                 panel_factory: Optional[Callable[[], Any]] = None,
                 opener: Callable = urllib.request.urlopen, runner: Callable = subprocess.run,
                 spawner: Callable = subprocess.Popen, connector: Callable = socket.create_connection,
                 sources: Optional[Sources] = None, route_path: str = ROUTE_PATH):
        self.config = config
        self.db = db
        self.log = log
        self.notifier = notifier or SystemdNotifier(address="")
        self.opener = opener
        self.runner = runner
        self.started_at = clock.now()  # reset after the NTP wait in start()
        self.ntp_synced: Optional[bool] = None
        self.panel = Panel(log, driver_factory=panel_factory, monotonic=clock.monotonic)
        self.wifi = WifiWatch(log, runner=runner, connector=connector, route_path=route_path,
                              sleeper=clock.sleep, monotonic=clock.monotonic)
        self.machine = Machine(db, log, network=self.wifi, runner=runner,
                               sources=sources or Sources(data_dir=str(config.paths.database.parent)))
        self.frames = FrameBuilder(db, log, config)
        self.hourly = Hourly(db, log)
        self.nightly = Nightly(db, log, config, heartbeat=self.notifier.heartbeat)
        self.watch = CollectorWatch(log, spawner=spawner)
        self.commands = CommandRunner(db, log, spawner=spawner, monotonic=clock.monotonic)
        self.weather_doc: Optional[Dict[str, Any]] = None
        self.weather_state: Dict[str, Any] = {"ok": None, "fetched_at": None, "error": None,
                                              "pressure_hpa": None, "fetches": 0, "failures": 0}
        self._weather_failing = False
        self.weather_task: Optional[Task] = None
        self.frame_count = 0

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        self.log.start_line(self.config)
        failed = self.db.fail_running(APP, "manager restarted")
        if failed:
            self.log.info("app", "stale_commands_failed", count=failed)
        # No RTC: wait for NTP like the collector does (Q136), so the first frame,
        # the weather blocks and the vitals carry the right time the first time.
        self.ntp_synced = clock.wait_for_ntp(runner=self.runner)
        if not self.ntp_synced:
            self.log.event("warning", "app", "clock_unsynced",
                           "system time not confirmed by NTP; painting anyway")
        self.started_at = clock.now()
        stored = self.db.get_state("last_weather")
        if stored and isinstance(stored.get("value"), dict) and stored["value"].get("hourly"):
            self.weather_doc = stored["value"]  # the first frame uses the stored forecast
            self.weather_state.update(ok=None, fetched_at=self.weather_doc.get("fetched_at"),
                                      pressure_hpa=self.weather_doc.get("pressure_hpa"))
        self.log.event("info", "app", "started", "manager started",
                       stored_weather=self.weather_doc is not None, ntp_synced=self.ntp_synced)
        self.publish_status()

    def stop(self, reason: str) -> None:
        self.log.event("info", "app", "shutdown", f"manager stopping: {reason}", reason=reason)
        try:
            self.publish_status()
        except Exception:
            pass
        self.panel.sleep()
        self.panel.close()

    def tasks(self):
        self.weather_task = Task("weather", weather_mod.WEATHER_EVERY, self.fetch_weather)
        return [
            self.weather_task,  # before the first frame, so it has a forecast
            Task("minute", MINUTE, self.minute, aligned=True, initial_delay=FIRST_FRAME_DELAY),
            Task("commands", COMMAND_POLL, self.process_commands),
            Task("status", STATUS_EVERY, self.publish_status),
            Task("probes", PROBE_EVERY, self.probes),
            Task("machine", MACHINE_EVERY, self.machine_tick),
            Task("hourly", 60, self.hourly_tick, aligned=True),
            Task("nightly", 60, self.nightly_tick, first_run_immediately=False),
            Task("unclaimed", UNCLAIMED_EVERY, self.unclaimed, first_run_immediately=False),
        ]

    # --- the jobs ----------------------------------------------------------------------------

    def minute(self) -> None:
        now = clock.now()
        doc = self.frames.build(now, self.weather_doc, self.wifi.glyph(), self.machine.glyph())
        self.db.set_state("display_data", doc)
        started = time.perf_counter()
        image, _painted = render(doc, now=now)
        self.panel.render_ms = round((time.perf_counter() - started) * 1000, 1)
        mode = self.panel.show(image, now)
        self.frame_count += 1
        debug_frame_line(self.log, doc, mode, self.panel.render_ms, self.panel.busy_ms)
        self.watch.tick(now, self.db.latest_raw_at())

    def fetch_weather(self) -> None:
        started = time.perf_counter()
        now = clock.now()
        try:
            doc = weather_mod.fetch(self.config, opener=self.opener, now=now)
        except weather_mod.WeatherError as exc:
            ms = (time.perf_counter() - started) * 1000
            self.weather_state.update(ok=False, error=str(exc), failures=self.weather_state["failures"] + 1)
            debug_weather_line(self.log, False, ms, error=str(exc))
            if not self._weather_failing:
                self.log.event("warning", "weather", "weather_error", f"forecast fetch failed: {exc}",
                               error=str(exc))
                self._weather_failing = True
            if self.weather_task is not None:
                self.weather_task.retry_in(weather_mod.WEATHER_RETRY)
            return
        ms = (time.perf_counter() - started) * 1000
        self.weather_doc = doc
        self.db.set_state("last_weather", doc)
        self.weather_state.update(ok=True, error=None, fetched_at=doc["fetched_at"],
                                  pressure_hpa=doc.get("pressure_hpa"), fetches=self.weather_state["fetches"] + 1)
        self._weather_failing = False
        debug_weather_line(self.log, True, ms, doc)

    def process_commands(self) -> None:
        self.commands.process(clock.now())

    def probes(self) -> None:
        self.wifi.tick(clock.now())

    def machine_tick(self) -> None:
        self.machine.tick(clock.now())

    def hourly_tick(self) -> None:
        self.hourly.tick(clock.now())

    def nightly_tick(self) -> None:
        self.nightly.tick(clock.now())

    def unclaimed(self) -> None:
        fail_unclaimed(self.db, self.log, clock.now())

    def publish_status(self) -> None:
        now = clock.now()
        self.db.set_state("manager_status", build_status(
            started_at=self.started_at, now=now, log_failures=self.log.failures, panel=self.panel,
            weather_state=self.weather_state, wifi=self.wifi, machine=self.machine, db=self.db,
            nightly=self.nightly, hourly=self.hourly, collector_state=self.frames.collector_state(now)))


def run(config, db: Database, log: Log, notifier: Optional[SystemdNotifier] = None,
        max_passes: Optional[int] = None, extra_tasks=None, **kwargs) -> str:
    """Build a Manager and run its loop. ``extra_tasks`` is for tests and the demo."""
    manager = Manager(config, db, log, notifier=notifier, **kwargs)
    manager.start()
    loop = Loop(log, notifier, manager.tasks() + list(extra_tasks or []))
    loop.install_signal_handlers()
    reason = "error"
    try:
        reason = loop.run(max_passes=max_passes)
    except BaseException as exc:
        reason = f"fatal: {exc!r}"
        raise
    finally:
        manager.stop(reason)
    return reason


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="manager")
    parser.add_argument("--fake", action="store_true", help="fake panel and system tools (dev server / demo)")
    parser.add_argument("--config", default=None, help="path to config.toml (default: repo root)")
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    db = Database(config.paths.database, now=clock.now)
    log = Log(APP, config, db=db)
    kwargs: Dict[str, Any] = {}
    if args.fake:
        from tests.mocks.fake_hardware import install
        install()
        from tests.mocks.fake_devices import FakeRunner, PngPanelDriver
        out_dir = config.paths.database.parent / "demo"
        kwargs["panel_factory"] = lambda: PngPanelDriver(out_dir)
        runner = FakeRunner()
        runner.results["vcgencmd"] = FakeRunner.Completed(stdout="throttled=0x0\n")
        runner.results["iw"] = FakeRunner.Completed(stdout="\ttx bitrate: 43.3 MBit/s\n")
        runner.results["sudo"] = FakeRunner.Completed()
        runner.results["timedatectl"] = FakeRunner.Completed(stdout="yes\n")
        kwargs["runner"] = runner
        kwargs["spawner"] = lambda argv, **_k: log.info("app", "fake_spawn", argv=" ".join(argv))
        import contextlib
        kwargs["connector"] = lambda _address, timeout: contextlib.nullcontext()  # every probe answers
    try:
        run(config, db, log, SystemdNotifier(), **kwargs)
    except Exception:
        log.exception("app", "fatal")
        return 1
    finally:
        log.close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
