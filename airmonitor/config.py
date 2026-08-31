"""Application configuration.

Every setting has a sensible default and can be overridden with an
environment variable (see the `env` column below / README.md).
"""

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None else value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # How often things happen (all in seconds)
    sample_interval: int = 10           # read sensors
    partial_update_interval: int = 60   # quick e-paper refresh
    full_update_interval: int = 300     # deep e-paper refresh (removes ghosting)
    weather_update_interval: int = 1800  # fetch forecast
    command_poll_interval: int = 2      # look for dashboard commands
    network_check_interval: int = 30    # Wi-Fi / internet probe
    status_publish_interval: int = 30   # write collector status for the dashboard

    # Files and paths
    database_path: str = "data/airmonitor.db"
    log_file: str = "data/logs/collector.log"
    font_path: str = "assets/fonts/dejavu-sans-bold.ttf"

    # Display
    display_rotation: int = 90

    # Weather (Open-Meteo) location
    weather_latitude: float = 49.842957
    weather_longitude: float = 24.031111

    # Network probe
    wifi_interface: str = "wlan0"
    connectivity_host: str = "1.1.1.1"
    connectivity_port: int = 53
    connectivity_timeout: int = 3
    # Every Nth consecutive failed probe triggers a recovery action
    # (bounce interface, then restart networking). 0 disables recovery.
    wifi_recovery_after_failures: int = 6

    # SCD41 (CO2)
    scd41_asc_enabled: bool = False      # automatic self-calibration
    min_valid_co2_ppm: int = 350         # readings below this are sensor glitches
    # Sensor altitude for pressure compensation; the SCD41's CO2 math is
    # measurably off without it. Default: Lviv (~296 m).
    scd41_altitude_m: int = 296

    # SHT41 mounting correction: added to every reading. Set negative if the
    # sensor sits close enough to the Pi to pick up its self-heating.
    sht41_temp_offset: float = 0.0
    # After this many invalid readings in a row the sensor is re-initialized.
    # 30 readings x 10s sample interval = 5 minutes of bad data.
    scd41_reinit_after_invalid: int = 30

    # With ASC off (the default here — see README "Sensor care"), forced
    # recalibration is the only drift correction the SCD41 gets. Warn once
    # per boot when the last one is older than this. 0 disables the reminder.
    scd41_calibration_reminder_days: int = 180

    # SCD41 forced-calibration safety limits
    calibration_min_runtime: int = 180       # sensor must run this long first
    calibration_window: int = 300            # recent samples considered "recent"
    calibration_min_samples: int = 3
    calibration_max_drift_ppm: int = 30      # readings must be stable
    calibration_max_reference_delta_ppm: int = 200

    # SPS30 (particulate matter)
    sps30_manual_clean_cooldown: int = 1800  # min seconds between fan cleanings

    # Data retention (0 disables pruning)
    keep_measurements_days: int = 90
    keep_events_days: int = 14

    # A measurement older than this is shown as missing/stale
    measurement_max_age: int = 45

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            sample_interval=_env_int("AIRMONITOR_SAMPLE_INTERVAL", cls.sample_interval),
            partial_update_interval=_env_int("AIRMONITOR_PARTIAL_UPDATE_INTERVAL", cls.partial_update_interval),
            full_update_interval=_env_int("AIRMONITOR_FULL_UPDATE_INTERVAL", cls.full_update_interval),
            weather_update_interval=_env_int("AIRMONITOR_WEATHER_UPDATE_INTERVAL", cls.weather_update_interval),
            command_poll_interval=_env_int("AIRMONITOR_COMMAND_POLL_INTERVAL", cls.command_poll_interval),
            network_check_interval=_env_int("AIRMONITOR_CONNECTIVITY_CHECK_INTERVAL", cls.network_check_interval),
            status_publish_interval=_env_int("AIRMONITOR_STATUS_PUBLISH_INTERVAL", cls.status_publish_interval),
            database_path=_env_str("AIRMONITOR_DATABASE_PATH", cls.database_path),
            log_file=_env_str("AIRMONITOR_LOG_FILE", cls.log_file),
            font_path=_env_str("AIRMONITOR_FONT_PATH", cls.font_path),
            display_rotation=_env_int("AIRMONITOR_DISPLAY_ROTATION", cls.display_rotation),
            weather_latitude=_env_float("AIRMONITOR_WEATHER_LAT", cls.weather_latitude),
            weather_longitude=_env_float("AIRMONITOR_WEATHER_LON", cls.weather_longitude),
            wifi_interface=_env_str("AIRMONITOR_WIFI_INTERFACE", cls.wifi_interface),
            connectivity_host=_env_str("AIRMONITOR_CONNECTIVITY_TARGET_HOST", cls.connectivity_host),
            connectivity_port=_env_int("AIRMONITOR_CONNECTIVITY_TARGET_PORT", cls.connectivity_port),
            connectivity_timeout=_env_int("AIRMONITOR_CONNECTIVITY_TIMEOUT", cls.connectivity_timeout),
            wifi_recovery_after_failures=_env_int(
                "AIRMONITOR_WIFI_RECOVERY_AFTER_FAILURES", cls.wifi_recovery_after_failures
            ),
            scd41_asc_enabled=_env_bool("AIRMONITOR_SCD41_ASC_ENABLED", cls.scd41_asc_enabled),
            scd41_altitude_m=_env_int("AIRMONITOR_SCD41_ALTITUDE_M", cls.scd41_altitude_m),
            sht41_temp_offset=_env_float("AIRMONITOR_SHT41_TEMP_OFFSET", cls.sht41_temp_offset),
            min_valid_co2_ppm=_env_int("AIRMONITOR_MIN_VALID_CO2_PPM", cls.min_valid_co2_ppm),
            scd41_reinit_after_invalid=_env_int("AIRMONITOR_SCD41_REINIT_AFTER_INVALID", cls.scd41_reinit_after_invalid),
            scd41_calibration_reminder_days=_env_int(
                "AIRMONITOR_SCD41_CALIBRATION_REMINDER_DAYS", cls.scd41_calibration_reminder_days
            ),
            calibration_min_runtime=_env_int("AIRMONITOR_SCD41_CALIBRATION_MIN_RUNTIME", cls.calibration_min_runtime),
            calibration_window=_env_int("AIRMONITOR_SCD41_CALIBRATION_WINDOW", cls.calibration_window),
            calibration_min_samples=_env_int("AIRMONITOR_SCD41_CALIBRATION_MIN_SAMPLES", cls.calibration_min_samples),
            calibration_max_drift_ppm=_env_int("AIRMONITOR_SCD41_CALIBRATION_MAX_DRIFT", cls.calibration_max_drift_ppm),
            calibration_max_reference_delta_ppm=_env_int(
                "AIRMONITOR_SCD41_CALIBRATION_MAX_REFERENCE_DELTA", cls.calibration_max_reference_delta_ppm
            ),
            sps30_manual_clean_cooldown=_env_int(
                "AIRMONITOR_SPS30_MIN_SECONDS_BETWEEN_MANUAL_CLEANS", cls.sps30_manual_clean_cooldown
            ),
            keep_measurements_days=_env_int("AIRMONITOR_KEEP_MEASUREMENTS_DAYS", cls.keep_measurements_days),
            keep_events_days=_env_int("AIRMONITOR_KEEP_EVENTS_DAYS", cls.keep_events_days),
            measurement_max_age=_env_int("AIRMONITOR_MEASUREMENT_MAX_AGE", cls.measurement_max_age),
        )

    def validate(self) -> None:
        must_be_positive = {
            "AIRMONITOR_SAMPLE_INTERVAL": self.sample_interval,
            "AIRMONITOR_PARTIAL_UPDATE_INTERVAL": self.partial_update_interval,
            "AIRMONITOR_FULL_UPDATE_INTERVAL": self.full_update_interval,
            "AIRMONITOR_WEATHER_UPDATE_INTERVAL": self.weather_update_interval,
            "AIRMONITOR_COMMAND_POLL_INTERVAL": self.command_poll_interval,
            "AIRMONITOR_CONNECTIVITY_CHECK_INTERVAL": self.network_check_interval,
            "AIRMONITOR_STATUS_PUBLISH_INTERVAL": self.status_publish_interval,
            "AIRMONITOR_CONNECTIVITY_TIMEOUT": self.connectivity_timeout,
            "AIRMONITOR_MEASUREMENT_MAX_AGE": self.measurement_max_age,
        }
        for name, value in must_be_positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than 0")
        if self.full_update_interval < self.partial_update_interval:
            raise ValueError(
                "AIRMONITOR_FULL_UPDATE_INTERVAL must be >= AIRMONITOR_PARTIAL_UPDATE_INTERVAL"
            )
