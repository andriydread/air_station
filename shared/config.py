"""Loads ``config.toml`` — the only settings file, read once at start.

No environment variables, no defaults hidden in code: every key must be in the
file, and every value is range-checked so a typo fails at start with the key
name, not hours later on the Pi.
"""

from dataclasses import dataclass, asdict
from pathlib import Path
import tomllib
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "config.toml"

LEVELS = ("debug", "info", "warning")
BLOCK_HOURS = (1, 2, 3, 4, 6)


class ConfigError(ValueError):
    """A missing key, a wrong type or a value outside its allowed range."""


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    altitude_m: int


@dataclass(frozen=True)
class Sensors:
    scd41_temp_offset_c: float
    sht41_temp_offset_c: float
    asc: bool
    calibration_target_ppm: int


@dataclass(frozen=True)
class Retention:
    raw: int
    vitals: int
    events: int
    commands: int
    logs: int


@dataclass(frozen=True)
class Weather:
    block_hours: int


@dataclass(frozen=True)
class Dashboard:
    port: int


@dataclass(frozen=True)
class Paths:
    database: Path
    logs: Path


@dataclass(frozen=True)
class Logging:
    level: str
    i2c_trace: bool  # every I2C transaction as a debug line (collector/i2c_trace.py)


@dataclass(frozen=True)
class Config:
    location: Location
    sensors: Sensors
    retention_days: Retention
    weather: Weather
    dashboard: Dashboard
    paths: Paths
    logging: Logging
    repo_root: Path
    source: Path

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        source = Path(path).resolve() if path else DEFAULT_PATH
        try:
            with open(source, "rb") as handle:
                raw = tomllib.load(handle)
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {source}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{source}: {exc}") from None
        return cls.from_dict(raw, repo_root=source.parent, source=source)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], repo_root: Path, source: Path = DEFAULT_PATH) -> "Config":
        section = _Section(raw)
        location = Location(
            latitude=section.number("location", "latitude", -90, 90),
            longitude=section.number("location", "longitude", -180, 180),
            altitude_m=section.integer("location", "altitude_m", -500, 9000),
        )
        sensors = Sensors(
            scd41_temp_offset_c=section.number("sensors", "scd41_temp_offset_c", -20, 20),
            sht41_temp_offset_c=section.number("sensors", "sht41_temp_offset_c", -20, 20),
            asc=section.boolean("sensors", "asc"),
            calibration_target_ppm=section.integer("sensors", "calibration_target_ppm", 400, 2000),
        )
        retention = Retention(
            raw=section.integer("retention_days", "raw", 1, 3650),
            vitals=section.integer("retention_days", "vitals", 1, 3650),
            events=section.integer("retention_days", "events", 1, 3650),
            commands=section.integer("retention_days", "commands", 1, 3650),
            logs=section.integer("retention_days", "logs", 1, 3650),
        )
        block_hours = section.integer("weather", "block_hours", 1, 6)
        if block_hours not in BLOCK_HOURS:
            raise ConfigError(f"weather.block_hours must be one of {BLOCK_HOURS}, got {block_hours}")
        weather = Weather(block_hours=block_hours)
        dashboard = Dashboard(port=section.integer("dashboard", "port", 1, 65535))
        paths = Paths(
            database=_resolve(repo_root, section.text("paths", "database")),
            logs=_resolve(repo_root, section.text("paths", "logs")),
        )
        level = section.text("logging", "level")
        if level not in LEVELS:
            raise ConfigError(f"logging.level must be one of {LEVELS}, got {level!r}")
        return cls(
            location=location,
            sensors=sensors,
            retention_days=retention,
            weather=weather,
            dashboard=dashboard,
            paths=paths,
            logging=Logging(level=level, i2c_trace=section.boolean("logging", "i2c_trace")),
            repo_root=repo_root,
            source=source,
        )

    def as_dict(self) -> Dict[str, Any]:
        """Plain dict (paths as strings) for the start line and the status tool."""
        data = asdict(self)
        data["paths"] = {"database": str(self.paths.database), "logs": str(self.paths.logs)}
        data["repo_root"] = str(self.repo_root)
        data["source"] = str(self.source)
        return data


def _resolve(repo_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (repo_root / path).resolve()


class _Section:
    """Typed, range-checked access to the parsed TOML with key names in errors."""

    def __init__(self, raw: Dict[str, Any]):
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a table")
        self.raw = raw

    def _get(self, section: str, key: str) -> Any:
        table = self.raw.get(section)
        if not isinstance(table, dict):
            raise ConfigError(f"missing section [{section}]")
        if key not in table:
            raise ConfigError(f"missing key {section}.{key}")
        return table[key]

    def number(self, section: str, key: str, low: float, high: float) -> float:
        value = self._get(section, key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{section}.{key} must be a number, got {value!r}")
        if not low <= value <= high:
            raise ConfigError(f"{section}.{key} must be between {low} and {high}, got {value}")
        return float(value)

    def integer(self, section: str, key: str, low: int, high: int) -> int:
        value = self._get(section, key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{section}.{key} must be a whole number, got {value!r}")
        if not low <= value <= high:
            raise ConfigError(f"{section}.{key} must be between {low} and {high}, got {value}")
        return value

    def boolean(self, section: str, key: str) -> bool:
        value = self._get(section, key)
        if not isinstance(value, bool):
            raise ConfigError(f"{section}.{key} must be true or false, got {value!r}")
        return value

    def text(self, section: str, key: str) -> str:
        value = self._get(section, key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{section}.{key} must be a non-empty string, got {value!r}")
        return value
