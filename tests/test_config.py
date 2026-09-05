"""config.toml loading and validation."""

import copy
import tomllib
from pathlib import Path

import pytest

from shared.config import DEFAULT_PATH, Config, ConfigError

REPO = Path(__file__).resolve().parents[1]


def _raw():
    with open(DEFAULT_PATH, "rb") as handle:
        return tomllib.load(handle)


def _with(section, key, value):
    raw = copy.deepcopy(_raw())
    raw[section][key] = value
    return raw


def test_shipped_file_loads_with_the_carried_over_values():
    config = Config.load()
    assert config.location.latitude == pytest.approx(49.842957)
    assert config.location.longitude == pytest.approx(24.031111)
    assert config.location.altitude_m == 296
    assert config.sensors.scd41_temp_offset_c == 4.0
    assert config.sensors.sht41_temp_offset_c == 0.0
    assert config.sensors.asc is False
    assert config.sensors.calibration_target_ppm == 420
    assert (config.retention_days.raw, config.retention_days.vitals, config.retention_days.events,
            config.retention_days.commands, config.retention_days.logs) == (90, 30, 30, 30, 45)
    assert config.weather.block_hours == 3
    assert config.dashboard.port == 8080
    assert config.logging.level == "debug" and config.logging.i2c_trace is True


def test_relative_paths_resolve_against_the_repo_root(tmp_path):
    config = Config.load()
    assert config.paths.database == REPO / "data" / "airstation.db"
    assert config.paths.logs == REPO / "data" / "logs"
    absolute = Config.from_dict(_with("paths", "database", str(tmp_path / "x.db")), repo_root=REPO)
    assert absolute.paths.database == tmp_path / "x.db"


def test_explicit_path_sets_repo_root_to_its_directory(tmp_path):
    copy_path = tmp_path / "config.toml"
    copy_path.write_bytes(DEFAULT_PATH.read_bytes())
    config = Config.load(str(copy_path))
    assert config.repo_root == tmp_path
    assert config.paths.database == tmp_path / "data" / "airstation.db"


def test_missing_file_and_bad_toml(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        Config.load(str(tmp_path / "nope.toml"))
    bad = tmp_path / "bad.toml"
    bad.write_text("[location\nlatitude = 1")
    with pytest.raises(ConfigError):
        Config.load(str(bad))


def test_missing_section_and_key_are_named():
    raw = _raw()
    del raw["weather"]
    with pytest.raises(ConfigError, match=r"missing section \[weather\]"):
        Config.from_dict(raw, repo_root=REPO)
    raw = _raw()
    del raw["sensors"]["asc"]
    with pytest.raises(ConfigError, match="missing key sensors.asc"):
        Config.from_dict(raw, repo_root=REPO)


@pytest.mark.parametrize(
    "section, key, value, message",
    [
        ("location", "latitude", 91, "location.latitude"),
        ("location", "longitude", -181, "location.longitude"),
        ("location", "altitude_m", 9001, "location.altitude_m"),
        ("location", "altitude_m", 296.5, "whole number"),
        ("sensors", "asc", "no", "true or false"),
        ("sensors", "calibration_target_ppm", 399, "calibration_target_ppm"),
        ("retention_days", "raw", 0, "retention_days.raw"),
        ("retention_days", "logs", True, "whole number"),
        ("weather", "block_hours", 5, "one of"),
        ("dashboard", "port", 70000, "dashboard.port"),
        ("paths", "database", "", "non-empty string"),
        ("logging", "level", "verbose", "logging.level"),
        ("logging", "i2c_trace", "yes", "logging.i2c_trace"),
    ],
)
def test_range_and_type_rules(section, key, value, message):
    with pytest.raises(ConfigError, match=message):
        Config.from_dict(_with(section, key, value), repo_root=REPO)


def test_environment_variables_are_ignored(monkeypatch):
    monkeypatch.setenv("AIRMONITOR_WEB_PORT", "9999")
    monkeypatch.setenv("AIRSTATION_PORT", "9999")
    assert Config.load().dashboard.port == 8080


def test_as_dict_is_plain_json_material():
    data = Config.load().as_dict()
    assert data["paths"]["database"].endswith("airstation.db")
    assert data["retention_days"]["logs"] == 45
    assert isinstance(data["repo_root"], str)
