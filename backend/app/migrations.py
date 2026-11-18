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

SCHEMA_VERSION = 1
LEGACY_UNIQUE_KEYS = {
    "aws_credentials": ("user_id",),
    "instance_settings": ("user_id", "region", "instance_name"),
    "traffic_usage": ("user_id", "region", "instance_name", "year_month"),
}
LEGACY_ZERO_DEFAULTS = {('aws_credentials', 'is_default'), ('instance_settings', 'auto_stop_on_limit'),
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


def rebuild_definition(conn, table):
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


def apply_v1(conn, metadata, rebuilds):
    existing = tables(conn)
    if "users" in existing:
        add_columns(conn, "users", {"auto_stop_on_limit_default": "BOOLEAN DEFAULT 0 NOT NULL",
                                   "token_version": "INTEGER DEFAULT 0 NOT NULL"})
    if "aws_credentials" in existing:
        if "aws_credentials" in rebuilds:
            rebuild(conn, rebuilds["aws_credentials"], {"is_default": "1"})
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
            ambiguous = conn.execute(f"""SELECT o.id FROM {quote(name)} o
                WHERE (SELECT COUNT(*) FROM aws_credentials c WHERE c.user_id=o.user_id) != 1 LIMIT 1""").fetchone()
            if ambiguous:
                raise MigrationError(f"{name}: legacy credential ownership is missing or ambiguous")
            rebuild(conn, rebuilds[name], {
                "credential_id": "(SELECT c.id FROM aws_credentials c WHERE c.user_id=o.user_id)",
                "auto_stop_on_limit": "0"})
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


def validate_schema(conn, metadata):
    for table in metadata.sorted_tables:
        if not set(table.columns.keys()) <= columns(conn, table.name):
            raise MigrationError(f"{table.name}: schema columns do not match this application")
        uniques = {tuple(keys) for _, keys in unique_columns(conn, table.name)}
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                if tuple(col.name for col in constraint.columns) not in uniques:
                    raise MigrationError(f"{table.name}: required unique constraint is missing")
        for col in table.columns:
            if col.unique and (col.name,) not in uniques:
                raise MigrationError(f"{table.name}: required unique column constraint is missing")
        if not model_foreign_keys(table) <= set(foreign_keys(conn, table.name)):
            raise MigrationError(f"{table.name}: foreign key definition does not match")
        if table.name in LEGACY_UNIQUE_KEYS and legacy_unique_indexes(conn, table.name):
            raise MigrationError(f"{table.name}: obsolete single-credential unique constraint remains")
    check_database(conn)


def validate_ledger(conn):
    layout = {row[1]: (row[2].upper(), row[3], row[5]) for row in conn.execute("PRAGMA table_info(schema_migrations)")}
    if layout != {"version": ("INTEGER", 0, 1), "name": ("TEXT", 1, 0), "applied_at_utc": ("TEXT", 1, 0),
                  "backup_path": ("TEXT", 0, 0), "backup_sha256": ("TEXT", 0, 0)}:
        raise MigrationError("Invalid migration ledger schema; review the original database and backup")
    if conn.execute("SELECT version FROM schema_migrations").fetchall() != [(SCHEMA_VERSION,)]:
        raise MigrationError("Unsupported migration version; use the paired application image")


def preflight_rebuilds(conn, metadata, existing):
    definitions = {}
    for name in LEGACY_UNIQUE_KEYS:
        if name not in existing:
            continue
        rebuild_needed = (bool(legacy_unique_indexes(conn, name)) if name == 'aws_credentials'
                          else 'credential_id' not in columns(conn, name))
        if rebuild_needed:
            definitions[name] = rebuild_definition(conn, metadata.tables[name])
    return definitions


def migration_backup(conn, source, open_files):
    directory = source.parent / 'backups'
    stem = f'{source.name}.pre-v1'
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
            validate_ledger(conn)
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
        validate_schema(conn, metadata)
        conn.execute("INSERT INTO schema_migrations VALUES (?,?,?,?,?)", (
            SCHEMA_VERSION, "T01 baseline schema", datetime.now(timezone.utc).isoformat(),
            backup["path"] if backup else None, backup["sha256"] if backup else None))
        conn.commit()
    except BaseException as exc:
        conn.rollback()
        logger.error("Database migration failed; startup stopped. Backup: %s", backup["path"] if backup else "none")
        raise MigrationError("SQLite migration failed; startup stopped") from exc
    finally:
        open_files.close()
        conn.execute("PRAGMA foreign_keys=ON")
        raw.close()
