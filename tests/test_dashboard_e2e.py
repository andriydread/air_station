"""The real dashboard against a database the real collector and manager code filled.

Fake sensors → collector loop → raw rows → manager minute job → display_data →
the dashboard's endpoints; a button press → mailbox → the manager picks it up.
"""

import sys
import time as _time

import pytest

from collector.__main__ import run as run_collector
from dashboard.app import create_app
from manager.__main__ import run as run_manager
from shared import clock
from shared.db import Database
from shared.events import Log
from tests.mocks.fake_devices import FakeRunner, FakeScd41Device, FakeSht41Device, FakeSps30Device
from tests.test_manager_main import START, Station as ManagerStation

MINUTES = 3


@pytest.fixture
def world(tmp_config, tmp_path, fake_clock, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    _time.tzset()
    fake_clock._wall = START
    db = Database(tmp_config.paths.database, now=clock.now)
    # --- collector, with the real code on fakes, for MINUTES of simulated time
    scd, sht, sps = FakeScd41Device(), FakeSht41Device(), FakeSps30Device()
    scd.default_co2 = 812.0
    monkeypatch.setattr(sys.modules["adafruit_scd4x"], "SCD4X", lambda _i2c: scd)
    monkeypatch.setattr(sys.modules["adafruit_sht4x"], "SHT4x", lambda _i2c: sht)
    ntp = FakeRunner()
    ntp.results["timedatectl"] = FakeRunner.Completed(stdout="yes\n")
    clog = Log("collector", tmp_config, db=db, strict=True, clock=clock.now)
    run_collector(tmp_config, db, clog, lambda: object(), None, max_passes=int(MINUTES * 60 / 0.2) + 1,
                  ntp_runner=ntp, sps30_factory=lambda _i2c: sps)
    clog.close()
    # --- manager, rewound to the same start so it sees the rows as they "arrive"
    fake_clock._wall = START
    mstation = ManagerStation(tmp_config, tmp_path, fake_clock)
    mstation.db.close()
    mstation.db = db
    mstation.log.close()
    mstation.log = Log("manager", tmp_config, db=db, strict=True, clock=clock.now)
    mstation.refresh_collector_status = lambda: None  # the real collector wrote its status
    run_manager(tmp_config, db, mstation.log, None, max_passes=int(MINUTES * 60 / 0.2) + 1,
                extra_tasks=[], **mstation.kwargs())
    # --- the dashboard on the same database
    dlog = Log("dashboard", tmp_config, db=db, strict=True, clock=clock.now)
    app = create_app(tmp_config, db, dlog)
    yield {"client": app.test_client(), "db": db, "manager": mstation, "collector": (scd, sht, sps)}
    dlog.close()
    mstation.log.close()
    db.close()
    monkeypatch.delenv("TZ")
    _time.tzset()


def test_changes_live_and_history_agree_with_the_tables(world):
    client, db = world["client"], world["db"]
    changes = client.get("/api/changes").get_json()
    assert changes["display_data"] and changes["collector_status"] and changes["manager_status"]
    assert changes["raw_at"] == db.latest_raw_at() and changes["vitals_at"] == db.latest_vitals()["recorded_at"]

    live = client.get("/api/live").get_json()
    doc = live["display_data"]["value"]
    assert doc["values"]["temp"] == 22.5 and doc["values"]["co2"] == 812 and doc["aqi_short"] == "Good"
    assert doc["collector_silent"] is False and doc["warming_up"] is False
    assert live["collector_status"]["value"]["sensors"]["scd41"]["healthy"] is True
    assert live["manager_status"]["value"]["display"]["frames"] >= 3
    assert live["version"]["uptimes"]["collector"] is not None

    history = client.get(f"/api/history?from={int(START) - 60}&to={int(START) + MINUTES * 60}").get_json()
    assert history["resolution"] == "raw" and len(history["rows"]) >= MINUTES * 6 - 2
    assert history["stats"]["temp"]["avg"] == 22.5 and history["stats"]["co2"]["n"] > 0
    assert all(row["aqi"] is not None for row in history["rows"] if row["pm25"] is not None)

    vitals = client.get(f"/api/vitals?from={int(START)}&to={int(START) + MINUTES * 60}").get_json()
    assert vitals["latest"]["collector_lag"] is not None and len(vitals["rows"]) >= 2

    csv = client.get(f"/api/export.csv?from={int(START)}&to={int(START) + MINUTES * 60}").get_data(as_text=True)
    assert csv.count("\n") == len(db.raw_between(int(START), int(START) + MINUTES * 60)) + 1

    events = client.get("/api/events?app=collector").get_json()["events"]
    assert {e["type"] for e in events} >= {"started", "sensor_init", "warming_up", "shutdown"}
    preview = client.get("/api/display-preview.png")
    assert preview.status_code == 200 and preview.mimetype == "image/png"


def test_a_button_press_reaches_the_manager(world, fake_clock):
    client, db, mstation = world["client"], world["db"], world["manager"]
    response = client.post("/api/commands", json={"type": "restart_collector", "payload": {}})
    assert response.status_code == 202
    cid = response.get_json()["id"]
    assert client.get("/api/commands").get_json()["commands"][0]["status"] == "pending"
    # the manager's next loop picks it up
    mstation.log.close()
    mstation.log = Log("manager", mstation.config, db=db, strict=True, clock=clock.now)
    run_manager(mstation.config, db, mstation.log, None, max_passes=30, extra_tasks=[], **mstation.kwargs())
    row = {r["id"]: r for r in client.get("/api/commands").get_json()["commands"]}[cid]
    assert row["status"] == "success" and row["result"]["scheduled"].endswith("airstation-collector")
    assert mstation.spawned[-1][-1].endswith("airstation-collector")
    assert client.get("/api/changes").get_json()["command_id"] == cid
