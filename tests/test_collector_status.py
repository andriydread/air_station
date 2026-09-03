"""collector_status shape and the debug sample lines."""

from collector.status import build_status, debug_sample_lines
from tests.test_sampling import Rig


def test_status_shape_is_frozen(db, log, tmp_config, monkeypatch):
    rig = Rig(db, log, tmp_config, monkeypatch)
    rig.warm()
    rig.beat()
    now = rig.clock.now()
    status = build_status(rig.sampler, started_at=rig.clock.now() - 100, now=now,
                          log_failures=log.failures, last_calibration={"at": 1, "target_ppm": 420})
    assert set(status) == {"started_at", "uptime", "sample_count", "log_failures", "storage_failures",
                           "bus_reinits", "asc", "pressure_hpa", "sensors", "calibration"}
    assert status["uptime"] == 100 and status["sample_count"] == 2 and status["asc"] is False
    assert set(status["sensors"]) == {"i2c", "scd41", "sht41", "sps30"}
    for sensor in status["sensors"].values():
        assert set(sensor) == {"available", "healthy", "last_error", "last_ok_at", "warmup_left",
                               "reinit_count", "id"}
    assert status["sensors"]["sps30"]["id"] == "2.2" and status["sensors"]["scd41"]["healthy"] is True
    assert status["calibration"]["sample_count"] == 1 and status["calibration"]["last"]["target_ppm"] == 420


def test_debug_lines_carry_unstored_values_and_errno(db, log, tmp_config, monkeypatch):
    rig = Rig(db, log, tmp_config, monkeypatch)
    rig.warm()
    rig.sht.raise_on_read = OSError(121, "Remote I/O error")
    rig.scd.default_co2 = 0.0
    record = rig.beat()
    debug_sample_lines(log, record)
    log.close()
    lines = [l for l in log.path.read_text().splitlines() if " sample " in l]
    sps = next(l for l in lines if " sps30 sample " in l)
    assert "x_pm4=3.0" in sps and "x_nc10=8.9" in sps and "st_fan_error=0" in sps and "pm25_ok=1" in sps
    sht = next(l for l in lines if " sht41 sample " in l)
    assert "errno=121" in sht and 'error="OSError: [Errno 121] Remote I/O error"' in sht and "temp=-" in sht
    scd = next(l for l in lines if " scd41 sample " in l)
    assert "co2=0.0" in scd and "co2_ok=0" in scd and "dropped=co2:range" in scd
    row_line = next(l for l in lines if " sample row " in l)
    assert "raised=sht41" in row_line and "empty=co2,temp,humid" in row_line
