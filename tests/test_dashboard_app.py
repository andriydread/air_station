"""The app factory: page, 404 under /api, 500 → event, request log lines, heartbeat thread."""

import pytest

from dashboard.__main__ import HeartbeatThread, serve
from dashboard.app import create_app, runtime
from shared.events import Log
from shared.heartbeat import SystemdNotifier


@pytest.fixture
def dlog(tmp_config, db):
    logger = Log("dashboard", tmp_config, db=db, strict=True)
    yield logger
    logger.close()


@pytest.fixture
def client(tmp_config, db, dlog):
    app = create_app(tmp_config, db, dlog)
    app.config["TESTING"] = False  # let the 500 handler run
    return app.test_client()


def test_index_and_icons(client):
    assert client.get("/").status_code == 200
    icon = client.get("/assets/icons/sun.png")
    assert icon.status_code == 200 and icon.mimetype == "image/png"
    assert client.get("/assets/icons/../../config.toml").status_code == 404


def test_api_404_is_json_and_page_404_is_text(client):
    missing = client.get("/api/nothing")
    assert missing.status_code == 404 and missing.get_json()["error"].startswith("no such endpoint")
    assert client.get("/nothing").status_code == 404


def test_500_writes_a_server_error_event(tmp_config, db, dlog):
    app = create_app(tmp_config, db, dlog)

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("kaboom")

    response = app.test_client().get("/api/boom")
    assert response.status_code == 500 and "kaboom" in response.get_json()["detail"]
    events = db.recent_events()
    assert events[0]["type"] == "server_error" and events[0]["source"] == "web"
    assert events[0]["details"]["path"] == "/api/boom"


def test_value_error_is_a_400(tmp_config, db, dlog):
    app = create_app(tmp_config, db, dlog)

    @app.get("/api/bad")
    def bad():
        raise ValueError("from must be before to")

    response = app.test_client().get("/api/bad")
    assert response.status_code == 400 and response.get_json() == {"error": "from must be before to"}


def test_every_request_gets_a_debug_line_and_no_store(client, dlog):
    response = client.get("/api/nothing?x=1")
    assert response.headers["Cache-Control"] == "no-store"
    dlog.close()
    lines = dlog.path.read_text().splitlines()
    line = next(l for l in lines if " web request " in l)
    assert "method=GET" in line and 'path="/api/nothing?x=1"' in line and "status=404" in line and "ms=" in line


def test_runtime_info(tmp_config, db, dlog):
    app = create_app(tmp_config, db, dlog)
    info = runtime(app)
    assert info["db"] is db and isinstance(info["commit"], str) and info["started_at"] > 0


def test_heartbeat_thread_pings_and_stops():
    class Pinger(SystemdNotifier):
        def __init__(self):
            super().__init__(address="")
            self.messages = []

        def _send(self, message):
            self.messages.append(message)

    pinger = Pinger()
    thread = HeartbeatThread(pinger, interval=0.02)
    thread.start()
    import time
    time.sleep(0.15)
    thread.stop()
    assert pinger.messages[0] == "READY=1" and pinger.messages[-1] == "STOPPING=1"
    assert pinger.messages.count("WATCHDOG=1") >= 3


def test_serve_builds_the_app_and_logs_started(tmp_config, db, dlog):
    seen = {}

    def fake_server(app, host, port, threads, ident):
        seen.update(host=host, port=port, threads=threads)
        assert app.test_client().get("/").status_code == 200

    assert serve(tmp_config, db, dlog, notifier=SystemdNotifier(address=""), server=fake_server) == 0
    assert seen == {"host": "0.0.0.0", "port": 8080, "threads": 4}
    assert db.recent_events()[0]["type"] == "started"
