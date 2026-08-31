"""Air monitor collector.

Reads the sensors on a schedule, stores history in SQLite, draws the
e-paper display, and executes commands queued by the web dashboard.

The flow:

    main() -> AirMonitor.run() -> a loop of small periodic tasks
        collect_sample     every 10s   read sensors, store to SQLite
        update_display     every 60s   queue an e-paper refresh (full every 5 min)
        process_commands   every 2s    commands from the dashboard
        publish_status     every 30s   status documents for the dashboard
        power check        every 60s   vcgencmd throttle/undervoltage flags
        prune_database     every 24h   delete old history rows

Slow subsystems run on their own worker threads so they can never stall
sampling (airmonitor/workers.py):

    display worker     renders queued frames (a full refresh blocks ~15s)
    weather worker     every 30min Open-Meteo forecast
    network worker     every 30s   Wi-Fi probe + recovery ladder
"""

import logging
import signal
import sys
import time
from typing import Any, Dict, List, Optional

import board
import busio
import requests

from airmonitor.commands import CommandProcessor
from airmonitor.config import Config
from airmonitor.logging_utils import EventLog, configure_logging
from airmonitor.network import probe_network
from airmonitor.power import PowerMonitor
from airmonitor.quality import CrossCheck, RateGuard
from airmonitor.sensors import (
    ReinitBackoff,
    Scd41,
    SensorHealth,
    Sht41,
    Sps30,
    utc_now_iso,
)
from airmonitor.storage import AirMonitorDatabase
from airmonitor.watchdog import SystemdNotifier
from airmonitor.wifi_recovery import WifiRecovery
from airmonitor.workers import DisplayWorker, PeriodicWorker
from lib.uc8253c import UC8253C_SPI
from utils.display import create_display_image
from utils.weather import get_weather_forecast

LOGGER = logging.getLogger("airmonitor")

METRICS = ("co2", "temp", "humid", "pm1", "pm25", "pm4", "pm10", "tps")


class PeriodicTask:
    """Runs a function at a fixed interval; one failure never kills the loop."""

    def __init__(self, name: str, interval_seconds: int, func):
        self.name = name
        self.interval = interval_seconds
        self.func = func
        self.next_run = time.monotonic()

    def run_if_due(self, now: float, events: EventLog) -> None:
        if now < self.next_run:
            return
        try:
            self.func()
        except Exception as exc:
            events.log(
                logging.ERROR, self.name, "task_failed", f"{self.name} task failed: {exc}"
            )
            LOGGER.exception("%s task failed", self.name)
        while self.next_run <= now:
            self.next_run += self.interval


class LatestReadings:
    """Remembers the newest value of every metric and how old it is."""

    def __init__(self, max_age_seconds: int, events: EventLog):
        self.max_age = max_age_seconds
        self.events = events
        self.values: Dict[str, Any] = {}       # metric -> value
        self.seen_monotonic: Dict[str, float] = {}
        self.seen_iso: Dict[str, str] = {}
        self.stale_reported: Dict[str, bool] = {}

    def record(self, metric: str, value: float) -> None:
        if self.stale_reported.get(metric):
            self.events.log(
                logging.INFO, metric, "measurement_recovered", f"{metric} measurements resumed"
            )
        self.values[metric] = value
        self.seen_monotonic[metric] = time.monotonic()
        self.seen_iso[metric] = utc_now_iso()
        self.stale_reported[metric] = False

    def report_stale(self, metric: str, source: str) -> None:
        """Log once when a metric stops updating."""
        seen = self.seen_monotonic.get(metric)
        if seen is None or self.stale_reported.get(metric):
            return
        age = time.monotonic() - seen
        if age <= self.max_age:
            return
        self.stale_reported[metric] = True
        self.events.log(
            logging.WARNING, source, "measurement_stale",
            f"{metric} measurement is stale after {int(age)}s",
            {"metric": metric, "age_seconds": int(age), "last_value": self.values.get(metric)},
        )

    def fresh_snapshot(self) -> Dict[str, Any]:
        """Current values with anything older than max_age replaced by None.

        Also carries per-metric ages (seconds since last accepted reading)
        so the dashboard can show exactly how fresh every number is.
        """
        now = time.monotonic()
        snapshot: Dict[str, Any] = {}
        ages: Dict[str, Optional[int]] = {}
        newest_iso = None
        newest_monotonic = None
        for metric in METRICS:
            seen = self.seen_monotonic.get(metric)
            ages[metric] = int(now - seen) if seen is not None else None
            if seen is None or now - seen > self.max_age:
                snapshot[metric] = None
                continue
            snapshot[metric] = self.values[metric]
            if newest_monotonic is None or seen > newest_monotonic:
                newest_monotonic = seen
                newest_iso = self.seen_iso[metric]
        snapshot["timestamp"] = newest_iso
        snapshot["ages"] = ages
        return snapshot


class SampleBuffer:
    """Collects samples between display refreshes and averages them."""

    def __init__(self):
        self.samples: Dict[str, List[float]] = {metric: [] for metric in METRICS}

    def add(self, metric: str, value: float) -> None:
        self.samples[metric].append(value)

    def take_averages(self) -> Dict[str, Optional[float]]:
        """Return the averaged values and start a new averaging window."""
        averages: Dict[str, Any] = {}
        for metric, values in self.samples.items():
            if not values:
                averages[metric] = None
                continue
            average = sum(values) / len(values)
            if metric == "co2":
                averages[metric] = int(round(average))
            elif metric in ("temp", "humid", "tps"):
                averages[metric] = round(average, 1)
            else:
                averages[metric] = round(average, 2)
            values.clear()
        return averages


class AirMonitor:
    def __init__(self, config: Config):
        self.config = config
        self.database = AirMonitorDatabase(
            config.database_path, min_valid_co2_ppm=config.min_valid_co2_ppm
        )
        self.events = EventLog(LOGGER, self.database)
        self.readings = LatestReadings(config.measurement_max_age, self.events)
        self.buffer = SampleBuffer()
        self.commands = CommandProcessor(self)
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "AirMonitor/1.0"})

        self.i2c = None
        self.scd41: Optional[Scd41] = None
        self.sht41: Optional[Sht41] = None
        self.sps30: Optional[Sps30] = None
        self.display: Optional[UC8253C_SPI] = None

        self.i2c_health = SensorHealth("i2c", self.events)
        self._i2c_backoff = ReinitBackoff()
        self.display_health = SensorHealth("display", self.events)
        self._display_backoff = ReinitBackoff()
        self.display_worker = DisplayWorker(self._render, self.events)
        self._workers: List[PeriodicWorker] = []
        self.weather_health = SensorHealth("weather", self.events)
        self.network_state: Dict[str, Any] = {"interface": config.wifi_interface}
        self.rate_guard = RateGuard(self.events)
        self.cross_check = CrossCheck(self.events)
        self.power = PowerMonitor(self.events)
        self.wifi_recovery = WifiRecovery(
            config.wifi_interface, self.events,
            after_failures=config.wifi_recovery_after_failures,
        )

        self.weather: Dict[str, Any] = {}
        self.last_display_snapshot: Optional[Dict[str, Any]] = None
        self._last_display_write = None  # (mode, snapshot minus timestamp)
        self.running = True
        self.started_at = utc_now_iso()
        self.started_monotonic = time.monotonic()
        self.notifier = SystemdNotifier()

    # --- Setup and teardown -------------------------------------------------

    def setup(self) -> None:
        self._init_i2c_and_sensors()
        self._ensure_display()
        self.check_network()
        self.publish_status()
        time.sleep(5)  # let the sensors produce their first measurement

    def install_signal_handlers(self) -> None:
        def stop(signum, _frame):
            LOGGER.info("Received signal %s, stopping", signum)
            self.running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

    def shutdown(self) -> None:
        self.notifier.stopping()
        self.events.log(logging.INFO, "collector", "shutdown", "Shutting down hardware")
        self.running = False
        self.publish_status()
        for worker in self._workers:
            worker.stop()
        self.display_worker.stop()  # before display.close(): no render mid-close
        if self.scd41 is not None:
            self.scd41.stop()
        if self.sps30 is not None:
            self.sps30.stop()
        if self.display is not None:
            try:
                self.display.close()
            except Exception:
                LOGGER.exception("Failed to close display")
        self.http.close()
        self.publish_status()
        self.database.close()
        self.notifier.close()

    # --- Hardware recovery ----------------------------------------------------

    def _init_i2c_and_sensors(self) -> None:
        LOGGER.info("Initializing I2C bus")
        try:
            self.i2c = busio.I2C(board.SCL, board.SDA)
            self.i2c_health.ok()
            self._i2c_backoff.reset()
        except Exception as exc:
            LOGGER.exception("Failed to initialize I2C bus")
            self.i2c = None
            self.i2c_health.failed(str(exc), available=False)
            self._i2c_backoff.failed()
            return

        LOGGER.info("Initializing sensors")
        self.scd41 = Scd41(self.i2c, self.config, self.events)
        self.sht41 = Sht41(self.i2c, self.events, temp_offset=self.config.sht41_temp_offset)
        self.sps30 = Sps30(self.i2c, self.config, self.events)

    def _ensure_display(self) -> None:
        """(Re-)create the display with backoff.

        Runs on the main thread during setup, then on the display worker
        thread before each render — a display that failed at boot comes
        back on its own instead of staying dead until a restart.
        """
        if self.display is not None:
            return
        if not self._display_backoff.due():
            return
        LOGGER.info("Initializing UC8253C display")
        try:
            display = UC8253C_SPI(rotation=self.config.display_rotation)
            display.clear()
            self.display = display
            self.display_health.ok()
            self._display_backoff.reset()
        except Exception as exc:
            LOGGER.exception("Failed to initialize display")
            self.display = None
            self.display_health.failed(str(exc), available=False)
            self._display_backoff.failed()

    def ensure_hardware(self) -> None:
        """Bring back anything that failed to initialize, with backoff.

        Before this existed, one transient I2C glitch at boot left a sensor
        (or the whole bus) dead until someone restarted the service.
        """
        if self.i2c is None:
            if self._i2c_backoff.due():
                self._init_i2c_and_sensors()
            return
        for sensor in (self.scd41, self.sht41, self.sps30):
            if sensor is not None:
                sensor.ensure()

    # --- Periodic tasks -----------------------------------------------------

    def collect_sample(self) -> None:
        """Read every sensor once; store whatever came back."""
        self.ensure_hardware()
        sample: Dict[str, Optional[float]] = {}

        if self.scd41 is not None:
            co2 = self.scd41.read()
            if co2 is not None:
                sample["co2"] = co2
            else:
                self.readings.report_stale("co2", "scd41")

        if self.sht41 is not None:
            ambient = self.sht41.read()
            if ambient is not None:
                sample["temp"], sample["humid"] = ambient
                if self.scd41 is not None:
                    self.cross_check.compare(
                        ambient[0], ambient[1],
                        self.scd41.last_temperature, self.scd41.last_humidity,
                    )
            else:
                self.readings.report_stale("temp", "sht41")
                self.readings.report_stale("humid", "sht41")

        if self.sps30 is not None:
            particles = self.sps30.read()
            if particles is not None:
                sample.update(particles)
            else:
                self.readings.report_stale("pm25", "sps30")

        # The rate guard splits the sample: plausible values are recorded,
        # implausible jumps are stored as flags (raw value + reason) so a
        # sensor glitch can't poison averages while still being inspectable.
        accepted, flags = self.rate_guard.filter(sample)
        for metric, value in accepted.items():
            self.readings.record(metric, value)
            self.buffer.add(metric, value)

        if sample:
            self.database.insert_measurement(accepted, flags=flags)
        # Only the live values are refreshed per sample; the full status
        # document is published on its own slower cadence (status task).
        self.database.set_state("latest_measurements", self.readings.fresh_snapshot())

    def update_display(self, full_refresh: bool) -> None:
        """Average the buffered samples and queue an e-paper redraw."""
        snapshot = self.buffer.take_averages()
        snapshot["timestamp"] = utc_now_iso()
        snapshot.update(self.weather)
        self.last_display_snapshot = snapshot
        self.display_worker.submit(snapshot, full_refresh)

    def redraw_display(self, full_refresh: bool) -> None:
        """Redraw the last snapshot (used by dashboard refresh commands)."""
        if self.last_display_snapshot is None:
            self.update_display(full_refresh)
        else:
            self.display_worker.submit(self.last_display_snapshot, full_refresh)

    def _render(self, snapshot: Dict[str, Any], full_refresh: bool) -> None:
        """Actually draw a frame. Runs on the display worker thread."""
        self._ensure_display()
        mode = "full" if full_refresh else "partial"
        # Skip the DB write when only the timestamp moved — with dead sensors
        # or a stable room this is most refreshes.
        comparable = (mode, {k: v for k, v in snapshot.items() if k != "timestamp"})
        if comparable != self._last_display_write:
            self.database.set_state(
                "latest_display_snapshot", {"mode": mode, "snapshot": snapshot}
            )
            self._last_display_write = comparable
        if self.display is None:
            self.display_health.failed("Display unavailable; snapshot stored only", available=False)
            self.publish_status()
            return
        try:
            image = create_display_image(
                self.display.width, self.display.height, snapshot, self.config.font_path
            )
            refresh = UC8253C_SPI.MODE_FULL if full_refresh else UC8253C_SPI.MODE_PARTIAL
            self.display.display_image(image, mode=refresh)
            LOGGER.info("Display updated with %s refresh", mode)
            self.display_health.state["last_refresh_at"] = utc_now_iso()
            self.display_health.ok()
        except Exception as exc:
            LOGGER.exception("Display update failed")
            self.display_health.failed(str(exc))
        self.publish_status()

    def fetch_weather(self) -> None:
        LOGGER.info("Fetching weather forecast")
        forecast = get_weather_forecast(
            self.config.weather_latitude, self.config.weather_longitude, self.http
        )
        if forecast:
            self.weather = forecast
            self.database.set_state("latest_weather", forecast)
            self.weather_health.state["last_success_at"] = utc_now_iso()
            self.weather_health.ok()
        else:
            self.weather_health.failed("Weather fetch failed; using previous forecast")
        self.publish_status()

    def check_network(self) -> None:
        status = probe_network(self.config)
        previous = self.network_state
        self.network_state = {
            **status,
            "last_error": status["error"],
            "last_checked_at": status["checked_at"],
            "last_success_at": (
                status["checked_at"] if status["healthy"] else previous.get("last_success_at")
            ),
        }
        watched = ("available", "healthy", "operstate", "carrier", "error")
        if any(previous.get(key) != status.get(key) for key in watched):
            level = logging.INFO if status["healthy"] else logging.WARNING
            message = (
                f"Wi-Fi check: available={status['available']} healthy={status['healthy']} "
                f"operstate={status['operstate']} carrier={status['carrier']}"
            )
            if status["error"]:
                message += f"; error={status['error']}"
            self.events.log(level, "network", "connectivity_check", message, status)
            self.publish_status()
        self.wifi_recovery.record_probe(status["healthy"])

    def process_commands(self) -> None:
        self.commands.process_pending()

    def prune_database(self) -> None:
        deleted = self.database.prune(
            self.config.keep_measurements_days, self.config.keep_events_days
        )
        if deleted["measurements"] or deleted["events"]:
            self.events.log(
                logging.INFO, "storage", "pruned",
                f"Pruned {deleted['measurements']} measurements and {deleted['events']} events",
                deleted,
            )

    # --- Status shared with the dashboard ------------------------------------

    def publish_status(self) -> None:
        self.database.set_state("collector_status", self._status_payload())
        self.database.set_state("latest_measurements", self.readings.fresh_snapshot())
        self.database.set_state("network_status", self.network_state)

    def _status_payload(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            # Minute granularity so an otherwise-unchanged status document
            # deduplicates in set_state instead of rewriting every publish.
            "uptime_seconds": int(time.monotonic() - self.started_monotonic) // 60 * 60,
            "database_path": self.config.database_path,
            "log_file": self.config.log_file,
            "sample_interval_seconds": self.config.sample_interval,
            "partial_update_interval_seconds": self.config.partial_update_interval,
            "full_update_interval_seconds": self.config.full_update_interval,
            "weather_update_interval_seconds": self.config.weather_update_interval,
            "measurement_max_age_seconds": self.config.measurement_max_age,
            "scd41_asc_enabled": self.scd41.asc_enabled if self.scd41 else None,
            "scd41_min_valid_co2_ppm": self.config.min_valid_co2_ppm,
            "scd41_measurement_runtime_seconds": (
                self.scd41.runtime_seconds() if self.scd41 else None
            ),
            "scd41_recent_valid_samples": (
                len(self.scd41.recent_valid_samples) if self.scd41 else 0
            ),
            "sps30_auto_cleaning_interval_seconds": (
                self.sps30.auto_cleaning_interval if self.sps30 else None
            ),
            "sensors": {
                "i2c": self.i2c_health.state,
                "scd41": self.scd41.health.state if self.scd41 else self._missing("SCD41"),
                "sht41": self.sht41.health.state if self.sht41 else self._missing("SHT41"),
                "sps30": self.sps30.health.state if self.sps30 else self._missing("SPS30"),
                "display": self.display_health.state,
                "weather": self.weather_health.state,
                "network": self.network_state,
                "power": self.power.state,
            },
        }

    @staticmethod
    def _missing(name: str) -> Dict[str, Any]:
        return {"available": False, "healthy": False, "last_error": f"{name} not initialized"}

    # --- Main loop ------------------------------------------------------------

    def run(self) -> None:
        self.config.validate()
        self.install_signal_handlers()
        self.setup()

        tasks = [
            PeriodicTask("collect_sample", self.config.sample_interval, self.collect_sample),
            PeriodicTask("commands", self.config.command_poll_interval, self.process_commands),
            PeriodicTask("status", self.config.status_publish_interval, self.publish_status),
            PeriodicTask("power", 60, self.power.check),
            PeriodicTask("storage_prune", 24 * 3600, self.prune_database),
            PeriodicTask("display", self.config.partial_update_interval, self._display_tick),
            PeriodicTask("display_watch", 30, self.display_worker.check_wedged),
        ]
        self._next_full_refresh = time.monotonic()

        # Blocking subsystems run on their own threads; sampling never waits.
        self._workers = [
            PeriodicWorker(
                "weather", self.config.weather_update_interval, self.fetch_weather, self.events
            ),
            PeriodicWorker(
                "network", self.config.network_check_interval, self.check_network, self.events
            ),
        ]
        self.display_worker.start()
        for worker in self._workers:
            worker.start()

        self.events.log(logging.INFO, "collector", "started", "Air monitor started")
        self.notifier.ready()
        last_heartbeat = 0.0
        try:
            while self.running:
                now = time.monotonic()
                for task in tasks:
                    task.run_if_due(now, self.events)
                # Watchdog heartbeat: the unit's WatchdogSec is 90s, so a
                # ping every 10s survives the slowest full display refresh.
                if now - last_heartbeat >= 10:
                    self.notifier.heartbeat()
                    last_heartbeat = now
                time.sleep(0.2)
        finally:
            self.shutdown()

    def _display_tick(self) -> None:
        """Partial refresh normally; a full refresh every full_update_interval."""
        now = time.monotonic()
        full = now >= self._next_full_refresh
        if full:
            while self._next_full_refresh <= now:
                self._next_full_refresh += self.config.full_update_interval
        self.update_display(full_refresh=full)


def main() -> int:
    config = Config.from_env()
    configure_logging("airmonitor", level=logging.INFO, log_file=config.log_file)
    try:
        AirMonitor(config).run()
        return 0
    except Exception:
        LOGGER.exception("Air monitor terminated with a fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
