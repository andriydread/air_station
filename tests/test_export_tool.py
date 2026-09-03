"""`make export` (tools/export.sh + tools/backup.py) run for real on this server
against a temp config, then `make agent-import` (tools/import_archive.py) on
the archive it made."""

import subprocess
import tarfile
from pathlib import Path

from tools import backup, import_archive

REPO = Path(__file__).resolve().parents[1]


def write_config(tmp_config, tmp_path) -> Path:
    text = (REPO / "config.toml").read_text()
    text = text.replace('database = "data/airstation.db"', f'database = "{tmp_config.paths.database}"')
    text = text.replace('logs = "data/logs"', f'logs = "{tmp_config.paths.logs}"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_backup_is_a_readable_copy(tmp_config, db, tmp_path):
    db.insert_raw(1_700_000_000, {"co2": 500})
    target = tmp_path / "copy" / "airstation.db"
    size = backup.backup(Path(tmp_config.paths.database), target)
    assert size > 0 and target.exists()
    import sqlite3
    assert sqlite3.connect(str(target)).execute("SELECT co2 FROM raw_measurements").fetchone()[0] == 500


def test_backup_main_reads_the_config(tmp_config, db, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(backup, "Config", type("C", (), {"load": staticmethod(lambda _p: tmp_config)}))
    assert backup.main([str(tmp_path / "out.db"), "--config", "x"]) == 0
    assert "database copied to" in capsys.readouterr().out


def test_export_then_import(tmp_config, db, tmp_path, capsys):
    for i in range(10):
        db.insert_raw(1_700_000_000 + 10 * i, {"co2": 500 + i, "temp": 21.0})
    db.insert_event("collector", "info", "app", "started", "collector started")
    logs = Path(tmp_config.paths.logs)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "collector.log").write_text("2026-09-03T00:00:00Z INFO collector app started\n")
    (logs / "collector.log.2026-09-02").write_text("2026-09-03T00:00:00Z INFO manager app started\n")
    config_path = write_config(tmp_config, tmp_path)
    out = tmp_path / "home"

    result = subprocess.run(["sh", str(REPO / "tools" / "export.sh"), "--config", str(config_path),
                             "--out", str(out)], capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    archives = list(out.glob("airstation-*.tar.gz"))
    assert len(archives) == 1
    assert f"archive: {archives[0]}" in result.stdout

    with tarfile.open(archives[0]) as tar:
        names = {n.split("/", 1)[1] for n in tar.getnames() if "/" in n}
    for member in ("db/airstation.db", "logs/collector.log", "logs/collector.log.2026-09-02",
                   "journal/units.txt", "journal/kernel-current.txt", "journal/kernel-previous.txt",
                   "system/throttled.txt", "system/temp.txt", "system/df.txt", "system/free.txt",
                   "system/uname.txt", "config.toml", "commit.txt"):
        assert member in names, member
    stamp = archives[0].name[len("airstation-"):-len(".tar.gz")]
    assert len(stamp) == 13 and stamp[8] == "-"

    # --- the other side: make agent-import FILE=…
    into = tmp_path / "from_pi"
    assert import_archive.main([str(archives[0]), "--into", str(into)]) == 0
    text = capsys.readouterr().out
    tree = into / archives[0].name[:-len(".tar.gz")]
    assert tree.is_dir() and (tree / "db" / "airstation.db").exists()
    assert f"unpacked to {tree}" in text
    assert "raw_measurements     10" in text and "events               1" in text
    assert "raw rows span 0.0 days" in text and "log files: 2 (collector.log … collector.log.2026-09-02)" in text
    assert (tree / "commit.txt").read_text().strip() != ""


def test_export_refuses_without_a_database(tmp_config, tmp_path):
    config_path = write_config(tmp_config, tmp_path)
    result = subprocess.run(["sh", str(REPO / "tools" / "export.sh"), "--config", str(config_path),
                             "--out", str(tmp_path / "home")], capture_output=True, text=True)
    assert result.returncode != 0 and "no database at" in result.stderr
    assert not list((tmp_path / "home").glob("*.tar.gz")) if (tmp_path / "home").exists() else True


def test_makefile_export_and_import_targets():
    out = subprocess.run(["make", "-C", str(REPO), "-n", "export"], capture_output=True, text=True).stdout
    assert "tools/export.sh" in out
    result = subprocess.run(["make", "-C", str(REPO), "agent-import"], capture_output=True, text=True)
    assert result.returncode != 0 and "FILE=" in result.stdout
    out = subprocess.run(["make", "-C", str(REPO), "-n", "agent-import", "FILE=x.tar.gz"],
                         capture_output=True, text=True).stdout
    assert "tools.import_archive x.tar.gz" in out
