"""The dashboard program: ``python -m dashboard``.

waitress serves the Flask app on the configured port; a small timer thread
sends the systemd heartbeat every 10 s (it proves the process is alive —
that is the limit of what a watchdog can see); SIGTERM logs a ``shutdown``
event and exits.
"""

import argparse
import signal
import sys
import threading
from typing import Optional

from dashboard.app import APP, create_app
from shared import clock
from shared.config import Config
from shared.db import Database
from shared.events import Log
from shared.heartbeat import HEARTBEAT_SECONDS, SystemdNotifier


class HeartbeatThread:
    """WATCHDOG=1 every 10 s from a daemon thread, until ``stop()``."""

    def __init__(self, notifier: SystemdNotifier, interval: float = HEARTBEAT_SECONDS):
        self.notifier = notifier
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)

    def start(self) -> None:
        self.notifier.ready()
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.notifier.heartbeat()

    def stop(self) -> None:
        self._stop.set()
        self.notifier.stopping()


def serve(config, db: Database, log: Log, notifier: Optional[SystemdNotifier] = None,
          server=None) -> int:
    """Build the app and block in the web server until SIGTERM/SIGINT."""
    from waitress import serve as waitress_serve

    app = create_app(config, db, log)
    notifier = notifier or SystemdNotifier()
    heartbeat = HeartbeatThread(notifier)
    log.start_line(config)
    log.event("info", "app", "started", "dashboard started", port=config.dashboard.port)

    def _stop(signum, _frame):
        reason = signal.Signals(signum).name
        log.event("info", "app", "shutdown", f"dashboard stopping: {reason}", reason=reason)
        heartbeat.stop()
        raise SystemExit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except ValueError:
            pass
    heartbeat.start()
    server = server or waitress_serve
    try:
        server(app, host="0.0.0.0", port=config.dashboard.port, threads=4, ident="airstation")
    except SystemExit:
        pass
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard")
    parser.add_argument("--config", default=None, help="path to config.toml (default: repo root)")
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    db = Database(config.paths.database, now=clock.now)
    log = Log(APP, config, db=db)
    try:
        return serve(config, db, log)
    except Exception:
        log.exception("app", "fatal")
        return 1
    finally:
        log.close()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
