import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('backup', Path(__file__).resolve().parents[1] / 'backup_store.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupTest(unittest.TestCase):
    def test_live_wal_data_can_be_restored_and_rotation_preserves_other_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'orders.sqlite3'
            output = root / 'backups'
            with sqlite3.connect(source) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE orders (id TEXT, payload TEXT)')
                db.execute("INSERT INTO orders VALUES ('synthetic-order', 'synthetic-data')")
                db.commit()
                self.assertTrue(Path(str(source) + '-wal').is_file())
                first = backup.backup_database(source, output, keep=2)
                with sqlite3.connect(first) as restored:
                    self.assertEqual(restored.execute('SELECT id,payload FROM orders').fetchall(), [('synthetic-order', 'synthetic-data')])
                    self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(first.stat().st_mode & 0o777, 0o600)
                other = output / 'unrelated.sqlite3'
                other.write_text('Keep this user file')
                for _ in range(3):
                    backup.backup_database(source, output, keep=2)
                self.assertTrue(other.exists())
                self.assertEqual(len([p for p in output.iterdir() if backup.PATTERN.fullmatch(p.name)]), 2)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)

    def test_missing_or_corrupt_database_does_not_replace_good_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'backups'
            missing = root / 'missing.sqlite3'
            with self.assertRaises(ValueError):
                backup.backup_database(missing, output)
            self.assertFalse(missing.exists())
            source = root / 'orders.sqlite3'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE orders (id TEXT)')
            good = backup.backup_database(source, output)
            source.write_bytes(b'not a database')
            with self.assertRaises(sqlite3.DatabaseError):
                backup.backup_database(source, output)
            self.assertTrue(good.exists())
            self.assertFalse(list(output.glob('*.pending')))


if __name__ == '__main__':
    unittest.main()
