"""T01 audit regressions, using only disposable SQLite databases."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_migrations import LEGACY, connect
from sqlalchemy import create_engine
from app import database, migrations
from app.sqlite_backup import DatabaseRecoveryError, file_sha256, restore_database, snapshot_database, verify_backup


def migrate(path):
    engine = create_engine(f"sqlite:///{path}")
    try:
        with patch.object(database, "engine", engine):
            database.init_db()
    finally:
        engine.dispose()


def dump(path):
    with connect(path) as conn:
        return list(conn.iterdump())


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_cli_rejects_occupied_output_without_touching_any_original_file(self):
        source, donor = self.root / "source.db", self.root / "old.db"
        with connect(donor) as conn:
            conn.executescript("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; "
                               "CREATE TABLE facts(value); INSERT INTO facts VALUES ('old');")
            wal = Path(str(donor) + "-wal").read_bytes()
            shm = Path(str(donor) + "-shm").read_bytes()
        with connect(donor) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("UPDATE facts SET value='pending'")
            journal = Path(str(donor) + "-journal").read_bytes()
            conn.rollback()
        occupied = {"wal": {"-wal": wal}, "shm": {"-shm": shm}, "journal": {"-journal": journal},
                    "empty-wal": {"-wal": b""}, "main": {"": donor.read_bytes()},
                    "family": {"": donor.read_bytes(), "-wal": wal, "-shm": shm}}
        with connect(source) as writer:
            writer.executescript("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; "
                                 "CREATE TABLE facts(value); INSERT INTO facts VALUES('trusted');")
            for case, files in occupied.items():
                with self.subTest(case=case):
                    folder = self.root / case
                    folder.mkdir()
                    for suffix, data in files.items():
                        (folder / ("snapshot.db" + suffix)).write_bytes(data)
                    result = subprocess.run([sys.executable, "-m", "app.sqlite_backup", "backup",
                                             "--database", source, "--output", folder / "snapshot.db"],
                                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertEqual(json.loads(result.stdout)["status"], "failed")
                    self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()},
                                     {"snapshot.db" + suffix: data for suffix, data in files.items()})

    def test_sidecar_appearing_at_publication_does_not_get_a_success_receipt(self):
        source, output = self.root / "app.db", self.root / "out.db"
        with connect(source) as conn:
            conn.execute("CREATE TABLE facts(value)")
        link = os.link
        sidecar = Path(str(output) + "-journal")

        def publish_then_collide(staged, target):
            link(staged, target)
            sidecar.write_bytes(b"other owner")

        with patch("app.sqlite_backup.os.link", side_effect=publish_then_collide):
            with self.assertRaises(DatabaseRecoveryError):
                snapshot_database(source, output)
        self.assertEqual(sidecar.read_bytes(), b"other owner")
        self.assertFalse(output.exists())

    def test_failed_publication_leaves_a_replacement_file_untouched(self):
        source, output = self.root / "app.db", self.root / "out.db"
        with connect(source) as conn:
            conn.execute("CREATE TABLE facts(value)")
        link = os.link

        def replace_at_publication(staged, target):
            link(staged, target)
            replacement = self.root / "another-file"
            replacement.write_bytes(b"belongs to another writer")
            os.replace(replacement, target)

        with patch("app.sqlite_backup.os.link", side_effect=replace_at_publication):
            with self.assertRaises(DatabaseRecoveryError):
                snapshot_database(source, output)
        self.assertEqual(output.read_bytes(), b"belongs to another writer")

    def test_wal_snapshot_verifies_and_restores_without_creating_source_sidecars(self):
        source, backup, restored = (self.root / name for name in ("app.db", "copy.db", "restored.db"))
        with connect(source) as writer:
            writer.executescript("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; "
                                 "CREATE TABLE facts(value); INSERT INTO facts VALUES('committed');")
            self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)
            digest = snapshot_database(source, backup)["sha256"]
        verify_backup(backup, digest)
        recovery = restore_database(backup, restored, digest, offline=True)
        for path, expected in ((backup, digest), (restored, recovery["sha256"])):
            with connect(path) as conn:
                self.assertEqual(conn.execute("SELECT * FROM facts").fetchall(), [("committed",)])
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone(), ("wal",))
            self.assertEqual(file_sha256(path), expected)
            self.assertEqual(list(self.root.glob(path.name + "-*")), [])

    def assert_rebuild_refused_with_intact_snapshot(self, path):
        original = dump(path)
        with self.assertLogs("app.migrations", level="INFO") as messages:
            with self.assertRaises(migrations.MigrationError):
                migrate(path)
        self.assertEqual(dump(path), original)
        backup, = (path.parent / "backups").glob("*.db")
        receipt, = [line for line in messages.output if " SHA256=" in line]
        verify_backup(backup, receipt.rsplit(" SHA256=", 1)[1])
        self.assertEqual(dump(backup), original)
        with connect(path) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())

    def test_extra_or_changed_foreign_key_rejects_rebuild_with_original_schema_and_backup(self):
        fk = "FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE"
        for case, clause in (("extra", fk + ", FOREIGN KEY(account_label) REFERENCES labels(value) ON DELETE RESTRICT"),
                             ("update", fk + " ON UPDATE CASCADE"), ("deferred", fk + " DEFERRABLE INITIALLY DEFERRED")):
            with self.subTest(case=case):
                folder = self.root / case
                folder.mkdir()
                path = folder / "app.db"
                with connect(path) as conn:
                    conn.executescript("CREATE TABLE labels(value TEXT PRIMARY KEY); INSERT INTO labels VALUES('alpha');" +
                                       LEGACY.replace(fk, clause, 1))
                self.assert_rebuild_refused_with_intact_snapshot(path)
                if case == "extra":
                    with connect(path) as conn:
                        conn.execute("PRAGMA foreign_keys=ON")
                        with self.assertRaises(sqlite3.IntegrityError):
                            conn.execute("UPDATE aws_credentials SET account_label='not-approved'")

    def test_autoincrement_rejection_preserves_sequence_high_water_mark(self):
        path = self.root / "app.db"
        with connect(path) as conn:
            conn.executescript(LEGACY.replace("aws_credentials (id INTEGER PRIMARY KEY,",
                                              "aws_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT,"))
            conn.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='aws_credentials'")
        self.assert_rebuild_refused_with_intact_snapshot(path)
        with connect(path) as conn:
            conn.execute("DELETE FROM aws_credentials WHERE id=7")
            cursor = conn.execute("INSERT INTO aws_credentials(user_id,access_key_id_enc,secret_access_key_enc) VALUES(1,'key','secret')")
            self.assertEqual(cursor.lastrowid, 101)

    def test_rebuild_refuses_non_rowid_primary_key(self):
        path = self.root / "app.db"
        with connect(path) as conn:
            conn.executescript(LEGACY.replace("aws_credentials (id INTEGER PRIMARY KEY,",
                                              "aws_credentials (id INTEGER PRIMARY KEY DESC,"))
        self.assert_rebuild_refused_with_intact_snapshot(path)

    def test_unique_rules_scope_writes_to_each_credential_for_both_legacy_forms(self):
        rules = {"instance_settings": "user_id,region,instance_name", "traffic_usage": "user_id,region,instance_name,year_month"}
        for form in ("explicit", "inline"):
            path = self.root / (form + ".db")
            sql = LEGACY
            for table, keys in rules.items():
                if form == "explicit":
                    sql += f"CREATE UNIQUE INDEX old_{table} ON {table}({keys});"
                else:
                    end = sql.index(");", sql.index("CREATE TABLE " + table))
                    sql = sql[:end] + f", UNIQUE ({keys})" + sql[end:]
            with connect(path) as conn:
                conn.executescript(sql + "CREATE INDEX by_note ON instance_settings(note);"
                                   "CREATE TABLE writes(observed_region TEXT); CREATE TRIGGER track_usage AFTER INSERT ON traffic_usage "
                                   "BEGIN INSERT INTO writes VALUES (new.region); END;")
            migrate(path)
            migrate(path)
            with connect(path) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("INSERT INTO aws_credentials(id,user_id,access_key_id_enc,secret_access_key_enc,is_default,created_at) "
                             "VALUES(8,1,'other','secret',0,'2026-01-01')")
                for table, insert in (("instance_settings", "INSERT INTO instance_settings(user_id,credential_id,region,instance_name,auto_stop_on_limit,created_at) VALUES(1,8,'ap-southeast-1','server',0,'2026-01-01')"),
                                      ("traffic_usage", "INSERT INTO traffic_usage(user_id,credential_id,region,instance_name,year_month,network_in_bytes,network_out_bytes,last_synced_at) VALUES(1,8,'ap-southeast-1','server','2026-11',1,2,'2026-01-01')")):
                    with self.subTest(form=form, table=table):
                        conn.execute(insert)
                        self.assertEqual(conn.execute(f"SELECT credential_id FROM {table} ORDER BY credential_id").fetchall(), [(7,), (8,)])
                        with self.assertRaises(sqlite3.IntegrityError):
                            conn.execute(insert)
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='by_note'").fetchone())
                self.assertEqual(conn.execute("SELECT * FROM writes").fetchall(), [("ap-southeast-1",)])

    def test_restart_refuses_each_residual_legacy_unique_key_without_rewriting_v1(self):
        for table, cols in (("instance_settings", "user_id, region, instance_name"),
                            ("traffic_usage", "user_id, region, instance_name, year_month")):
            with self.subTest(table=table):
                path = self.root / (table + ".db")
                migrate(path)
                with connect(path) as conn:
                    conn.execute(f"CREATE UNIQUE INDEX leftover ON {table}({cols})")
                original = dump(path)
                with self.assertRaises(migrations.MigrationError):
                    migrate(path)
                self.assertEqual(dump(path), original)

    def test_ambiguous_unique_rule_is_not_silently_removed_or_replayed(self):
        for case, definition in (("collation", "(user_id,region,instance_name COLLATE NOCASE)"),
                                 ("partial", "(user_id,region,instance_name) WHERE note IS NOT NULL"),
                                 ("reordered", "(region,user_id,instance_name)")):
            with self.subTest(case=case):
                folder = self.root / case
                folder.mkdir()
                path = folder / "app.db"
                with connect(path) as conn:
                    conn.executescript(LEGACY + "CREATE UNIQUE INDEX custom_rule ON instance_settings" + definition)
                self.assert_rebuild_refused_with_intact_snapshot(path)

    def test_restart_rejects_missing_ledger_metadata_or_primary_key(self):
        for case, definition in (("missing", "version INTEGER PRIMARY KEY"),
                                 ("no_pk", "version INTEGER, name TEXT NOT NULL, applied_at_utc TEXT NOT NULL, backup_path TEXT, backup_sha256 TEXT")):
            with self.subTest(case=case):
                path = self.root / (case + ".db")
                migrate(path)
                with connect(path) as conn:
                    conn.executescript(f"DROP TABLE schema_migrations; CREATE TABLE schema_migrations({definition});")
                    values = (1,) if case == "missing" else (1, "T01", "2026-11-18", None, None)
                    conn.execute(f"INSERT INTO schema_migrations VALUES ({','.join('?' for _ in values)})", values)
                original = dump(path)
                with self.assertRaises(migrations.MigrationError):
                    migrate(path)
                self.assertEqual(dump(path), original)
