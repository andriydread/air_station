"""The whole station on this server with fake hardware: ``make agent-demo``.

Runs the three real programs — collector (--fake), manager (--fake) and the
dashboard — as subprocesses against a demo database under ``data/demo/``,
optionally pre-filled with hours of generated history so charts have
something to show. Every screenshot then comes from the real code path.

    python tools/demo.py --seed-hours 48 [--port 8080] [--reset]

Ctrl-C (or ``make agent-demo-stop``) stops all three. The pid of this
runner is in ``data/demo/demo.pid``.
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "data" / "demo"
PYTHON = sys.executable


def write_demo_config(port: int) -> Path:
    text = (REPO / "config.toml").read_text()
    text = re.sub(r'^database = .*$', 'database = "data/demo/airstation.db"', text, flags=re.M)
    text = re.sub(r'^logs = .*$', 'logs = "data/demo/logs"', text, flags=re.M)
    text = re.sub(r'^port = .*$', f'port = {port}', text, flags=re.M)
    DEMO.mkdir(parents=True, exist_ok=True)
    path = DEMO / "config.toml"
    # config paths resolve against the config file's directory, so make them absolute
    text = text.replace('"data/demo/airstation.db"', f'"{DEMO / "airstation.db"}"')
    text = text.replace('"data/demo/logs"', f'"{DEMO / "logs"}"')
    path.write_text(text)
    return path


def seed(config_path: Path, hours: float) -> None:
    sys.path.insert(0, str(REPO))
    from shared.config import Config
    from shared.db import Database
    from tests.mocks.fake_hardware import install
    install()
    from tests.mocks.generators import seed_history
    config = Config.load(str(config_path))
    db = Database(config.paths.database)
    counts = seed_history(db, hours, raw_hours=min(hours, 24 * 30))
    db.close()
    print(f"seeded {counts['raw']} raw rows, {counts['hourly']} hourly rows, {counts['vitals']} vitals rows")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="demo")
    parser.add_argument("--seed-hours", type=float, default=0, help="pre-fill this many hours of history")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reset", action="store_true", help="delete data/demo first")
    parser.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
    args = parser.parse_args(argv)

    if args.reset and DEMO.exists():
        import shutil
        shutil.rmtree(DEMO)
    config_path = write_demo_config(args.port)
    if args.seed_hours:
        seed(config_path, args.seed_hours)

    env = {**os.environ, "PYTHONPATH": str(REPO)}
    commands = {
        "collector": [PYTHON, "-m", "collector", "--fake", "--config", str(config_path)],
        "manager": [PYTHON, "-m", "manager", "--fake", "--config", str(config_path)],
        "dashboard": [PYTHON, "-m", "dashboard", "--config", str(config_path)],
    }
    (DEMO / "demo.pid").write_text(str(os.getpid()))
    processes = {}
    for name, argv in commands.items():
        processes[name] = subprocess.Popen(argv, cwd=str(REPO), env=env)
        time.sleep(0.5)
    print(f"demo station running: http://127.0.0.1:{args.port}  (data in {DEMO})")

    def stop(*_a):
        for name, proc in processes.items():
            if proc.poll() is None:
                proc.terminate()
        for name, proc in processes.items():
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.time() + args.duration if args.duration else None
    try:
        while True:
            time.sleep(1)
            for name, proc in processes.items():
                if proc.poll() is not None:
                    print(f"{name} exited with {proc.returncode}", file=sys.stderr)
                    stop()
            if deadline and time.time() >= deadline:
                stop()
    except SystemExit:
        pass
    finally:
        try:
            (DEMO / "demo.pid").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
