"""Versioned SQLite startup migration. All DDL and the ledger share one transaction."""
import hashlib
import json
import logging
import os
import re
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DefaultClause, MetaData, Text, UniqueConstraint, text
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import sqlite

from app.sqlite_backup import check_database, snapshot_database, sync_directory, verify_migration_snapshot

SCHEMA_VERSION = 2
LEGACY_UNIQUE_KEYS = {
    "aws_credentials": ("user_id",),
    "instance_settings": ("user_id", "region", "instance_name"),
    "traffic_usage": ("user_id", "region", "instance_name", "year_month"),
}
SUPPORTED_REBUILD_UNIQUES = {
    "traffic_usage": {
        ("user_id", "credential_id", "region", "instance_name", "year_month"),
        ("user_id", "credential_id", "region", "instance_name", "year_month", "account_id"),
    },
}
LEGACY_ZERO_DEFAULTS = {('aws_credentials', 'is_default'), ('aws_credentials', 'auto_stop_conflict'),
                        ('instance_settings', 'auto_stop_on_limit'), ('instance_settings', 'auto_stop_conflict'),
                        ('instance_settings', 'auto_stop_configured'),
                        ('traffic_usage', 'network_in_bytes'), ('traffic_usage', 'network_out_bytes')}
LEGACY_NULLABLE_DATES = {('aws_credentials', 'created_at'), ('instance_settings', 'created_at'),
                         ('traffic_usage', 'last_synced_at')}
logger = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    pass


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({quote(table)})")}


def unique_columns(conn, table):
    return [(row[1], [col[2] for col in conn.execute(f"PRAGMA index_info({quote(row[1])})")])
            for row in conn.execute(f"PRAGMA index_list({quote(table)})") if row[2] and not row[4]]


def legacy_unique_indexes(conn, table):
    identity = LEGACY_UNIQUE_KEYS[table]
    found = set()
    for _, name, unique, _, partial in conn.execute(f"PRAGMA index_list({quote(table)})"):
        if not unique:
            continue
        keys = [row for row in conn.execute(f"PRAGMA index_xinfo({quote(name)})") if row[5]]
        names = tuple(row[2] for row in keys)
        if set(names) != set(identity):
            continue
        if partial or names != identity or any(row[3] or row[4].upper() != "BINARY" for row in keys):
            raise MigrationError(f"{table}: nonstandard unique index {name} requires manual review")
        found.add(name)
    return found


def model_foreign_keys(table):
    return {(col.name, fk.column.table.name, fk.column.name, (fk.onupdate or "NO ACTION").upper(),
             (fk.ondelete or "NO ACTION").upper()) for col in table.columns for fk in col.foreign_keys}


def foreign_keys(conn, table):
    return [(row[3], row[2], row[4], row[5].upper(), row[6].upper())
            for row in conn.execute(f"PRAGMA foreign_key_list({quote(table)})")]


def create_table(conn, table, name=None):
    ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    if name:
        original = str(table.name)
        # The temporary name only changes CREATE TABLE, never FK references.
        ddl = ddl.replace(f"CREATE TABLE {original} ", f"CREATE TABLE {quote(name)} ", 1)
    conn.execute(ddl)


def rebuild_definition(conn, table, allowed_unique_indexes=()):
    info = conn.execute(f"PRAGMA table_xinfo({quote(table.name)})").fetchall()
    old_cols = {row[1] for row in info}
    if old_cols - set(table.columns.keys()) or any(row[6] for row in info):
        raise MigrationError(f"{table.name}: unknown columns require manual review")
    table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table.name,)).fetchone()[0]
    if re.search(r"\b(CHECK|COLLATE|GENERATED|AUTOINCREMENT|DEFERRABLE|MATCH|STRICT|WITHOUT|CONFLICT)\b",
                 table_sql, re.IGNORECASE):
        raise MigrationError(f"{table.name}: custom table constraints require manual review")
    indexes = conn.execute(f"PRAGMA index_list({quote(table.name)})").fetchall()
    pk = [(row[1], row[2].upper()) for row in info if row[5]]
    if pk != [("id", "INTEGER")] or any(row[3] == "pk" for row in indexes):
        raise MigrationError(f"{table.name}: expected id to be the INTEGER PRIMARY KEY rowid alias")
    actual = foreign_keys(conn, table.name)
    if (len(actual) != len(set(actual)) or not set(actual) <= model_foreign_keys(table)
            or any(row[1] for row in conn.execute(f"PRAGMA foreign_key_list({quote(table.name)})"))):
        raise MigrationError(f"{table.name}: custom foreign key requires manual review")
    removed = legacy_unique_indexes(conn, table.name)
    allowed_unique_indexes = {tuple(index) for index in allowed_unique_indexes}
    for index in indexes:
        if not index[2] or index[1] in removed:
            continue
        keys = [row for row in conn.execute(f"PRAGMA index_xinfo({quote(index[1])})") if row[5]]
        names = tuple(row[2] for row in keys)
        if names in allowed_unique_indexes and not index[4] and not any(
            row[3] or row[4].upper() != "BINARY" for row in keys
        ):
            removed.add(index[1])
        elif index[3] == "u" or any(set(names) == set(candidate) for candidate in allowed_unique_indexes):
            raise MigrationError(f"{table.name}: custom unique constraint requires manual review")
        # Other explicit indexes are replayed unchanged by rebuild().
    if any(row[3] == "u" and row[1] not in removed for row in indexes):
        raise MigrationError(f"{table.name}: custom unique constraint requires manual review")
    # Never mutate the shared ORM metadata. Only these known historical differences
    # are supported, and the replacement must retain their actual SQL semantics.
    schema = MetaData()
    for parent in {fk.column.table for fk in table.foreign_keys}:
        parent.to_metadata(schema)
    target = table.to_metadata(schema)
    for _, name, declared, not_null, default, _, _ in info:
        col = target.c[name]
        key = (table.name, name)
        if key == ('instance_settings', 'note') and declared.upper() == 'TEXT':
            col.type = Text()
        if declared.upper() != col.type.compile(dialect=sqlite.dialect()).upper():
            raise MigrationError(f"{table.name}.{name}: custom column type requires manual review")
        if not not_null and key in LEGACY_NULLABLE_DATES:
            col.nullable = True
        if not col.primary_key and bool(not_null) != (not col.nullable):
            raise MigrationError(f"{table.name}.{name}: custom nullability requires manual review")
        if default is not None:
            if key not in LEGACY_ZERO_DEFAULTS or default != '0':
                raise MigrationError(f"{table.name}.{name}: custom SQL default requires manual review")
            col.server_default = DefaultClause(text('0'))
    return target, old_cols, removed


def rebuild(conn, definition, defaults):
    table, old_cols, removed = definition
    temp = f"_t01_{table.name}"
    if temp in tables(conn):
        raise MigrationError(f"{table.name}: unexpected temporary table")
    saved = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table.name,)).fetchall()
    create_table(conn, table, temp)
    names, values = [], []
    for col in table.columns:
        names.append(quote(col.name))
        if col.name in old_cols:
            values.append(f"o.{quote(col.name)}")
        elif col.name in defaults:
            values.append(defaults[col.name])
        elif col.nullable:
            values.append("NULL")
        else:
            raise MigrationError(f"{table.name}: missing required column {col.name}")
    count = conn.execute(f"SELECT COUNT(*) FROM {quote(table.name)}").fetchone()[0]
    conn.execute(f"INSERT INTO {quote(temp)} ({','.join(names)}) SELECT {','.join(values)} FROM {quote(table.name)} o")
    if conn.execute(f"SELECT COUNT(*) FROM {quote(temp)}").fetchone()[0] != count:
        raise MigrationError(f"{table.name}: row count changed")
    # Drop old parent with FK checks disabled, then rename the replacement.
    # RENAME of the original parent would rewrite child FK destinations to the old name.
    conn.execute(f"DROP TABLE {quote(table.name)}")
    conn.execute(f"ALTER TABLE {quote(temp)} RENAME TO {quote(table.name)}")
    for kind, name, ddl in saved:
        if kind != "index" or name not in removed:
            conn.execute(ddl)


def add_columns(conn, table, definitions):
    existing = columns(conn, table)
    for name, definition in definitions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} {definition}")


def apply_v2(conn, metadata):
    existing = tables(conn)
    if "aws_credentials" in existing:
        add_columns(conn, "aws_credentials", {"account_id": "VARCHAR(12)"})
        add_columns(conn, "aws_credentials", {"last_confirmed_account_id": "VARCHAR(12)"})
        add_columns(conn, "aws_credentials", {"auto_stop_conflict": "BOOLEAN DEFAULT 0 NOT NULL"})
        conn.execute("CREATE INDEX IF NOT EXISTS ix_aws_credentials_account_id ON aws_credentials (account_id)")
    if "instance_settings" in existing:
        add_columns(conn, "instance_settings", {
            "auto_stop_conflict": "BOOLEAN DEFAULT 0 NOT NULL",
            "auto_stop_configured": "BOOLEAN DEFAULT 0 NOT NULL",
            "auto_stop_account_id": "VARCHAR(12)",
        })
        # T01 behavior treated every persisted row as an explicit override, including False.
        conn.execute("UPDATE instance_settings SET auto_stop_configured=1")
    if "traffic_usage" in existing and "account_id" not in columns(conn, "traffic_usage"):
        definition = rebuild_definition(conn, metadata.tables["traffic_usage"], {
            ("user_id", "credential_id", "region", "instance_name", "year_month"),
        })
        rebuild(conn, definition, {"account_id": "''"})


def apply_v1(conn, metadata, rebuilds):
    existing = tables(conn)
    if "users" in existing:
        add_columns(conn, "users", {"auto_stop_on_limit_default": "BOOLEAN DEFAULT 0 NOT NULL",
                                   "token_version": "INTEGER DEFAULT 0 NOT NULL"})
    if "aws_credentials" in existing:
        if "aws_credentials" in rebuilds:
            rebuild(conn, rebuilds["aws_credentials"], {
                "is_default": "1", "auto_stop_conflict": "0"
            })
        else:
            add_columns(conn, "aws_credentials", {
                "is_default": "BOOLEAN DEFAULT 0 NOT NULL", "proxy_url_enc": "TEXT",
                "vcpu_quota": "FLOAT", "vcpu_tier": "VARCHAR(32)", "static_ip_quota": "FLOAT",
                "used_vcpu": "FLOAT", "used_instance_count": "INTEGER", "quota_region": "VARCHAR(32)",
                "quota_message": "TEXT", "quota_checked_at": "DATETIME"})
    for name in ("instance_settings", "traffic_usage"):
        if name not in existing:
            continue
        if name in rebuilds:
            ambiguous = None
            if "credential_id" not in rebuilds[name][1]:
                ambiguous = conn.execute(f"""SELECT o.id FROM {quote(name)} o
                    WHERE (SELECT COUNT(*) FROM aws_credentials c WHERE c.user_id=o.user_id) != 1 LIMIT 1""").fetchone()
            if ambiguous:
                raise MigrationError(f"{name}: legacy credential ownership is missing or ambiguous")
            defaults = {
                "credential_id": "(SELECT c.id FROM aws_credentials c WHERE c.user_id=o.user_id)",
                "auto_stop_on_limit": "0", "auto_stop_conflict": "0",
                "auto_stop_configured": "1"}
            if name == "traffic_usage":
                defaults["account_id"] = "''"
            rebuild(conn, rebuilds[name], defaults)
        elif name == "instance_settings":
            add_columns(conn, name, {"auto_stop_on_limit": "BOOLEAN DEFAULT 0 NOT NULL"})
    if "credit_records" in existing:
        # Historical manual records remain unassigned; T03 resolves ownership explicitly.
        add_columns(conn, "credit_records", {"credential_id": "INTEGER REFERENCES aws_credentials(id) ON DELETE SET NULL"})
    for table in metadata.sorted_tables:
        if table.name not in tables(conn):
            create_table(conn, table)
        for index in table.indexes:
            conn.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=sqlite.dialect())))


def validate_schema(conn, metadata, ignored_missing=None):
    ignored_missing = ignored_missing or {}
    for table in metadata.sorted_tables:
        required_columns = set(table.columns.keys()) - ignored_missing.get(table.name, set())
        if not required_columns <= columns(conn, table.name):
            raise MigrationError(f"{table.name}: schema columns do not match this application")
        uniques = {tuple(keys) for _, keys in unique_columns(conn, table.name)}
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                names = tuple(col.name for col in constraint.columns)
                missing_identity_columns = set(names) & ignored_missing.get(table.name, set())
                if missing_identity_columns:
                    legacy_names = tuple(name for name in names if name not in missing_identity_columns)
                    if legacy_names not in uniques:
                        raise MigrationError(f"{table.name}: legacy unique constraint is missing")
                    continue
                if names not in uniques:
                    raise MigrationError(f"{table.name}: required unique constraint is missing")
        for col in table.columns:
            if col.unique and (col.name,) not in uniques:
                raise MigrationError(f"{table.name}: required unique column constraint is missing")
        if not model_foreign_keys(table) <= set(foreign_keys(conn, table.name)):
            raise MigrationError(f"{table.name}: foreign key definition does not match")
        if table.name in LEGACY_UNIQUE_KEYS and not ignored_missing.get(table.name):
            legacy_keys = LEGACY_UNIQUE_KEYS[table.name]
            if any(tuple(keys) == legacy_keys for _, keys in unique_columns(conn, table.name)):
                raise MigrationError(f"{table.name}: obsolete single-credential unique constraint remains")
    check_database(conn)


def validate_ledger(conn):
    layout = {row[1]: (row[2].upper(), row[3], row[5]) for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    if layout != {"version": ("INTEGER", 0, 1), "name": ("TEXT", 1, 0), "applied_at_utc": ("TEXT", 1, 0),
                  "backup_path": ("TEXT", 0, 0), "backup_sha256": ("TEXT", 0, 0)}:
        raise MigrationError("Invalid migration ledger schema; review the original database and backup")
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    if not versions or versions != list(range(1, versions[-1] + 1)) or versions[-1] > SCHEMA_VERSION:
        raise MigrationError("Unsupported migration version; use the paired application image")
    return versions[-1]


def preflight_rebuilds(conn, metadata, existing):
    definitions = {}
    for name in LEGACY_UNIQUE_KEYS:
        if name not in existing:
            continue
        rebuild_needed = (
            bool(legacy_unique_indexes(conn, name)) if name == 'aws_credentials'
            else ('credential_id' not in columns(conn, name) if name != "traffic_usage"
                  else 'credential_id' not in columns(conn, name) or 'account_id' not in columns(conn, name))
        )
        if rebuild_needed:
            definitions[name] = rebuild_definition(
                conn, metadata.tables[name], SUPPORTED_REBUILD_UNIQUES.get(name, ())
            )
    return definitions


def migration_backup(conn, source, open_files, version=1):
    directory = source.parent / 'backups'
    stem = f'{source.name}.pre-v{version}'
    receipt = directory / (stem + '.json')
    if directory.is_symlink() or directory.is_junction():
        raise MigrationError(f'Invalid migration backup directory: {directory}')
    sources = [source]
    wal = Path(str(source) + '-wal')
    if conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal' and wal.is_file() and wal.stat().st_size:
        sources.append(wal)
    # On POSIX, closing a raw descriptor for the live DB drops the process's SQLite locks.
    fingerprints = [hashlib.file_digest(open_files.enter_context(path.open('rb')), 'sha256').hexdigest()
                    for path in sources]
    if os.path.lexists(receipt):
        if receipt.is_symlink() or not receipt.is_file():
            raise MigrationError(f'Invalid migration backup receipt: {receipt}')
        state = json.loads(receipt.read_text(encoding='utf-8'))
        checksum = state['backup_sha256']
        recorded = state['source_files_sha256']
        if (not isinstance(recorded, list) or len(recorded) not in (1, 2)
                or not all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
                           for value in [*recorded, checksum])):
            raise MigrationError(f'Invalid migration backup receipt hashes: {receipt}')
        previous = verify_migration_snapshot(directory / f'{stem}-{checksum}.db', checksum, source)
        if recorded == fingerprints:
            return previous
    backup = snapshot_database(source, directory / (stem + '.db'), by_content=True)
    fd, name = tempfile.mkstemp(prefix='.migration-receipt-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump({'source_files_sha256': fingerprints, 'backup_sha256': backup['sha256']}, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, receipt)
        sync_directory(directory)
    finally:
        Path(name).unlink(missing_ok=True)
    return backup


def upgrade_database(engine, metadata: MetaData) -> None:
    raw = engine.raw_connection()
    conn = raw.driver_connection
    open_files = ExitStack()
    backup = None
    try:
        # SQLite ignores foreign_keys changes inside a transaction.
        conn.rollback()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        existing = tables(conn)
        if "schema_migrations" in existing:
            current_version = validate_ledger(conn)
            if current_version == 1:
                validate_schema(conn, metadata, {
                    "aws_credentials": {"account_id", "last_confirmed_account_id", "auto_stop_conflict"},
                    "instance_settings": {"auto_stop_conflict", "auto_stop_configured", "auto_stop_account_id"},
                    "traffic_usage": {"account_id"},
                })
                path = engine.url.database
                if not path or path == ":memory:":
                    backup = None
                else:
                    if engine.url.query:
                        raise MigrationError("SQLite URI options are unsupported for automatic migration backup")
                    source = Path(path).resolve()
                    backup = migration_backup(conn, source, open_files, version=2)
                    logger.info("Migration v2 backup: %s SHA256=%s", backup["path"], backup["sha256"])
                apply_v2(conn, metadata)
                validate_schema(conn, metadata)
                conn.execute("INSERT INTO schema_migrations VALUES (?,?,?,?,?)", (
                    2, "T02 account identity and policy conflicts", datetime.now(timezone.utc).isoformat(),
                    backup["path"] if backup else None, backup["sha256"] if backup else None))
            else:
                validate_schema(conn, metadata)
            conn.commit()
            return
        check_database(conn)
        rejection = None
        try:
            rebuilds = preflight_rebuilds(conn, metadata, existing)
        except MigrationError as exc:
            rejection = exc
        path = engine.url.database
        if existing and path and path != ":memory:":
            if engine.url.query:
                raise MigrationError("SQLite URI options are unsupported for automatic migration backup")
            source = Path(path).resolve()
            backup = migration_backup(conn, source, open_files)
            logger.info("Migration backup: %s SHA256=%s", backup["path"], backup["sha256"])
        if rejection is not None:
            raise rejection
        conn.execute("""CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at_utc TEXT NOT NULL,
            backup_path TEXT, backup_sha256 TEXT)""")
        apply_v1(conn, metadata, rebuilds)
        apply_v2(conn, metadata)
        validate_schema(conn, metadata)
        conn.execute("INSERT INTO schema_migrations VALUES (?,?,?,?,?)", (
            1, "T01 baseline schema", datetime.now(timezone.utc).isoformat(),
            backup["path"] if backup else None, backup["sha256"] if backup else None))
        t02_backup = backup
        conn.execute("INSERT INTO schema_migrations VALUES (?,?,?,?,?)", (
            2, "T02 account identity and policy conflicts", datetime.now(timezone.utc).isoformat(),
            t02_backup["path"] if t02_backup else None, t02_backup["sha256"] if t02_backup else None))
        conn.commit()
    except BaseException as exc:
        conn.rollback()
        logger.error("Database migration failed; startup stopped. Backup: %s", backup["path"] if backup else "none")
        raise MigrationError("SQLite migration failed; startup stopped") from exc
    finally:
        open_files.close()
        conn.execute("PRAGMA foreign_keys=ON")
        raw.close()
