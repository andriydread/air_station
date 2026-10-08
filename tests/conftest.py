"""Install fake hardware modules before any app import, so the apps load off the Pi."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.mocks.fake_hardware import install as _install_fake_hardware  # noqa: E402

_install_fake_hardware()


import pytest  # noqa: E402  (after the fakes exist)


@pytest.fixture
def tmp_config(tmp_path):
    """The shipped config with database and logs under tmp_path."""
    import tomllib

    from shared.config import DEFAULT_PATH, Config

    with open(DEFAULT_PATH, "rb") as handle:
        raw = tomllib.load(handle)
    raw["paths"]["database"] = str(tmp_path / "data" / "airstation.db")
    raw["paths"]["logs"] = str(tmp_path / "data" / "logs")
    raw["logging"]["level"] = "debug"
    return Config.from_dict(raw, repo_root=REPO_ROOT, source=tmp_path / "config.toml")


@pytest.fixture
def db(tmp_config):
    from shared.db import Database

    database = Database(tmp_config.paths.database)
    yield database
    database.close()


@pytest.fixture
def log(tmp_config, db):
    """Strict collector logger that writes events into the test database."""
    from shared.events import Log

    logger = Log("collector", tmp_config, db=db, strict=True)
    yield logger
    logger.close()


@pytest.fixture
def fake_clock(monkeypatch):
    from shared import clock
    from tests.mocks.fake_devices import FakeClock

    fake = FakeClock(start=1_788_436_800.0)  # 2026-09-03 12:00:00 UTC
    monkeypatch.setattr(clock, "now", fake.now)
    monkeypatch.setattr(clock, "monotonic", fake.monotonic)
    monkeypatch.setattr(clock, "sleep", fake.sleep)
    return fake


@pytest.fixture(autouse=True)
def _reset_fake_gpio():
    import RPi.GPIO as gpio

    gpio.pin_values.clear()
    gpio.outputs.clear()
    yield
    gpio.pin_values.clear()
    gpio.outputs.clear()
