"""T01 migration acceptance tests; temporary SQLite only, no AWS calls."""
import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import sqlite
from app import database, models, migrations
from app.sqlite_backup import DatabaseRecoveryError, file_sha256, restore_database, snapshot_database, verify_backup


@contextmanager
def connect(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


LEGACY = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE,
 password_hash VARCHAR(255) NOT NULL, is_admin BOOLEAN NOT NULL,
 monthly_limit_gb FLOAT, created_at DATETIME);
CREATE TABLE aws_credentials (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE,
 access_key_id_enc TEXT NOT NULL, secret_access_key_enc TEXT NOT NULL,
 account_label VARCHAR(128), last_validated_at DATETIME, created_at DATETIME,
 FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE instance_settings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
 region VARCHAR(32) NOT NULL, instance_name VARCHAR(128) NOT NULL, monthly_limit_gb FLOAT,
 auto_stop_on_limit BOOLEAN DEFAULT 0 NOT NULL, note TEXT, created_at DATETIME,
 FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE traffic_usage (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
 region VARCHAR(32) NOT NULL, instance_name VARCHAR(128) NOT NULL, year_month VARCHAR(7) NOT NULL,
 network_in_bytes BIGINT NOT NULL, network_out_bytes BIGINT NOT NULL, last_synced_at DATETIME,
 FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
INSERT INTO users VALUES (1, 'alpha', 'opaque-password-hash', 0, 9.5, '2026-01-01');
INSERT INTO aws_credentials VALUES (7, 1, 'opaque-key', 'opaque-secret', 'alpha',
 '2026-02-01', '2026-01-01');
INSERT INTO instance_settings VALUES (9, 1, 'ap-southeast-1', 'server', 123.5, 1, 'keep', '2026-01-01');
INSERT INTO traffic_usage VALUES (11, 1, 'ap-southeast-1', 'server', '2026-11', 123456, 987654, '2026-11-17');
"""


def seed_current(path, unique=False):
    with connect(path) as conn:
        for table in database.Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
            if unique and table.name == "aws_credentials":
                pos = ddl.rfind(")")
                ddl = ddl[:pos] + ", UNIQUE(user_id)" + ddl[pos:]
            conn.execute(ddl)
        conn.execute("INSERT INTO users (id,username,password_hash,is_admin,token_version,auto_stop_on_limit_default,created_at) VALUES (1,'alpha','opaque-hash',0,3,1,'2026-01-01')")
        conn.execute("""INSERT INTO aws_credentials
          (id,user_id,access_key_id_enc,secret_access_key_enc,proxy_url_enc,account_label,is_default,
           vcpu_quota,vcpu_tier,static_ip_quota,used_vcpu,used_instance_count,quota_region,
           quota_message,quota_checked_at,last_validated_at,created_at)
          VALUES (7,1,'opaque-key','opaque-secret','opaque-proxy','alpha',0,32,'32V',5,4,2,
            'ap-southeast-1','keep message','2026-11-17','2026-11-16','2026-01-01')""")
        conn.execute("INSERT INTO instance_settings (id,user_id,credential_id,region,instance_name,auto_stop_on_limit,note,created_at) VALUES (9,1,7,'ap-southeast-1','server',1,'keep','2026-01-01')")
        conn.execute("INSERT INTO traffic_usage (id,user_id,credential_id,region,instance_name,year_month,network_in_bytes,network_out_bytes,last_synced_at) VALUES (11,1,7,'ap-southeast-1','server','2026-11',123456,987654,'2026-11-17')")
        conn.execute("INSERT INTO credit_records (id,user_id,credential_id,name,total_amount,currency,start_date,created_at) VALUES (13,1,7,'credit',100,'USD','2026-01-01','2026-01-01')")


def downgrade_fixture_to_t01(path):
    """Restore the T01 columns and its original five-column traffic UNIQUE key."""
    with connect(path) as conn:
        conn.execute("DROP INDEX IF EXISTS ix_aws_credentials_account_id")
        for column in ("account_id", "last_confirmed_account_id", "auto_stop_conflict"):
            if column in {row[1] for row in conn.execute("PRAGMA table_info(aws_credentials)")}:
                conn.execute(f'ALTER TABLE aws_credentials DROP COLUMN "{column}"')
        for column in ("auto_stop_conflict", "auto_stop_configured", "auto_stop_account_id"):
            if column in {row[1] for row in conn.execute("PRAGMA table_info(instance_settings)")}:
                conn.execute(f'ALTER TABLE instance_settings DROP COLUMN "{column}"')
        conn.execute("""CREATE TABLE traffic_usage_t01 (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, credential_id INTEGER NOT NULL,
            region VARCHAR(32) NOT NULL, instance_name VARCHAR(128) NOT NULL,
            year_month VARCHAR(7) NOT NULL, network_in_bytes BIGINT NOT NULL,
            network_out_bytes BIGINT NOT NULL, last_synced_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(credential_id) REFERENCES aws_credentials(id) ON DELETE CASCADE,
            CONSTRAINT uq_traffic_usage UNIQUE(user_id,credential_id,region,instance_name,year_month))""")
        conn.execute("INSERT INTO traffic_usage_t01 SELECT id,user_id,credential_id,region,instance_name,year_month,network_in_bytes,network_out_bytes,last_synced_at FROM traffic_usage")
        conn.execute("DROP TABLE traffic_usage")
        conn.execute("ALTER TABLE traffic_usage_t01 RENAME TO traffic_usage")
        conn.execute("DELETE FROM schema_migrations WHERE version=2")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.db"
        self.engine = create_engine(f"sqlite:///{self.path}")
        self.addCleanup(self.engine.dispose)

    def migrate(self):
        with patch.object(database, "engine", self.engine):
            database.init_db()

    def rows(self, sql):
        with connect(self.path) as conn:
            return conn.execute(sql).fetchall()

    def test_legacy_rebuild_preserves_all_credential_fields_and_children(self):
        seed_current(self.path, unique=True)
        before = self.rows("SELECT * FROM aws_credentials")
        self.migrate()
        self.assertEqual(self.rows("SELECT * FROM aws_credentials"), before)
        for table, ident in [("instance_settings", 9), ("traffic_usage", 11), ("credit_records", 13)]:
            self.assertEqual(self.rows(f"SELECT id,credential_id FROM {table}"), [(ident, 7)])
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])
        self.assertIn("aws_credentials", [row[2] for row in self.rows("PRAGMA foreign_key_list(credit_records)")])
        with connect(self.path) as conn:
            conn.execute("INSERT INTO aws_credentials (id,user_id,access_key_id_enc,secret_access_key_enc,is_default,created_at) VALUES (8,1,'second-key','second-secret',0,'2026-01-01')")

    def test_minimal_legacy_preserves_auto_stop_and_assigns_only_owner_key(self):
        with connect(self.path) as conn:
            conn.executescript(LEGACY)
        self.migrate()
        self.assertEqual(self.rows("SELECT id,credential_id,auto_stop_on_limit,note FROM instance_settings"), [(9, 7, 1, "keep")])
        self.assertEqual(self.rows("SELECT auto_stop_configured,auto_stop_account_id FROM instance_settings"), [(1, None)])
        self.assertEqual(self.rows("SELECT id,credential_id,network_in_bytes,network_out_bytes FROM traffic_usage"), [(11, 7, 123456, 987654)])
        self.assertEqual(self.rows("SELECT proxy_url_enc,vcpu_quota,is_default FROM aws_credentials"), [(None, None, 1)])

    def test_unowned_legacy_records_abort_instead_of_being_dropped(self):
        with connect(self.path) as conn:
            conn.executescript(LEGACY)
            conn.execute("DELETE FROM aws_credentials")
        before = self.rows("SELECT * FROM instance_settings")
        with self.assertRaises(Exception):
            self.migrate()
        self.assertEqual(self.rows("SELECT * FROM instance_settings"), before)

    def test_new_database_has_version_and_repeated_start_is_idempotent(self):
        self.migrate()
        self.assertEqual(self.rows("SELECT version FROM schema_migrations"), [(1,), (2,)])
        self.migrate()
        self.assertEqual(self.rows("SELECT version FROM schema_migrations"), [(1,), (2,)])

    def test_t01_database_upgrades_atomically_to_t02_with_paired_snapshot(self):
        seed_current(self.path)
        self.migrate()
        downgrade_fixture_to_t01(self.path)
        with connect(self.path) as conn:
            conn.execute("UPDATE instance_settings SET auto_stop_on_limit=1 WHERE id=9")
        self.migrate()
        self.assertEqual(self.rows("SELECT version FROM schema_migrations ORDER BY version"), [(1,), (2,)])
        self.assertEqual(self.rows("SELECT account_id FROM aws_credentials WHERE id=7"), [(None,)])
        self.assertEqual(self.rows("SELECT last_confirmed_account_id FROM aws_credentials WHERE id=7"), [(None,)])
        self.assertEqual(self.rows("SELECT auto_stop_configured,auto_stop_account_id FROM instance_settings WHERE id=9"), [(1, None)])
        self.assertIn("account_id", {row[1] for row in self.rows("PRAGMA table_info(traffic_usage)")})
        receipt, checksum = self.rows("SELECT backup_path,backup_sha256 FROM schema_migrations WHERE version=2")[0]
        self.assertTrue(Path(receipt).is_file())
        restored = Path(self.tmp.name) / "v1-restored.db"
        restore_database(receipt, restored, checksum, offline=True)
        with connect(restored) as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(1,)])
            self.assertNotIn("account_id", {row[1] for row in conn.execute("PRAGMA table_info(aws_credentials)")})
            self.assertNotIn("account_id", {row[1] for row in conn.execute("PRAGMA table_info(traffic_usage)")})
            self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(1,)])

    def test_false_legacy_policy_remains_off_after_upgrade_with_user_default_on(self):
        from app.models import AwsCredential, InstanceSetting, User
        from app.services.collector import sync_user_traffic

        seed_current(self.path)
        self.migrate()
        downgrade_fixture_to_t01(self.path)
        with connect(self.path) as conn:
            conn.execute("UPDATE users SET auto_stop_on_limit_default=1 WHERE id=1")
            conn.execute("UPDATE instance_settings SET auto_stop_on_limit=0 WHERE id=9")
            from app.security import encrypt_secret
            conn.execute("UPDATE aws_credentials SET access_key_id_enc=?, secret_access_key_enc=?, proxy_url_enc=NULL WHERE id=7",
                         (encrypt_secret("AKIAIOSFODNN7EXAMPLE"),
                          encrypt_secret("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")))
        self.migrate()
        self.assertEqual(self.rows("SELECT auto_stop_configured,auto_stop_on_limit,auto_stop_account_id FROM instance_settings WHERE id=9"), [(1, 0, None)])

        db = sessionmaker(bind=self.engine)()
        try:
            user = db.query(User).filter_by(id=1).one()
            credential = db.query(AwsCredential).filter_by(id=7).one()
            self.assertIsNone(credential.account_id)
            with patch("app.services.collector.ls.get_regions", return_value=[{"name": "ap-southeast-1"}]), \
                 patch("app.services.collector.ls.list_instances_in_region", return_value=[{"name": "server", "state": "running"}]), \
                 patch("app.services.collector.ls.get_metric_sum_bytes", return_value=200 * 1024**3) as metrics, \
                 patch("app.services.collector.ls.stop_instance") as stop:
                self.assertEqual(sync_user_traffic(db, user), 1)
                self.assertEqual(metrics.call_count, 2)
                stop.assert_not_called()
            setting = db.query(InstanceSetting).filter_by(id=9).one()
            self.assertFalse(setting.auto_stop_on_limit)
            self.assertTrue(user.auto_stop_on_limit_default)
        finally:
            db.close()

    def test_current_multi_key_data_is_unchanged(self):
        seed_current(self.path)
        with connect(self.path) as conn:
            conn.execute("INSERT INTO aws_credentials (id,user_id,access_key_id_enc,secret_access_key_enc,is_default,created_at) VALUES (8,1,'second-key','second-secret',1,'2026-01-01')")
        before = self.rows("SELECT * FROM aws_credentials")
        self.migrate()
        self.assertEqual(self.rows("SELECT * FROM aws_credentials"), before)
        self.assertEqual(self.rows("SELECT version FROM schema_migrations"), [(1,), (2,)])


    def test_migration_failure_rolls_back_ddl_and_keeps_verified_original_backup(self):
        with connect(self.path) as conn:
            conn.executescript(LEGACY)
        before = self.rows("SELECT type,name,sql FROM sqlite_master ORDER BY name")
        real_rebuild = migrations.rebuild

        def fail_after_rebuild(conn, table, defaults):
            real_rebuild(conn, table, defaults)
            raise RuntimeError("injected after table replacement")

        with patch.object(migrations, "rebuild", side_effect=fail_after_rebuild):
            with self.assertRaises(migrations.MigrationError):
                self.migrate()
        self.assertEqual(self.rows("SELECT type,name,sql FROM sqlite_master ORDER BY name"), before)
        self.assertEqual(self.rows("SELECT id,access_key_id_enc FROM aws_credentials"), [(7, "opaque-key")])
        backups = list((self.path.parent / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        verify_backup(backups[0], file_sha256(backups[0]))
        with connect(backups[0]) as conn:
            self.assertEqual(conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY name").fetchall(), before)
        # No version is committed on failure; the same original DB can upgrade next time.
        self.migrate()
        self.assertEqual(self.rows("SELECT version FROM schema_migrations"), [(1,), (2,)])

    def test_backup_failure_prevents_any_schema_change(self):
        with connect(self.path) as conn:
            conn.executescript(LEGACY)
        before = self.rows("SELECT type,name,sql FROM sqlite_master ORDER BY name")
        with patch.object(migrations, "snapshot_database", side_effect=OSError("disk full")):
            with self.assertRaises(migrations.MigrationError):
                self.migrate()
        self.assertEqual(self.rows("SELECT type,name,sql FROM sqlite_master ORDER BY name"), before)

    def test_future_version_and_versioned_schema_drift_stop_startup(self):
        self.migrate()
        with connect(self.path) as conn:
            conn.execute("INSERT INTO schema_migrations VALUES (3,'future','2026-11-19',NULL,NULL)")
        with self.assertRaises(migrations.MigrationError):
            self.migrate()
        self.assertEqual(self.rows("SELECT version FROM schema_migrations"), [(1,), (2,), (3,)])
        with connect(self.path) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version=3")
            conn.execute("ALTER TABLE aws_credentials DROP COLUMN quota_message")
        with self.assertRaises(migrations.MigrationError):
            self.migrate()

    def test_ambiguous_legacy_owner_stops_instead_of_choosing_a_key(self):
        seed_current(self.path)
        with connect(self.path) as conn:
            conn.execute("DROP TABLE instance_settings")
            ddl = LEGACY[LEGACY.index("CREATE TABLE instance_settings"):LEGACY.index("CREATE TABLE traffic_usage")]
            conn.execute(ddl)
            conn.execute("INSERT INTO instance_settings VALUES (9,1,'ap-southeast-1','server',123.5,1,'keep','2026-01-01')")
            conn.execute("INSERT INTO aws_credentials (id,user_id,access_key_id_enc,secret_access_key_enc,is_default,created_at) VALUES (8,1,'second-key','second-secret',1,'2026-01-01')")
        with self.assertRaises(migrations.MigrationError):
            self.migrate()
        self.assertNotIn("credential_id", [r[1] for r in self.rows("PRAGMA table_info(instance_settings)")])
        self.assertEqual(self.rows("SELECT id FROM instance_settings"), [(9,)])

    def test_integrity_or_fk_error_is_not_versioned(self):
        seed_current(self.path)
        with connect(self.path) as conn:
            conn.execute("UPDATE credit_records SET credential_id=999")
        with self.assertRaises(migrations.MigrationError):
            self.migrate()
        self.assertNotIn(("schema_migrations",), self.rows("SELECT name FROM sqlite_master"))

    def test_restart_does_not_create_more_backups_or_change_version_time(self):
        seed_current(self.path)
        self.migrate()
        version = self.rows("SELECT * FROM schema_migrations")
        backups = list((self.path.parent / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(version[0][3], str(backups[0]))
        verify_backup(backups[0], version[0][4])
        self.migrate()
        self.assertEqual(self.rows("SELECT * FROM schema_migrations"), version)
        self.assertEqual(list((self.path.parent / "backups").glob("*.db")), backups)

    def test_startup_failure_does_not_seed_or_start_scheduler(self):
        import asyncio
        from app import main

        async def enter():
            async with main.lifespan(main.app):
                self.fail("startup must not yield")

        with patch.object(main, "init_db", side_effect=migrations.MigrationError("injected")), patch.object(main, "seed_admin") as seed, patch.object(main, "start_scheduler") as scheduler:
            with self.assertRaises(migrations.MigrationError):
                asyncio.run(enter())
            seed.assert_not_called()
            scheduler.assert_not_called()

    def test_upgrade_snapshot_includes_uncheckpointed_wal(self):
        seed_current(self.path, unique=True)
        writer = sqlite3.connect(self.path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE aws_credentials SET quota_message='committed in WAL'")
            writer.commit()
            self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 0)
            self.migrate()
            record = self.rows("SELECT backup_path,backup_sha256 FROM schema_migrations")[0]
            verify_backup(Path(record[0]), record[1])
            with connect(record[0]) as conn:
                self.assertEqual(conn.execute("SELECT quota_message FROM aws_credentials").fetchall(), [("committed in WAL",)])
            self.assertEqual(self.rows("SELECT quota_message FROM aws_credentials"), [("committed in WAL",)])
        finally:
            writer.close()


    def test_rebuild_refuses_unknown_inline_unique_constraints_without_loss(self):
        with connect(self.path) as conn:
            conn.executescript(LEGACY.replace("account_label VARCHAR(128)", "account_label VARCHAR(128) UNIQUE"))
        before = self.rows("SELECT sql FROM sqlite_master WHERE name='aws_credentials'")
        with self.assertRaises(migrations.MigrationError):
            self.migrate()
        self.assertEqual(self.rows("SELECT sql FROM sqlite_master WHERE name='aws_credentials'"), before)

    def test_rebuild_preserves_explicit_indexes_triggers_and_delete_rules(self):
        seed_current(self.path, unique=True)
        with connect(self.path) as conn:
            conn.execute("CREATE INDEX custom_quota ON aws_credentials(quota_region)")
            conn.execute("CREATE TABLE notices (id INTEGER)")
            conn.execute("CREATE TRIGGER credential_insert AFTER INSERT ON aws_credentials BEGIN INSERT INTO notices VALUES (new.id); END")
        self.migrate()
        with connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT INTO aws_credentials (id,user_id,access_key_id_enc,secret_access_key_enc,is_default,created_at) VALUES (8,1,'another','secret',0,'2026-01-01')")
            self.assertEqual(conn.execute("SELECT * FROM notices").fetchall(), [(8,)])
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='custom_quota'").fetchone())
            conn.execute("DELETE FROM aws_credentials WHERE id=7")
            self.assertEqual(conn.execute("SELECT credential_id FROM credit_records").fetchall(), [(None,)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM instance_settings").fetchone(), (0,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM traffic_usage").fetchone(), (0,))

    def test_migration_crash_during_ddl_rolls_back_on_next_connection(self):
        with connect(self.path) as conn:
            conn.executescript(LEGACY)
        before = self.rows("SELECT type,name,sql FROM sqlite_master ORDER BY name")
        script = """
import os
from app import database, migrations
real = migrations.rebuild
def crash(conn, table, defaults):
    real(conn, table, defaults)
    os._exit(86)
migrations.rebuild = crash
database.init_db()
"""
        env = dict(os.environ, DATABASE_URL=f"sqlite:///{self.path}")
        result = subprocess.run([sys.executable, "-c", script], env=env, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 86, result.stderr)
        self.assertEqual(self.rows("SELECT type,name,sql FROM sqlite_master ORDER BY name"), before)
        self.migrate()
        self.assertEqual(self.rows("SELECT version FROM schema_migrations"), [(1,), (2,)])

    def test_roundtrip_backup_upgrade_restore_for_three_database_generations(self):
        for kind in ("new", "single", "multi"):
            with self.subTest(kind=kind):
                path = self.path.parent / f"{kind}.db"
                if kind == "new":
                    with connect(path) as conn:
                        conn.execute("PRAGMA user_version=0")
                elif kind == "single":
                    with connect(path) as conn:
                        conn.executescript(LEGACY)
                else:
                    seed_current(path)
                backup = path.with_name(f"{kind}.snapshot.db")
                receipt = snapshot_database(path, backup)
                with connect(path) as conn:
                    original = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                engine = create_engine(f"sqlite:///{path}")
                try:
                    with patch.object(database, "engine", engine):
                        database.init_db()
                finally:
                    engine.dispose()
                restore_database(backup, path, receipt["sha256"], offline=True)
                with connect(path) as conn:
                    self.assertEqual(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(), original)
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                    if kind != "new":
                        self.assertEqual(conn.execute("SELECT id,access_key_id_enc FROM aws_credentials").fetchall(), [(7,"opaque-key")])

    def test_legacy_binding_stays_with_each_panel_user(self):
        with connect(self.path) as conn:
            conn.executescript(LEGACY)
            conn.execute("INSERT INTO users VALUES (2,'beta','opaque-hash',0,12,'2026-01-01')")
            conn.execute("INSERT INTO aws_credentials VALUES (27,2,'beta-key','beta-secret','beta','2026-02-01','2026-01-01')")
            conn.execute("INSERT INTO instance_settings VALUES (29,2,'ap-southeast-1','server',999,0,'beta','2026-01-01')")
        self.migrate()
        self.assertEqual(self.rows("SELECT user_id,credential_id,monthly_limit_gb FROM instance_settings ORDER BY user_id"), [(1,7,123.5),(2,27,999)])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "app.db"
        with connect(self.path) as conn:
            conn.executescript("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT); INSERT INTO facts VALUES (1,'before');")

    def test_backup_includes_committed_wal_excludes_uncommitted_and_does_not_overwrite(self):
        writer = sqlite3.connect(self.path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO facts VALUES (2,'committed WAL')")
            writer.commit()
            writer.execute("INSERT INTO facts VALUES (3,'uncommitted')")
            self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 0)
            output = self.path.parent / "snapshot.db"
            receipt = snapshot_database(self.path, output)
            verify_backup(output, receipt["sha256"])
            with connect(output) as conn:
                self.assertEqual(conn.execute("SELECT * FROM facts").fetchall(), [(1, "before"), (2, "committed WAL")])
            with self.assertRaises(DatabaseRecoveryError):
                snapshot_database(self.path, output)
        finally:
            writer.rollback()
            writer.close()

    def test_restore_reverts_data_preserves_safety_backup_and_requires_offline(self):
        backup = self.path.parent / "snapshot.db"
        receipt = snapshot_database(self.path, backup)
        with connect(self.path) as conn:
            conn.execute("INSERT INTO facts VALUES (2,'after')")
        with self.assertRaises(DatabaseRecoveryError):
            restore_database(backup, self.path, receipt["sha256"])
        restored = restore_database(backup, self.path, receipt["sha256"], offline=True)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM facts").fetchall(), [(1, "before")])
        with connect(restored["safety_backup"]["path"]) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM facts").fetchone(), (2,))

    def test_restore_rejects_hash_mismatch_corruption_and_same_path(self):
        backup = self.path.parent / "snapshot.db"
        receipt = snapshot_database(self.path, backup)
        initial = file_sha256(self.path)
        for digest in ("bad", "0" * 64):
            with self.assertRaises(DatabaseRecoveryError):
                restore_database(backup, self.path, digest, offline=True)
        with self.assertRaises(DatabaseRecoveryError):
            restore_database(backup, backup, receipt["sha256"], offline=True)
        backup.write_bytes(b"not a sqlite database")
        with self.assertRaises((DatabaseRecoveryError, sqlite3.Error)):
            restore_database(backup, self.path, file_sha256(backup), offline=True)
        self.assertEqual(file_sha256(self.path), initial)

    def test_restore_to_new_database_and_wal_destination(self):
        backup = self.path.parent / "snapshot.db"
        receipt = snapshot_database(self.path, backup)
        target = self.path.parent / "new.db"
        restore_database(backup, target, receipt["sha256"], offline=True)
        with connect(target) as conn:
            self.assertEqual(conn.execute("SELECT * FROM facts").fetchall(), [(1, "before")])
        with connect(self.path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("INSERT INTO facts VALUES (2,'after')")
        restore_database(backup, self.path, receipt["sha256"], offline=True)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM facts").fetchall(), [(1, "before")])

    def test_restore_rejects_active_wal_reader_without_removing_sidecars(self):
        backup = self.path.parent / "snapshot.db"
        receipt = snapshot_database(self.path, backup)
        writer = sqlite3.connect(self.path)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO facts VALUES (2,'keep')")
            writer.commit()
            writer.execute("BEGIN")
            writer.execute("SELECT * FROM facts").fetchall()
            writer.execute("INSERT INTO facts VALUES (3,'newer')")
            writer.commit()
            reader = sqlite3.connect(self.path)
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT * FROM facts").fetchall()
                writer.execute("INSERT INTO facts VALUES (4,'newest')")
                writer.commit()
                with self.assertRaises(DatabaseRecoveryError):
                    restore_database(backup, self.path, receipt["sha256"], offline=True)
                self.assertTrue(Path(str(self.path) + "-wal").exists())
                self.assertEqual(writer.execute("SELECT COUNT(*) FROM facts").fetchone(), (4,))
            finally:
                reader.close()
        finally:
            writer.close()


if __name__ == "__main__":
    unittest.main()
