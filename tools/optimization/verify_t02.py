"""Reproduce T02 tests and bounded SQLite probes without AWS transport."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

BASE = "541967282fa85aff8861a33ae3c6f2b483ccf273"
ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"


def command(argv, cwd, output):
    started = time.perf_counter()
    result = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=240, env=os.environ.copy())
    output.write_bytes(result.stdout + result.stderr)
    return {"argv": [str(item) for item in argv], "cwd": str(cwd), "exit": result.returncode,
            "seconds": round(time.perf_counter() - started, 3), "log": output.name}


def performance(source):
    sys.path.insert(0, str(source / "backend"))
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import AwsCredential, InstanceSetting, TrafficUsage, User
    from app.services.traffic import build_traffic_summary, current_year_month

    observations = []
    for size in (24, 100, 240):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        user = User(username="synthetic", password_hash="opaque", is_admin=False)
        extra = {"account_id": "123456789012"} if hasattr(AwsCredential, "account_id") else {}
        cred = AwsCredential(user=user, access_key_id_enc="opaque-key", secret_access_key_enc="opaque-secret",
                             is_default=True, **extra)
        db.add(user)
        db.flush()
        for i in range(size):
            policy = {"auto_stop_configured": True, "auto_stop_account_id": "123456789012"} if hasattr(
                InstanceSetting, "auto_stop_configured") else {}
            usage = {"account_id": "123456789012"} if hasattr(TrafficUsage, "account_id") else {}
            db.add(InstanceSetting(user_id=user.id, credential_id=cred.id, region="us-east-1",
                instance_name=f"server-{i}", monthly_limit_gb=10, auto_stop_on_limit=False, **policy))
            db.add(TrafficUsage(user_id=user.id, credential_id=cred.id, region="us-east-1",
                instance_name=f"server-{i}", year_month=current_year_month(), network_in_bytes=1024,
                network_out_bytes=2048, **usage))
        db.commit()
        build_traffic_summary(db, user)
        samples, sql_counts = [], []
        for _ in range(7):
            statements = []
            def record(_conn, _cursor, sql, _parameters, _context, _executemany):
                statements.append(sql)
            event.listen(engine, "before_cursor_execute", record)
            started = time.perf_counter()
            summary = build_traffic_summary(db, user)
            samples.append((time.perf_counter() - started) * 1000)
            event.remove(engine, "before_cursor_execute", record)
            assert len(summary["instances"]) == size
            sql_counts.append(len(statements))
        observations.append({"instances": size, "median_ms": round(statistics.median(samples), 3),
                             "max_sql": max(sql_counts), "samples_ms": samples})
        db.close()
        engine.dispose()
    print(json.dumps(observations))


def seed_t01(source, path):
    sys.path.insert(0, str(source / "backend"))
    os.environ["DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
    from app.database import init_db, engine, SessionLocal
    from app.models import AwsCredential, InstanceSetting, TrafficUsage, User
    from app.security import encrypt_secret
    init_db()
    with SessionLocal() as db:
        user = User(username="legacy", password_hash="opaque", is_admin=False,
                    monthly_limit_gb=1, auto_stop_on_limit_default=True)
        db.add(user)
        db.flush()
        for index in range(2):
            cred = AwsCredential(user_id=user.id, access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"),
                secret_access_key_enc=encrypt_secret("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
                is_default=index == 0)
            db.add(cred)
            db.flush()
            db.add(InstanceSetting(user_id=user.id, credential_id=cred.id, region="us-east-1",
                instance_name="server", monthly_limit_gb=1, auto_stop_on_limit=False))
            db.add(TrafficUsage(user_id=user.id, credential_id=cred.id, region="us-east-1",
                instance_name="server", year_month="2026-11", network_in_bytes=index + 1,
                network_out_bytes=0))
        db.commit()
    engine.dispose()
    print(json.dumps({"source": BASE, "fixture": str(path)}))


def migration_probe(baseline, output):
    sys.path.insert(0, str(ROOT / "backend"))
    import sqlite3
    from unittest.mock import patch
    from sqlalchemy import create_engine
    from app.database import Base
    from app import models, migrations
    from app.sqlite_backup import restore_database, file_sha256
    results = []
    for case in ("standard", "custom-index", "injected-failure"):
        path = output / (case + ".db")
        command_result = command([sys.executable, __file__, "--seed", str(baseline), str(path)],
                                 ROOT, output / (case + "-seed.log"))
        assert command_result["exit"] == 0, command_result
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
            assert "account_id" not in {r[1] for r in conn.execute("PRAGMA table_info(traffic_usage)")}
            if case == "custom-index":
                conn.execute("CREATE UNIQUE INDEX extra_usage_index ON traffic_usage(network_in_bytes) WHERE network_in_bytes > 0")
        before_sha = file_sha256(path)
        engine = create_engine(f"sqlite:///{path.as_posix()}")
        started = time.perf_counter()
        if case == "injected-failure":
            original = migrations.apply_v2
            def fail_after_v2(conn, metadata):
                original(conn, metadata)
                raise RuntimeError("injected after T02 DDL")
            with patch.object(migrations, "apply_v2", side_effect=fail_after_v2):
                try:
                    migrations.upgrade_database(engine, Base.metadata)
                except migrations.MigrationError:
                    pass
                else:
                    raise AssertionError("Expected migration failure")
            with sqlite3.connect(path) as conn:
                assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
                assert "account_id" not in {r[1] for r in conn.execute("PRAGMA table_info(traffic_usage)")}
            assert file_sha256(path) == before_sha
            migrations.upgrade_database(engine, Base.metadata)
        else:
            migrations.upgrade_database(engine, Base.metadata)
        duration = (time.perf_counter() - started) * 1000
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
            assert conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
            assert conn.execute("SELECT count(*) FROM traffic_usage WHERE account_id=''").fetchone()[0] == 2
            backup, sha = conn.execute("SELECT backup_path,backup_sha256 FROM schema_migrations WHERE version=2").fetchone()
            if case == "custom-index":
                assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='extra_usage_index'").fetchone()[0] == 1
        restored = output / (case + "-restored.db")
        restore_database(Path(backup), restored, sha, offline=True)
        with sqlite3.connect(restored) as conn:
            assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
            assert "account_id" not in {r[1] for r in conn.execute("PRAGMA table_info(traffic_usage)")}
        migrations.upgrade_database(engine, Base.metadata)
        engine.dispose()
        results.append({"case": case, "base_fixture": BASE, "upgrade_ms": round(duration, 3),
                        "source_bytes": path.stat().st_size, "snapshot_sha256": sha,
                        "restore": "pass", "idempotent_restart": "pass"})
    return results


def main(output):
    output.mkdir(parents=True, exist_ok=False)
    runs = {}
    test_program = """import sys,unittest
from unittest.mock import patch
suite=unittest.defaultTestLoader.discover('tests',pattern=sys.argv[1])
with patch('botocore.endpoint.Endpoint._send',side_effect=AssertionError('AWS transport forbidden')) as aws:
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 print('AWS_TRANSPORT_ATTEMPTS',aws.call_count)
 sys.exit(0 if result.wasSuccessful() and not aws.call_count else 1)
"""
    runs["compileall"] = command([sys.executable, "-m", "compileall", "-q", "app"], ROOT / "backend", output / "compileall.log")
    for name, pattern in (("focused", "test_t02*.py"), ("migration", "test_migrat*.py"), ("full", "test*.py")):
        runs[name] = command([sys.executable, "-c", test_program, pattern], ROOT / "backend", output / (name + ".log"))
    runs["traffic-script"] = command([sys.executable, "-c", "import runpy; runpy.run_path('tests/test_traffic_sum.py', run_name='__main__')"],
                                      ROOT / "backend", output / "traffic-script.log")
    runs["typescript"] = command(["node", "node_modules/typescript/bin/tsc", "--noEmit", "-p", "tsconfig.json"],
                                  ROOT / "frontend", output / "typescript.log")
    runs["frontend-build"] = command(["node", "node_modules/vite/bin/vite.js", "build", "--outDir", str(output / "frontend-build")],
                                     ROOT / "frontend", output / "frontend-build.log")
    with tempfile.TemporaryDirectory(prefix="t02-baseline-") as directory:
        base = Path(directory)
        archive = subprocess.check_output(["git", "archive", BASE, "backend"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(base, filter="data")
        migrations = migration_probe(base, output)
        for name, source in (("base", base), ("candidate", ROOT)):
            runs["performance-" + name] = command([sys.executable, __file__, "--performance", str(source)],
                                                   ROOT, output / ("performance-" + name + ".json"))
    source_paths = [path for directory in ("backend/app", "backend/tests", "frontend/src")
                    for path in (ROOT / directory).rglob("*")
                    if path.suffix in (".py", ".ts", ".tsx")]
    record = {"base": BASE, "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip(),
              "status": subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True).strip(),
              "source_sha256": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted(source_paths)},
              "platform": platform.platform(), "python": sys.version, "runs": runs, "migration_probes": migrations,
              "all_commands_passed": all(run["exit"] == 0 for run in runs.values()),
              "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in output.iterdir() if path.is_file()}}
    (output / "verification.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "all_commands_passed": record["all_commands_passed"],
                      "runs": runs}, ensure_ascii=False, indent=2))
    return 0 if record["all_commands_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--performance", type=Path)
    parser.add_argument("--seed", nargs=2, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.performance:
        performance(args.performance)
    elif args.seed:
        seed_t01(*args.seed)
    elif args.output:
        raise SystemExit(main(args.output.resolve()))
    else:
        parser.error("Choose --output, --performance, or --seed")
