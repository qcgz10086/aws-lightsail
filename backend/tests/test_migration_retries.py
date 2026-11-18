"""Regression tests for T01-F04/F05. Every database is disposable."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_migration_boundaries import dump, migrate
from test_migrations import LEGACY, connect
from app import migrations
from app.sqlite_backup import file_sha256, verify_backup


AUTO = LEGACY.replace('aws_credentials (id INTEGER PRIMARY KEY,',
                      'aws_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT,')


class RetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'app.db'

    def seed(self, sql):
        with connect(self.path) as conn:
            conn.executescript(sql)

    def backups(self):
        return {p: file_sha256(p) for p in (self.path.parent / 'backups').glob('*.db')}

    def refused(self):
        with self.assertRaises(migrations.MigrationError):
            migrate(self.path)
        with connect(self.path) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_migrations'").fetchall())

    def test_failed_retries_reuse_first_verified_snapshot_without_creating_another(self):
        self.seed(AUTO)
        before = file_sha256(self.path)
        self.refused()
        first = self.backups()
        self.assertEqual(len(first), 1)
        for path, digest in first.items():
            verify_backup(path, digest)
        with patch.object(migrations, 'snapshot_database', wraps=migrations.snapshot_database) as create:
            for _ in range(3):
                self.refused()
                self.assertEqual(self.backups(), first)
                self.assertEqual(file_sha256(self.path), before)
            create.assert_not_called()

    def test_new_wal_commit_gets_its_own_snapshot_but_rollback_and_checkpoint_reuse_it(self):
        self.seed(AUTO)
        with connect(self.path) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            self.refused()
            first = self.backups()
            main_hash = file_sha256(self.path)
            writer.execute("UPDATE aws_credentials SET account_label='second'")
            writer.commit()
            self.assertGreater(Path(str(self.path) + '-wal').stat().st_size, 0)
            self.assertEqual(main_hash, file_sha256(self.path))
            self.refused()
            both = self.backups()
            self.assertEqual(len(both), 2)
            self.assertTrue(first.items() <= both.items())
            second, = set(both) - set(first)
            verify_backup(second, both[second])
            with connect(second) as snapshot:
                self.assertEqual(snapshot.execute('SELECT account_label FROM aws_credentials').fetchone(), ('second',))
            writer.execute("UPDATE aws_credentials SET account_label='do not retain'")
            writer.rollback()
            self.refused()
            self.assertEqual(self.backups(), both)
            self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
            self.refused()
            self.assertEqual(self.backups(), both)

    def test_transient_failure_then_success_records_original_path_and_sha(self):
        self.seed(LEGACY)
        with patch.object(migrations, 'apply_v1', side_effect=RuntimeError('interrupted')):
            self.refused()
        before = self.backups()
        migrate(self.path)
        self.assertEqual(self.backups(), before)
        with connect(self.path) as conn:
            path, checksum = conn.execute('SELECT backup_path,backup_sha256 FROM schema_migrations').fetchone()
        self.assertEqual(before, {Path(path): checksum})

    def test_snapshot_survives_failure_between_publication_and_retry_receipt(self):
        self.seed(LEGACY)
        with patch.object(migrations.os, 'replace', side_effect=OSError('cannot publish retry receipt')):
            self.refused()
        first = self.backups()
        self.assertEqual(len(first), 1)
        migrate(self.path)
        self.assertEqual(self.backups(), first)
        with connect(self.path) as conn:
            path, checksum = conn.execute('SELECT backup_path,backup_sha256 FROM schema_migrations').fetchone()
        self.assertEqual(first, {Path(path): checksum})

    def test_contaminated_or_replaced_retry_snapshot_is_not_trusted_or_overwritten(self):
        self.seed(LEGACY)
        with patch.object(migrations, 'apply_v1', side_effect=RuntimeError('interrupted')):
            self.refused()
        snapshot, = self.backups()
        sidecar = Path(str(snapshot) + '-wal')
        sidecar.write_bytes(b'keep this file')
        self.refused()
        self.assertEqual(sidecar.read_bytes(), b'keep this file')
        sidecar.unlink()
        with connect(snapshot) as conn:
            conn.execute("UPDATE aws_credentials SET account_label='unrelated'")
        wrong = self.backups()
        self.refused()
        self.assertEqual(self.backups(), wrong)

    def test_malformed_receipt_or_missing_recorded_snapshot_stops_before_ddl(self):
        self.seed(LEGACY)
        with patch.object(migrations, 'apply_v1', side_effect=RuntimeError('interrupted')):
            self.refused()
        snapshot, = self.backups()
        receipt = snapshot.parent / 'app.db.pre-v1.json'
        original = receipt.read_bytes()
        for value in (b'{', b'[]', b'{}', original.replace(file_sha256(snapshot).encode(), b'0' * 64)):
            with self.subTest(value=value):
                receipt.write_bytes(value)
                self.refused()
                self.assertEqual(receipt.read_bytes(), value)
        receipt.write_bytes(original)
        snapshot.rename(snapshot.with_suffix('.saved'))
        self.refused()
        self.assertFalse(snapshot.exists())
        self.assertEqual(receipt.read_bytes(), original)

    def test_retry_rejects_a_link_in_place_of_the_receipt_or_recorded_snapshot(self):
        self.seed(LEGACY)
        with patch.object(migrations, 'apply_v1', side_effect=RuntimeError('interrupted')):
            self.refused()
        snapshot, = self.backups()
        for path in (snapshot, snapshot.parent / 'app.db.pre-v1.json'):
            saved = path.with_suffix('.saved-' + path.suffix[1:])
            path.rename(saved)
            try:
                path.symlink_to(saved)
            except OSError as exc:
                self.skipTest(f'symlink unavailable: {exc}')
            self.refused()
            self.assertTrue(path.is_symlink())
            path.unlink()
            saved.rename(path)

    def test_retry_rejects_a_hard_link_to_the_live_database(self):
        self.seed(LEGACY)
        with patch.object(migrations, 'apply_v1', side_effect=RuntimeError('interrupted')):
            self.refused()
        snapshot, = self.backups()
        snapshot.unlink()
        os.link(self.path, snapshot)
        before = file_sha256(self.path)
        self.refused()
        self.assertTrue(snapshot.samefile(self.path))
        self.assertEqual(file_sha256(self.path), before)

    def test_snapshot_creation_and_reuse_hold_the_sqlite_writer_lock(self):
        self.seed(LEGACY)
        backup = migrations.migration_backup
        attempts = []

        def check_lock(conn, path, open_files):
            result = backup(conn, path, open_files)
            contender = subprocess.run([sys.executable, '-c', """
import sqlite3, sys
with sqlite3.connect(sys.argv[1], timeout=0) as conn:
    try:
        conn.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
            raise
        print('locked')
    else:
        print('acquired')
""", str(path)], capture_output=True, text=True, check=True, timeout=15)
            attempts.append(contender.stdout.strip())
            return result

        with patch.object(migrations, 'migration_backup', side_effect=check_lock):
            with patch.object(migrations, 'apply_v1', side_effect=RuntimeError('interrupted')):
                self.refused()
            migrate(self.path)
        self.assertEqual(attempts, ['locked', 'locked'])

    def test_custom_column_rules_are_refused_with_verified_original_and_no_ledger(self):
        for index, clause in enumerate(("NOT NULL DEFAULT 'unlabelled'", 'NOT NULL', "DEFAULT 'other'", 'COLLATE NOCASE',
                                        "CHECK (account_label != '')")):
            with self.subTest(clause=clause):
                self.path = self.path.with_name(f'case-{index}.db')
                self.seed(LEGACY.replace('account_label VARCHAR(128)', 'account_label VARCHAR(128) ' + clause))
                before = dump(self.path)
                source_hash = file_sha256(self.path)
                previous = self.backups()
                self.refused()
                self.assertEqual(file_sha256(self.path), source_hash)
                backup, = set(self.backups()) - set(previous)
                verify_backup(backup, self.backups()[backup])
                self.assertEqual(dump(self.path), before)
                self.assertEqual(dump(backup), before)
                if clause.startswith('NOT NULL'):
                    with connect(self.path) as conn, self.assertRaises(sqlite3.IntegrityError):
                        conn.execute('UPDATE aws_credentials SET account_label=NULL')

    def test_generated_known_column_is_not_rebuilt_as_a_plain_column(self):
        self.seed(LEGACY + 'ALTER TABLE aws_credentials ADD COLUMN proxy_url_enc TEXT AS (account_label) VIRTUAL;')
        before = dump(self.path)
        self.refused()
        self.assertEqual(dump(self.path), before)
        backup, = self.backups()
        self.assertEqual(dump(backup), before)

    def test_historical_zero_server_defaults_still_apply_to_omitted_columns(self):
        self.seed(LEGACY.replace('BIGINT NOT NULL', 'BIGINT DEFAULT 0 NOT NULL') +
                  'ALTER TABLE aws_credentials ADD COLUMN is_default BOOLEAN DEFAULT 0 NOT NULL;')
        migrate(self.path)
        with connect(self.path) as conn:
            for table, column in [('aws_credentials', 'is_default'), ('instance_settings', 'auto_stop_on_limit'),
                                  ('traffic_usage', 'network_in_bytes'), ('traffic_usage', 'network_out_bytes')]:
                row = next(r for r in conn.execute(f'PRAGMA table_info({table})') if r[1] == column)
                self.assertEqual((row[3], row[4]), (1, '0'))
            dates = dict((r[1], r[3]) for r in conn.execute('PRAGMA table_info(aws_credentials)'))
            self.assertEqual(dates['created_at'], 0)
            conn.execute("INSERT INTO aws_credentials(user_id,access_key_id_enc,secret_access_key_enc,created_at) "
                         "VALUES(1,'other','other','2026-01-01')")
            self.assertEqual(conn.execute('SELECT is_default FROM aws_credentials ORDER BY id DESC LIMIT 1').fetchone(), (0,))

    def test_custom_column_in_each_child_is_classified_before_application_ddl(self):
        for i, (old, new) in enumerate((('note TEXT', "note TEXT DEFAULT 'custom'"),
                                        ('network_out_bytes BIGINT', 'network_out_bytes TEXT'))):
            with self.subTest(column=old):
                self.path = self.path.with_name(f'child-{i}.db')
                self.seed(LEGACY.replace(old, new))
                before = dump(self.path)
                with patch.object(migrations, 'apply_v1', wraps=migrations.apply_v1) as apply:
                    self.refused()
                    apply.assert_not_called()
                self.assertEqual(dump(self.path), before)
