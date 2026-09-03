"""`python -m tools.backup <destination> [--config path]` — a consistent copy of
the live database (SQLite online backup, read-only on the source), used by
`make export`. Prints the copy's size."""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional

from shared.config import Config


def backup(source: Path, destination: Path) -> int:
    if not source.exists():
        raise FileNotFoundError(f"no database at {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst, pages=2000)
    finally:
        dst.close()
        src.close()
    return destination.stat().st_size


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="online backup of the station's database")
    parser.add_argument("destination")
    parser.add_argument("--config", default=None, help="path to config.toml (default: repo root)")
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    size = backup(Path(config.paths.database), Path(args.destination))
    print(f"database copied to {args.destination} ({size / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
