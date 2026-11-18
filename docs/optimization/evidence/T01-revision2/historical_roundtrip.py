"""Create model-derived historical schemas, then test upgrade, restart and rollback."""
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase


def create_historical(model, path):
    class HistoricalBase(DeclarativeBase):
        pass

    sys.modules['app'] = ModuleType('app')
    historical_db = ModuleType('app.database')
    historical_db.Base = HistoricalBase
    sys.modules['app.database'] = historical_db
    spec = importlib.util.spec_from_file_location('historical_models', model)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    engine = create_engine(f'sqlite:///{path}')
    metadata = HistoricalBase.metadata

    def add(conn, table_name, **data):
        table = metadata.tables[table_name]
        conn.execute(table.insert().values(**{key: value for key, value in data.items() if key in table.c}))

    try:
        metadata.create_all(engine)
        with engine.begin() as conn:
            for user in (1, 2):
                add(conn, 'users', id=user, username=f'user{user}', password_hash=f'dummy{user}', is_admin=False)
            for user, credential in ((1, 7), (1, 8), (2, 27)):
                add(conn, 'aws_credentials', id=credential, user_id=user, access_key_id_enc=f'opaque{credential}',
                    secret_access_key_enc=f'secret{credential}', is_default=credential != 8, proxy_url_enc=f'proxy{credential}',
                    used_vcpu=3.5, quota_region='same', account_label=f'label{credential}')
                add(conn, 'instance_settings', id=credential, user_id=user, credential_id=credential, region='same',
                    instance_name='same', monthly_limit_gb=credential, auto_stop_on_limit=credential == 8)
                add(conn, 'traffic_usage', id=credential, user_id=user, credential_id=credential, region='same',
                    instance_name='same', year_month='2026-11', network_in_bytes=credential, network_out_bytes=2 * credential)
            add(conn, 'operation_logs', user_id=2, action='synthetic', status='success')
            if 'credit_records' in metadata.tables:
                add(conn, 'credit_records', user_id=1, credential_id=None, name='unassigned',
                    total_amount=25.5, currency='USD', start_date=date(2026, 1, 1))
    finally:
        engine.dispose()


def sample(path):
    with closing(sqlite3.connect(path)) as conn:
        result = {}
        schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        for kind, name, _, _ in schema:
            if kind == 'table':
                cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')]
                result[name] = [dict(zip(cols, values)) for values in conn.execute(f'SELECT * FROM "{name}" ORDER BY 1')]
        return schema, result


def exercise(root, model, path):
    from app import database, migrations
    from app.sqlite_backup import restore_database, verify_backup
    subprocess.run([sys.executable, __file__, 'create', str(model), str(path)], check=True)
    original_schema, original = sample(path)
    engine = create_engine(f'sqlite:///{path}')
    try:
        for _ in range(2):
            migrations.upgrade_database(engine, database.Base.metadata)
    finally:
        engine.dispose()
    _, migrated = sample(path)
    assert len(migrated['schema_migrations']) == 1
    ledger = migrated['schema_migrations'][0]
    assert ledger['version'] == 1 and len(list((path.parent / 'backups').glob('*.db'))) == 1
    verify_backup(Path(ledger['backup_path']), ledger['backup_sha256'])
    assert sample(ledger['backup_path']) == (original_schema, original)
    for name, before_rows in original.items():
        assert len(before_rows) == len(migrated[name]), name
        for before, after in zip(before_rows, migrated[name]):
            assert all(after[col] == value for col, value in before.items()), name
    with closing(sqlite3.connect(path)) as conn:
        assert list(conn.execute('PRAGMA foreign_key_check')) == []
        conn.execute("UPDATE aws_credentials SET account_label='after-upgrade' WHERE id=27")
        conn.commit()
    restored = restore_database(Path(ledger['backup_path']), path, ledger['backup_sha256'], offline=True)
    safety = restored['safety_backup']
    verify_backup(Path(safety['path']), safety['sha256'])
    with closing(sqlite3.connect(safety['path'])) as conn:
        assert conn.execute('SELECT account_label FROM aws_credentials WHERE id=27').fetchone() == ('after-upgrade',)
    assert sample(path) == (original_schema, original)
    return {'model': model.stem, 'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
            'original_rows_checked': {table: len(rows) for table, rows in original.items()},
            'upgraded': True, 'single_backup_after_restart': True, 'original_schema_and_values_restored': True,
            'safety_snapshot_has_later_write': True}


if __name__ == '__main__':
    if sys.argv[1] == 'create':
        create_historical(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        root = Path(sys.argv[1]).resolve()
        backend = root / 'source' / 'backend'
        sys.path[:0] = [str(backend), str(backend / 'tests')]
        import test_migrations
        with tempfile.TemporaryDirectory() as directory:
            results = []
            for model in sorted((root / 'historical-models').glob('*.py')):
                case = Path(directory) / model.stem
                case.mkdir()
                results.append(exercise(root, model, case / 'app.db'))
        print(json.dumps(results, indent=2))
