"""The web server's skeleton: the Flask app factory.

Side-effect free on import: the app is built only by ``create_app()`` (the
service entry point is ``python -m dashboard``; tests call the factory).
Every request gets a debug log line; a 500 becomes a ``server_error`` event.
The routes themselves live in ``dashboard/api.py``.
"""

import time
from pathlib import Path
from typing import Any, Dict

from flask import Flask, g, jsonify, render_template, request, send_from_directory

from shared import clock
from shared.events import git_commit
from shared.render import ICONS_DIR

APP = "dashboard"
TEMPLATES = Path(__file__).resolve().parent / "templates"
STATIC = Path(__file__).resolve().parent / "static"


def create_app(config, db, log) -> Flask:
    app = Flask(APP, template_folder=str(TEMPLATES), static_folder=str(STATIC), static_url_path="/static")
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.extensions["airstation"] = {
        "config": config, "db": db, "log": log,
        "started_at": int(clock.now()), "commit": git_commit(Path(config.repo_root)),
    }

    @app.before_request
    def _start_timer():
        g.started = time.perf_counter()

    @app.after_request
    def _log_request(response):
        started = getattr(g, "started", None)
        ms = round((time.perf_counter() - started) * 1000, 1) if started else None
        if not request.path.startswith("/static/"):
            log.debug("web", "request", method=request.method, path=request.full_path.rstrip("?"),
                      status=response.status_code, ms=ms, ip=request.remote_addr)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(ValueError)
    def _bad_request(exc):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(404)
    def _not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": f"no such endpoint: {request.path}"}), 404
        return "not found", 404

    @app.errorhandler(Exception)
    def _server_error(exc):
        try:
            log.exception("web", "request_failed", path=request.path)
            log.event("error", "web", "server_error", f"{request.path}: {exc.__class__.__name__}: {exc}",
                      path=request.path, error=str(exc))
        except Exception:
            pass
        return jsonify({"error": "internal error", "detail": f"{exc.__class__.__name__}: {exc}"}), 500

    @app.get("/")
    def index() -> Any:
        info = app.extensions["airstation"]
        return render_template("index.html", commit=info["commit"], port=config.dashboard.port)

    @app.get("/assets/icons/<path:filename>")
    def asset_icon(filename: str) -> Any:
        return send_from_directory(str(ICONS_DIR), filename, max_age=86400)

    try:
        from dashboard.api import api  # the routes (T072 onward)
        app.register_blueprint(api)
    except ImportError:
        pass
    return app


def runtime(app: Flask) -> Dict[str, Any]:
    return app.extensions["airstation"]
