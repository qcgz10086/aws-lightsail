"""Fixed mock route comparison; never creates an AWS client or network call."""
import argparse
from contextlib import ExitStack, asynccontextmanager
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")


def main(source):
    sys.path.insert(0, str(source / "backend"))
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from app.main import app
    from app.database import Base, get_db
    from app.deps import get_current_user
    from app.models import AwsCredential, User
    from app.security import encrypt_secret
    from app.services import lightsail as ls
    candidate = (source / "backend/app/services/snapshots.py").is_file()
    results = []
    for sample in range(7):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{Path(directory).as_posix()}/app.db", connect_args={"check_same_thread": False})
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            Base.metadata.create_all(engine)
            db = sessionmaker(bind=engine)()
            user = User(username="perf", password_hash="opaque")
            db.add(user); db.flush()
            for account in ("123456789012", "210987654321"):
                db.add(AwsCredential(user_id=user.id, account_id=account,
                    access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"),
                    secret_access_key_enc=encrypt_secret("fixed-fake-secret")))
            db.commit()
            old = dict(app.dependency_overrides)
            app.dependency_overrides[get_db] = lambda: db
            app.dependency_overrides[get_current_user] = lambda: user
            original_lifespan = app.router.lifespan_context
            @asynccontextmanager
            async def mock_lifespan(_app):
                yield
            app.router.lifespan_context = mock_lifespan
            client_context = TestClient(app)
            client = client_context.__enter__()
            calls = []
            sql = []
            def count(_conn, _cursor, statement, *_args):
                if statement.lstrip().upper().startswith("SELECT"):
                    sql.append(statement)
            event.listen(engine, "before_cursor_execute", count)
            def regions(*_args, **_kwargs):
                calls.append("regions"); time.sleep(0.02)
                return [{"name": "us-east-1"}, {"name": "us-west-2"}, {"name": "eu-west-1"}]
            def instances(_ak, _sk, region, **_kwargs):
                calls.append(region); time.sleep(0.04)
                return [{"name": f"server-{index:03}", "region": region, "state": "running"} for index in range(8)]
            with ExitStack() as stack:
                transport = stack.enter_context(patch("botocore.endpoint.Endpoint._send", side_effect=AssertionError("AWS forbidden")))
                stack.enter_context(patch.object(ls, "get_regions", side_effect=regions))
                stack.enter_context(patch.object(ls, "list_instances_in_region", side_effect=instances))
                path = "/api/v2/instances?wait=true" if candidate else "/api/instances"
                started = time.perf_counter()
                first = client.get(path)
                cold_response = (time.perf_counter() - started) * 1000
                first_data = None
                if candidate:
                    current = first.json()
                    if current["instances"]:
                        first_data = cold_response
                    for _ in range(200):
                        if len(current["instances"]) == 48 and not current["refreshing"]:
                            break
                        current = client.get(path).json()
                        if current["instances"] and first_data is None:
                            first_data = (time.perf_counter() - started) * 1000
                        if len(current["instances"]) == 48 and not current["refreshing"]:
                            break
                        time.sleep(0.005)
                    else:
                        raise AssertionError("Mock refresh did not complete")
                else:
                    assert first.status_code == 200 and len(first.json()) == 48, first.text
                    first_data = cold_response
                complete = (time.perf_counter() - started) * 1000
                calls_before_hot = len(calls)
                hot = []
                hot_sql = []
                for _ in range(7):
                    sql.clear(); start = time.perf_counter()
                    response = client.get(path)
                    hot.append((time.perf_counter() - start) * 1000); hot_sql.append(len(sql))
                    assert response.status_code == 200
                assert transport.call_count == 0
                results.append({"cold_response_ms": round(cold_response, 3), "first_data_ms": round(first_data, 3),
                    "complete_ms": round(complete, 3), "hot_ms": hot,
                    "hot_aws_calls": len(calls) - calls_before_hot, "initial_mock_aws_calls": calls_before_hot,
                    "hot_select_max": max(hot_sql)})
            if candidate:
                from app.services.snapshots import wait_for_refresh
                from app.models import DataSnapshot
                for row in db.query(DataSnapshot.key).all(): wait_for_refresh(row[0])
            client_context.__exit__(None, None, None)
            app.router.lifespan_context = original_lifespan
            event.remove(engine, "before_cursor_execute", count)
            app.dependency_overrides.clear(); app.dependency_overrides.update(old)
            db.close(); engine.dispose()
    hot_values = sorted(value for row in results for value in row["hot_ms"])
    print(json.dumps({"candidate": candidate, "samples": results,
        "median_first_data_ms": statistics.median(row["first_data_ms"] for row in results),
        "median_complete_ms": statistics.median(row["complete_ms"] for row in results),
        "hot_p95_ms": hot_values[math.ceil(len(hot_values) * .95)-1],
        "hot_aws_calls": sum(row["hot_aws_calls"] for row in results)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    main(parser.parse_args().source.resolve())
