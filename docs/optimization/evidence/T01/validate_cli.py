"""Reproducible T01 CLI smoke checks and small-fixture startup observations."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    backend = root / "backend"
    env = dict(os.environ)
    for key in ("SECRET_KEY", "ENCRYPTION_KEY", "ADMIN_PASSWORD", "DATABASE_URL"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    commands = []

    def cli(*arguments, expected=0):
        argv = [sys.executable, "-m", "app.sqlite_backup", *map(str, arguments)]
        result = subprocess.run(argv, cwd=backend, env=env, capture_output=True, text=True, encoding="utf-8", timeout=40)
        commands.append({"argv": argv, "exit_code": result.returncode,
                         "expected_exit_code": expected, "stdout": result.stdout, "stderr": result.stderr})
        assert result.returncode == expected, commands[-1]
        return json.loads(result.stdout)

    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        source, backup = directory / "source.db", directory / "snapshot.db"
        with closing(sqlite3.connect(source)) as writer:
            writer.executescript("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT); INSERT INTO sample VALUES(1,'before');")
            writer.execute("INSERT INTO sample VALUES(2,'committed in WAL')")
            writer.commit()
            assert Path(str(source) + "-wal").stat().st_size > 0
            receipt = cli("backup", "--database", source, "--output", backup)
        cli("verify", "--backup", backup, "--sha256", receipt["sha256"].upper())
        with closing(sqlite3.connect(source)) as writer:
            writer.execute("UPDATE sample SET value='after' WHERE id=1")
            writer.commit()
        cli("restore", "--backup", backup, "--database", source, "--sha256", receipt["sha256"], expected=1)
        cli("restore", "--backup", backup, "--database", source, "--sha256", "0" * 64, "--offline", expected=1)
        restored = cli("restore", "--backup", backup, "--database", source, "--sha256", receipt["sha256"], "--offline")
        cli("verify", "--backup", backup, "--sha256", receipt["sha256"])
        with closing(sqlite3.connect(source)) as reader:
            assert reader.execute("SELECT * FROM sample ORDER BY id").fetchall() == [(1, "before"), (2, "committed in WAL")]
        with closing(sqlite3.connect(restored["safety_backup"]["path"])) as reader:
            assert reader.execute("SELECT value FROM sample WHERE id=1").fetchone() == ("after",)
        cli_result = {"commands": commands, "restored_committed_wal": True,
                      "preserved_pre_restore_data": True,
                      "snapshot_bytes": backup.stat().st_size,
                      "snapshot_permission_mode": oct(backup.stat().st_mode & 0o777),
                      "application_secret_variables_removed": True}

    sys.path[:0] = [str(backend), str(backend / "tests")]
    from test_migrations import LEGACY, seed_current
    from sqlalchemy import create_engine
    import sqlalchemy
    from app import database
    from app.migrations import upgrade_database
    observations = []
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        for kind in ("new", "single", "multi"):
            samples = []
            for index in range(10):
                path = directory / kind / str(index) / "app.db"
                path.parent.mkdir(parents=True)
                if kind == "single":
                    with closing(sqlite3.connect(path)) as conn:
                        conn.executescript(LEGACY)
                elif kind == "multi":
                    seed_current(path)
                size_before = path.stat().st_size if path.exists() else 0
                engine = create_engine(f"sqlite:///{path}")
                try:
                    start = time.perf_counter()
                    upgrade_database(engine, database.Base.metadata)
                    upgrade_ms = (time.perf_counter() - start) * 1000
                    start = time.perf_counter()
                    upgrade_database(engine, database.Base.metadata)
                    restart_ms = (time.perf_counter() - start) * 1000
                finally:
                    engine.dispose()
                with closing(sqlite3.connect(path)) as conn:
                    versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
                assert versions == [(1,)]
                backups = list((path.parent / "backups").glob("*.db"))
                assert len(backups) == (0 if kind == "new" else 1)
                samples.append({"upgrade_ms": upgrade_ms, "restart_ms": restart_ms,
                                "database_before_bytes": size_before, "database_after_bytes": path.stat().st_size,
                                "backup_bytes": sum(p.stat().st_size for p in backups), "backup_count": len(backups)})
            observations.append({"fixture": kind, "samples": samples,
                                 "upgrade_median_ms": statistics.median(s["upgrade_ms"] for s in samples),
                                 "upgrade_p95_ms": sorted(s["upgrade_ms"] for s in samples)[math.ceil(len(samples)*.95)-1],
                                 "restart_median_ms": statistics.median(s["restart_ms"] for s in samples)})
    result = {"status": "passed", "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "command": [sys.executable, *sys.argv], "platform": platform.platform(),
              "python": sys.version, "sqlite": sqlite3.sqlite_version, "sqlalchemy": sqlalchemy.__version__,
              "cli_smoke": cli_result, "small_fixture_observations": observations,
              "limits": "10 small temporary fixtures per generation; startup and disk observations only. No page, AWS, image-size, CPU, peak-memory, production-volume or power-loss acceptance claim."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "cli_commands": len(commands), "observations": [{k:v for k,v in row.items() if k != "samples"} for row in observations]}))


if __name__ == "__main__":
    main()
