#!/usr/bin/env python3
"""Consistent SQLite snapshots. Runs with the same unprivileged UID as the app."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.parse import quote
import uuid

PATTERN = re.compile(r'^kwazar-\d{8}T\d{6}Z-[a-f0-9]{8}\.sqlite3$')


def backup_database(source, output_dir, keep=168):
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    if not source.is_file():
        raise ValueError('Database does not exist; no empty replacement was created')
    if keep < 1:
        raise ValueError('Keep must be at least one')
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = output_dir / f'kwazar-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3'
    descriptor, temporary_name = tempfile.mkstemp(prefix='.kwazar-', suffix='.pending', dir=output_dir)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect('file:' + quote(str(source), safe='/') + '?mode=ro', uri=True, timeout=10) as original:
            with sqlite3.connect(temporary) as snapshot:
                original.backup(snapshot, pages=256, sleep=0.05)
                if snapshot.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Snapshot integrity check failed')
                snapshot.execute('PRAGMA journal_mode=DELETE')
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
        snapshots = sorted((p for p in output_dir.iterdir() if PATTERN.fullmatch(p.name) and p.is_file() and not p.is_symlink()),
                           key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for old in snapshots[keep:]:
            old.unlink()
        return destination
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('KWAZAR_DATA_DIR', '/workspace/kwazar-data')))
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--keep', type=int, default=168)
    parser.add_argument('--interval', type=int, default=0, help='Seconds between snapshots; 0 makes one backup')
    args = parser.parse_args()
    if args.interval and args.interval < 60:
        parser.error('Interval must be 0 or at least 60 seconds')
    while True:
        try:
            result = backup_database(args.data_dir / 'orders.sqlite3', args.output_dir, args.keep)
            print('Snapshot verified: ' + result.name, flush=True)
        except (ValueError, OSError, sqlite3.Error):
            print('Backup failed; existing database and snapshots left intact.', flush=True)
            if not args.interval:
                return 1
        if not args.interval:
            return 0
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
