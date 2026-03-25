#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse


def resolve_sqlite_path(database_url: str) -> Path:
    if not database_url.startswith('sqlite:///'):
        raise ValueError('Only sqlite:/// URLs are supported for backup export.')
    raw_path = database_url.replace('sqlite:///', '', 1)
    parsed = urlparse(f'file:///{raw_path}')
    if parsed.path and raw_path.startswith('/'):
        path = Path(unquote(parsed.path))
    else:
        path = Path(unquote(raw_path))
    return path.expanduser().resolve()


def backup_sqlite(source_path: Path, output_path: Path) -> Path:
    if not source_path.exists():
        raise FileNotFoundError(f'SQLite database not found: {source_path}')

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(output_path) as destination:
        source.backup(destination)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Create a portable backup copy of the GPU Monitor SQLite database.')
    parser.add_argument(
        '--database-url',
        default=os.getenv('DATABASE_URL', 'sqlite:///./data/gpu_monitor.db'),
        help='SQLAlchemy database URL. Only sqlite:/// URLs are supported.',
    )
    parser.add_argument(
        '--output',
        default='./backups/gpu_monitor_backup.db',
        help='Where to write the backup SQLite file.',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_path = resolve_sqlite_path(args.database_url)
    output_path = Path(args.output).expanduser().resolve()
    backup_sqlite(source_path, output_path)
    print(output_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
