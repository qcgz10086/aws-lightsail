"""SQLite snapshots and offline recovery; usable without application secrets."""
import argparse
from contextlib import closing, suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid


class DatabaseRecoveryError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def check_database(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise DatabaseRecoveryError("SQLite integrity_check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise DatabaseRecoveryError("SQLite foreign_key_check failed")


def require_no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(str(path) + suffix):
            raise DatabaseRecoveryError(f"SQLite sidecar present: {path}{suffix}")


def snapshot_database(source: Path, destination: Path) -> dict:
    """Copy committed data through SQLite's backup API, including a live WAL."""
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if source == destination or os.path.lexists(destination):
        raise DatabaseRecoveryError("Backup destination must be a new path")
    require_no_sidecars(destination)
    if not source.is_file():
        raise DatabaseRecoveryError("Source database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".sqlite-snapshot-", dir=destination.parent)
    os.close(fd)
    staging = Path(name)
    try:
        deadline = time.monotonic() + 30

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise DatabaseRecoveryError("SQLite backup exceeded 30 seconds")

        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(staging)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=0.05)
                check_database(dst)
        with staging.open("r+b") as stream:
            os.fsync(stream.fileno())
        digest = file_sha256(staging)
        require_no_sidecars(destination)
        # Link publishes atomically without overwriting an existing destination.
        os.link(staging, destination)
        try:
            sync_directory(destination.parent)
            verify_backup(destination, digest)
        except (DatabaseRecoveryError, OSError, sqlite3.Error):
            with suppress(FileNotFoundError):
                if os.path.samestat(staging.stat(), destination.lstat()):
                    destination.unlink()
                    sync_directory(destination.parent)
            raise
        return {"path": str(destination), "sha256": digest,
                "created_at_utc": datetime.now(timezone.utc).isoformat()}
    finally:
        staging.unlink(missing_ok=True)


def verify_backup(path: Path, expected_sha256: str) -> dict:
    path = Path(path).resolve()
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256.lower()):
        raise DatabaseRecoveryError("A full SHA256 is required")
    require_no_sidecars(path)
    if file_sha256(path) != expected_sha256.lower():
        raise DatabaseRecoveryError("Backup SHA256 mismatch")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        check_database(conn)
    require_no_sidecars(path)
    return {"path": str(path), "sha256": expected_sha256.lower(), "integrity": "ok"}


def restore_database(backup: Path, destination: Path, expected_sha256: str, *, offline: bool = False) -> dict:
    """Caller must stop every process using destination before recovery."""
    if not offline:
        raise DatabaseRecoveryError("Stop all database users and explicitly select offline recovery")
    backup, destination = Path(backup).resolve(), Path(destination).resolve()
    if backup == destination:
        raise DatabaseRecoveryError("Restore source and target must differ")
    verify_backup(backup, expected_sha256)
    token = uuid.uuid4().hex
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".restore-", dir=destination.parent)
    staging = Path(name)
    safety = None
    try:
        with os.fdopen(fd, "wb") as target, backup.open("rb") as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        verify_backup(staging, expected_sha256)
        require_no_sidecars(backup)
        if destination.exists():
            safety = snapshot_database(destination, destination.with_name(f"{destination.name}.pre-restore-{token}.db"))
            with closing(sqlite3.connect(destination, timeout=1, isolation_level=None)) as conn:
                conn.execute("BEGIN EXCLUSIVE")
                conn.execute("ROLLBACK")
                busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy:
                    raise DatabaseRecoveryError("Destination WAL is still in use")
            # SQLite removes sidecars on the last clean close. Never remove them ourselves.
        require_no_sidecars(destination)
        os.replace(staging, destination)
        sync_directory(destination.parent)
        return {"path": str(destination), "restored_from": str(backup),
                "sha256": file_sha256(destination), "safety_backup": safety,
                "requires_paired_images_and_task_reconciliation": True}
    finally:
        staging.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    verify.add_argument("--sha256", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--database", type=Path, required=True)
    restore.add_argument("--sha256", required=True)
    restore.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            result = snapshot_database(args.database, args.output)
        elif args.command == "verify":
            result = verify_backup(args.backup, args.sha256)
        else:
            result = restore_database(args.backup, args.database, args.sha256, offline=args.offline)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (DatabaseRecoveryError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
