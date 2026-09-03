"""`make agent-import FILE=<archive>` — unpack a `make export` archive on the
dev server into `from_pi/<stamp>/` and print what the database holds."""

import argparse
import sqlite3
import sys
import tarfile
from pathlib import Path
from typing import List, Optional

from shared.config import REPO_ROOT

TABLES = ("raw_measurements", "hourly_measurements", "vitals", "events", "commands", "state")


def unpack(archive: Path, into: Path) -> Path:
    with tarfile.open(archive) as tar:
        names = [m.name for m in tar.getmembers()]
        top = {n.split("/", 1)[0] for n in names if n and not n.startswith(("/", ".."))}
        if len(top) != 1:
            raise ValueError(f"expected one top-level directory in {archive}, got {sorted(top)}")
        tar.extractall(into, filter="data")
    return into / top.pop()


def summary(tree: Path) -> List[str]:
    lines = [f"unpacked to {tree}"]
    commit = tree / "commit.txt"
    if commit.exists():
        lines.append(f"commit {commit.read_text().strip()}")
    db_path = tree / "db" / "airstation.db"
    if not db_path.exists():
        lines.append("no database in the archive")
        return lines
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for table in TABLES:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                count = "missing"
            lines.append(f"  {table:<20} {count}")
        span = conn.execute("SELECT MIN(recorded_at), MAX(recorded_at) FROM raw_measurements").fetchone()
        if span and span[0] is not None:
            days = (span[1] - span[0]) / 86400
            lines.append(f"raw rows span {days:.1f} days")
    finally:
        conn.close()
    logs = sorted(p.name for p in (tree / "logs").glob("*")) if (tree / "logs").exists() else []
    lines.append(f"log files: {len(logs)}" + (f" ({logs[0]} … {logs[-1]})" if logs else ""))
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="unpack a make export archive")
    parser.add_argument("archive")
    parser.add_argument("--into", default=str(REPO_ROOT / "from_pi"))
    args = parser.parse_args(argv)
    tree = unpack(Path(args.archive), Path(args.into))
    print("\n".join(summary(tree)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
