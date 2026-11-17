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


def create_table(conn, table, name=None):
    ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    if name:
        original = str(table.name)
        # The temporary name only changes CREATE TABLE, never FK references.
        ddl = ddl.replace(f"CREATE TABLE {original} ", f"CREATE TABLE {quote(name)} ", 1)
    conn.execute(ddl)


def rebuild(conn, table, defaults):
    old_cols = columns(conn, table.name)
    unknown = old_cols - set(table.columns.keys())
    if unknown:
        raise MigrationError(f"{table.name}: unknown columns require manual review")
    table_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table.name,)).fetchone()[0]
    if re.search(r"\b(CHECK|COLLATE|GENERATED)\b|WITHOUT\s+ROWID", table_sql, re.IGNORECASE):
        raise MigrationError(f"{table.name}: custom table constraints require manual review")
    allowed_legacy_unique = {
        "aws_credentials": ["user_id"],
        "instance_settings": ["user_id", "region", "instance_name"],
        "traffic_usage": ["user_id", "region", "instance_name", "year_month"],
    }[table.name]
    for row in conn.execute(f"PRAGMA index_list({quote(table.name)})"):
        if row[2] and row[3] == "u":
            keys = [item[2] for item in conn.execute(f"PRAGMA index_info({quote(row[1])})")]
            if keys != allowed_legacy_unique:
                raise MigrationError(f"{table.name}: custom unique constraint requires manual review")
    temp = f"_t01_{table.name}"
    if temp in tables(conn):
        raise MigrationError(f"{table.name}: unexpected temporary table")
    saved = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table.name,)).fetchall()
    removed = {name for name, cols in unique_columns(conn, table.name)
               if table.name == "aws_credentials" and cols == ["user_id"]}
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
        if name not in removed:
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
        if any(cols == ["user_id"] for _, cols in unique_columns(conn, "aws_credentials")):
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
        actual_fks = {(r[3], r[2], r[4], r[6].upper()) for r in conn.execute(f"PRAGMA foreign_key_list({quote(table.name)})")}
        for col in table.columns:
            for fk in col.foreign_keys:
                if (col.name, fk.column.table.name, fk.column.name, (fk.ondelete or "NO ACTION").upper()) not in actual_fks:
                    raise MigrationError(f"{table.name}: foreign key definition does not match")
    if any(cols == ["user_id"] for _, cols in unique_columns(conn, "aws_credentials")):
        raise MigrationError("aws_credentials: obsolete single-key constraint remains")
    check_database(conn)


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
        versions = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() if "schema_migrations" in existing else []
        if versions not in ([], [(SCHEMA_VERSION,)]):
            raise MigrationError("Unsupported migration version; use the paired application image")
        if versions:
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
