"""Independent T01 probes. Product source is imported without modification."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    backend = args.source.resolve() / "backend"
    sys.path[:0] = [str(backend), str(backend / "tests")]
    for name in ("SECRET_KEY", "ENCRYPTION_KEY", "ADMIN_PASSWORD", "DATABASE_URL"):
        os.environ.pop(name, None)
    from test_migrations import LEGACY, seed_current
    from sqlalchemy import create_engine
    from app import database
    from app.sqlite_backup import file_sha256, snapshot_database

    results = []

    def migrate(path):
        engine = create_engine(f"sqlite:///{path}")
        try:
            with patch.object(database, "engine", engine):
                database.init_db()
        finally:
            engine.dispose()

    def rows(path, sql):
        with closing(sqlite3.connect(path)) as conn:
            return conn.execute(sql).fetchall()

    def execute(path, sql):
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(sql)

    def attempt(path):
        try:
            migrate(path)
            return {"accepted": True}
        except Exception as exc:
            return {"accepted": False, "error": str(exc), "cause": str(exc.__cause__)}

    with tempfile.TemporaryDirectory(prefix="audit-t01-") as name:
        root = Path(name)

        # A valid v1 database whose ledger has lost its recovery metadata.
        path = root / "ledger.db"
        seed_current(path)
        migrate(path)
        execute(path, """DROP TABLE schema_migrations;
            CREATE TABLE schema_migrations(version INTEGER);
            INSERT INTO schema_migrations VALUES(1);""")
        result = {"probe": "ledger_required_columns", **attempt(path)}
        result["ledger_columns"] = rows(path, "PRAGMA table_info(schema_migrations)")
        results.append(result)

        # An old inline FK must be preserved, or the custom schema rejected.
        path = root / "custom-fk.db"
        extra = "FOREIGN KEY(account_label) REFERENCES allowed_labels(label) ON DELETE RESTRICT"
        legacy = LEGACY.replace(
            "FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);",
            f"FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, {extra});",
            1,
        )
        execute(path, """CREATE TABLE allowed_labels(label TEXT PRIMARY KEY);
            INSERT INTO allowed_labels VALUES('alpha');""" + legacy)
        before = rows(path, "PRAGMA foreign_key_list(aws_credentials)")
        result = {"probe": "custom_inline_foreign_key", "before": before, **attempt(path)}
        result["after"] = rows(path, "PRAGMA foreign_key_list(aws_credentials)")
        if result["accepted"]:
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                try:
                    conn.execute("UPDATE aws_credentials SET account_label='not-approved'")
                    conn.commit()
                    result["unapproved_label_accepted"] = True
                except sqlite3.IntegrityError as exc:
                    result["unapproved_label_accepted"] = False
                    result["constraint_error"] = str(exc)
        results.append(result)

        # A legacy uniqueness rule expressed as an explicit index.
        path = root / "legacy-explicit-unique.db"
        execute(path, LEGACY + """
            CREATE UNIQUE INDEX old_instance_identity
            ON instance_settings(user_id,region,instance_name);
            CREATE UNIQUE INDEX old_traffic_identity
            ON traffic_usage(user_id,region,instance_name,year_month);""")
        result = {"probe": "legacy_explicit_unique_index", **attempt(path)}
        if result["accepted"]:
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("""INSERT INTO aws_credentials
                    (id,user_id,access_key_id_enc,secret_access_key_enc,is_default,created_at)
                    VALUES(8,1,'other-key','other-secret',0,'2026-01-01')""")
                conn.commit()
                try:
                    conn.execute("""INSERT INTO instance_settings
                        (id,user_id,credential_id,region,instance_name,auto_stop_on_limit,created_at)
                        VALUES(10,1,8,'ap-southeast-1','server',0,'2026-01-01')""")
                    conn.commit()
                    result["second_credential_same_instance_accepted"] = True
                except sqlite3.IntegrityError as exc:
                    result["second_credential_same_instance_accepted"] = False
                    result["insert_error"] = str(exc)
                try:
                    conn.execute("""INSERT INTO traffic_usage
                        (id,user_id,credential_id,region,instance_name,year_month,
                         network_in_bytes,network_out_bytes,last_synced_at)
                        VALUES(12,1,8,'ap-southeast-1','server','2026-11',1,2,'2026-11-18')""")
                    conn.commit()
                    result["second_credential_same_traffic_accepted"] = True
                except sqlite3.IntegrityError as exc:
                    result["second_credential_same_traffic_accepted"] = False
                    result["traffic_insert_error"] = str(exc)
        result["indexes"] = rows(path, "PRAGMA index_list(instance_settings)")
        results.append(result)

        # AUTOINCREMENT's recorded high-water mark is not in ordinary row data.
        path = root / "autoincrement.db"
        legacy = LEGACY.replace(
            "aws_credentials (id INTEGER PRIMARY KEY,",
            "aws_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT,",
        )
        execute(path, legacy + """
            INSERT INTO aws_credentials
            VALUES(100,2,'deleted-key','deleted-secret','gone',NULL,NULL);
            DELETE FROM aws_credentials WHERE id=100;""")
        result = {"probe": "custom_autoincrement", "sequence_before": rows(
            path, "SELECT name,seq FROM sqlite_sequence"), **attempt(path)}
        if result["accepted"]:
            execute(path, """INSERT INTO aws_credentials
                (user_id,access_key_id_enc,secret_access_key_enc,is_default,created_at)
                VALUES(1,'new-key','new-secret',0,'2026-01-01');""")
            result["new_id"] = rows(path, "SELECT MAX(id) FROM aws_credentials")[0][0]
            result["table_sql"] = rows(
                path, "SELECT sql FROM sqlite_master WHERE name='aws_credentials'")[0][0]
        results.append(result)

        # Snapshot output paths with sidecars must be treated as occupied.
        source = root / "snapshot-source.db"
        output = root / "occupied-output.db"
        execute(source, """PRAGMA journal_mode=WAL;
            CREATE TABLE facts(id INTEGER PRIMARY KEY,value TEXT);
            INSERT INTO facts VALUES(1,'trusted');""")
        with closing(sqlite3.connect(output)) as old_writer:
            old_writer.execute("PRAGMA journal_mode=WAL")
            old_writer.execute("PRAGMA wal_autocheckpoint=0")
            old_writer.executescript("""CREATE TABLE facts(id INTEGER PRIMARY KEY,value TEXT);
                INSERT INTO facts VALUES(1,'unrelated-old-data');""")
            old_wal = Path(str(output) + "-wal").read_bytes()
        output.unlink()
        Path(str(output) + "-wal").write_bytes(old_wal)
        try:
            argv = [sys.executable, "-m", "app.sqlite_backup", "backup",
                    "--database", str(source), "--output", str(output)]
            cli = subprocess.run(argv, cwd=backend, capture_output=True, text=True, timeout=40)
            receipt = json.loads(cli.stdout)
            if cli.returncode:
                raise RuntimeError(receipt)
            result = {"probe": "snapshot_output_sidecar", "accepted": True,
                      "cli_exit_code": cli.returncode, "cli_receipt": receipt,
                      "sha256_matches_main_file": receipt["sha256"] == file_sha256(output),
                      "sidecar_still_present": Path(str(output) + "-wal").exists()}
            result["source_rows"] = rows(source, "SELECT * FROM facts")
            result["snapshot_rows_on_open"] = rows(output, "SELECT * FROM facts")
            result["hash_still_matches_after_open"] = receipt["sha256"] == file_sha256(output)
        except Exception as exc:
            result = {"probe": "snapshot_output_sidecar", "accepted": False, "error": str(exc)}
        results.append(result)

    data = {"head": "7acd675ef41d5200c55b0148575d97fac0df2e9f",
            "python": sys.version, "sqlite": sqlite3.sqlite_version, "observations": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
