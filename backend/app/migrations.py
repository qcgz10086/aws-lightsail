"""Versioned SQLite startup migration. All DDL and the ledger share one transaction."""
from datetime import datetime, timezone
import logging
import re
from pathlib import Path
import sqlite3
import uuid

from sqlalchemy import MetaData, UniqueConstraint
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects import sqlite

from app.sqlite_backup import check_database, snapshot_database

SCHEMA_VERSION = 1
LEGACY_UNIQUE_KEYS = {
    "aws_credentials": ("user_id",),
    "instance_settings": ("user_id", "region", "instance_name"),
    "traffic_usage": ("user_id", "region", "instance_name", "year_month"),
}
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


def rebuild(conn, table, defaults):
    info = conn.execute(f"PRAGMA table_xinfo({quote(table.name)})").fetchall()
    old_cols = {row[1] for row in info}
    if old_cols - set(table.columns.keys()) or any(row[6] for row in info):
        raise MigrationError(f"{table.name}: unknown columns require manual review")
    table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table.name,)).fetchone()[0]
    if re.search(r"\b(CHECK|COLLATE|GENERATED|AUTOINCREMENT|DEFERRABLE|MATCH|STRICT)\b|WITHOUT\s+ROWID|ON\s+CONFLICT",
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


def apply_v1(conn, metadata):
    existing = tables(conn)
    if "users" in existing:
        add_columns(conn, "users", {"auto_stop_on_limit_default": "BOOLEAN DEFAULT 0 NOT NULL",
                                   "token_version": "INTEGER DEFAULT 0 NOT NULL"})
    if "aws_credentials" in existing:
        if legacy_unique_indexes(conn, "aws_credentials"):
            rebuild(conn, metadata.tables["aws_credentials"], {"is_default": "1"})
        else:
            add_columns(conn, "aws_credentials", {
                "is_default": "BOOLEAN DEFAULT 0 NOT NULL", "proxy_url_enc": "TEXT",
                "vcpu_quota": "FLOAT", "vcpu_tier": "VARCHAR(32)", "static_ip_quota": "FLOAT",
                "used_vcpu": "FLOAT", "used_instance_count": "INTEGER", "quota_region": "VARCHAR(32)",
                "quota_message": "TEXT", "quota_checked_at": "DATETIME"})
    for name in ("instance_settings", "traffic_usage"):
        if name not in existing:
            continue
        if "credential_id" not in columns(conn, name):
            ambiguous = conn.execute(f"""SELECT o.id FROM {quote(name)} o
                WHERE (SELECT COUNT(*) FROM aws_credentials c WHERE c.user_id=o.user_id) != 1 LIMIT 1""").fetchone()
            if ambiguous:
                raise MigrationError(f"{name}: legacy credential ownership is missing or ambiguous")
            rebuild(conn, metadata.tables[name], {
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


def upgrade_database(engine, metadata: MetaData) -> None:
    raw = engine.raw_connection()
    conn = raw.driver_connection
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
        path = engine.url.database
        if existing and path and path != ":memory:":
            if engine.url.query:
                raise MigrationError("SQLite URI options are unsupported for automatic migration backup")
            source = Path(path).resolve()
            destination = source.parent / "backups" / f"{source.name}.pre-v1-{uuid.uuid4().hex}.db"
            backup = snapshot_database(source, destination)
            logger.info("Migration backup: %s SHA256=%s", backup["path"], backup["sha256"])
        conn.execute("""CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at_utc TEXT NOT NULL,
            backup_path TEXT, backup_sha256 TEXT)""")
        apply_v1(conn, metadata)
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
        conn.execute("PRAGMA foreign_keys=ON")
        raw.close()
