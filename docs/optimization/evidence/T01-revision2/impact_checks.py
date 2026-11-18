"""Observe migration retry costs and column semantics in disposable databases."""
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def observe(source):
    sys.path[:0] = [str(source / 'backend'), str(source / 'backend' / 'tests')]
    from test_migrations import LEGACY
    from app import database, migrations
    from app.sqlite_backup import verify_backup
    from sqlalchemy import create_engine

    def startup(path):
        engine = create_engine(f'sqlite:///{path}')
        try:
            migrations.upgrade_database(engine, database.Base.metadata)
            return {'accepted': True}
        except migrations.MigrationError as exc:
            return {'accepted': False, 'cause': str(exc.__cause__)}
        finally:
            engine.dispose()

    def exercise_null(path):
        with closing(sqlite3.connect(path)) as conn:
            row = next(col for col in conn.execute('PRAGMA table_info(aws_credentials)') if col[1] == 'account_label')
            try:
                conn.execute('UPDATE aws_credentials SET account_label=NULL WHERE id=7')
                accepted = conn.execute('SELECT account_label FROM aws_credentials WHERE id=7').fetchone() == (None,)
            except sqlite3.IntegrityError:
                accepted = False
            finally:
                conn.rollback()
            return {'declared_not_null': bool(row[3]), 'sql_default': row[4], 'null_write_allowed': accepted}

    with tempfile.TemporaryDirectory(prefix='independent-r1-') as name:
        root = Path(name)
        retry = root / 'retry' / 'app.db'
        retry.parent.mkdir()
        with closing(sqlite3.connect(retry)) as conn:
            conn.executescript(LEGACY.replace('aws_credentials (id INTEGER PRIMARY KEY,',
                                              'aws_credentials (id INTEGER PRIMARY KEY AUTOINCREMENT,'))
        starting_digest = digest(retry)
        attempts = []
        for _ in range(4):
            outcome = startup(retry)
            snapshots = sorted((retry.parent / 'backups').glob('*.db'))
            for snapshot in snapshots:
                verify_backup(snapshot, digest(snapshot))
            with closing(sqlite3.connect(retry)) as conn:
                versions = conn.execute("SELECT count(*) FROM sqlite_schema WHERE name='schema_migrations'").fetchone()[0]
            attempts.append({**outcome, 'ledger_present': bool(versions), 'source_sha256': digest(retry),
                             'source_unchanged': digest(retry) == starting_digest, 'paths': [item.name for item in snapshots],
                             'count': len(snapshots), 'snapshot_bytes': sum(item.stat().st_size for item in snapshots),
                             'digests': [digest(item) for item in snapshots]})
        label = root / 'column' / 'app.db'
        label.parent.mkdir()
        with closing(sqlite3.connect(label)) as conn:
            conn.executescript(LEGACY.replace('account_label VARCHAR(128)', "account_label VARCHAR(128) NOT NULL DEFAULT 'unlabelled'"))
        old_label = exercise_null(label)
        label_hash = digest(label)
        upgraded = startup(label)
        new_label = exercise_null(label)
        with closing(sqlite3.connect(label)) as conn:
            ledger_present = bool(conn.execute("SELECT 1 FROM sqlite_schema WHERE name='schema_migrations'").fetchone())
            if ledger_present:
                version, backup, checksum = conn.execute('SELECT version,backup_path,backup_sha256 FROM schema_migrations').fetchone()
            else:
                version = None
                backup, = (label.parent / 'backups').glob('*.db')
                checksum = digest(backup)
            verify_backup(Path(backup), checksum)
            snapshot_rules = exercise_null(backup)
        return {'python': sys.version.split()[0], 'sqlite': sqlite3.sqlite_version, 'retry': attempts, 'retry_source_sha256_before': starting_digest,
                'retry_source_bytes': retry.stat().st_size, 'column_rule': {'before': old_label, **upgraded, 'after': new_label,
                'ledger_present': ledger_present, 'ledger_version': version, 'source_sha256_before': label_hash,
                'source_sha256_after': digest(label), 'verified_backup_name': Path(backup).name,
                'verified_backup_sha256': checksum, 'verified_backup_rule': snapshot_rules}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    print(json.dumps(observe(args.source.resolve()), indent=2))
