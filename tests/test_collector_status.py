"""collector_status shape."""

from collector.status import build_status
from tests.test_sampling import START, Rig


def test_status_shape_is_frozen(db, log, tmp_config, monkeypatch):
    rig = Rig(db, log, tmp_config, monkeypatch)
    rig.ready()
    rig.beat()
    now = rig.clock.now()
    status = build_status(rig.sampler, started_at=now - 100, now=now,
                          log_failures=log.failures, last_calibration={"at": 1, "target_ppm": 420})
    assert set(status) == {"started_at", "uptime", "ready_at", "sample_count", "log_failures",
                           "storage_failures", "bus_reinits", "asc", "pressure_hpa", "sensors", "calibration"}
    assert status["uptime"] == 100 and status["sample_count"] == 1 and status["asc"] is False
    assert status["ready_at"] == START + 60
    assert set(status["sensors"]) == {"i2c", "scd41", "sht41", "sps30"}
    for sensor in status["sensors"].values():
        assert set(sensor) == {"available", "healthy", "last_error", "last_ok_at", "ready_at",
                               "reinit_count", "id"}
    assert status["sensors"]["sps30"]["id"] == "2.2" and status["sensors"]["scd41"]["healthy"] is True
    assert status["sensors"]["scd41"]["ready_at"] == START + 60 and status["sensors"]["i2c"]["ready_at"] is None
    assert status["calibration"]["sample_count"] == 1 and status["calibration"]["last"]["target_ppm"] == 420


def test_ready_at_moves_with_a_reinit(db, log, tmp_config, monkeypatch):
    rig = Rig(db, log, tmp_config, monkeypatch)
    rig.ready()
    rig.scd41.reinit(rig.clock.now(), "test")
    status = build_status(rig.sampler, started_at=START, now=rig.clock.now(), log_failures=0)
    assert status["ready_at"] == rig.scd41.ready_at > status["sensors"]["sht41"]["ready_at"]
