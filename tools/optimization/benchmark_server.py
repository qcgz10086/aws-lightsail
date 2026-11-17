"""Local-only baseline host. Runs real app routes with a strict fake AWS boundary."""
import argparse
from contextlib import closing
from datetime import date, datetime
import gc
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import Event, Lock, Thread
import time
from unittest.mock import patch

import psutil
import uvicorn

from tools.optimization.fake_aws import FixtureAws, block_external_connections

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "aws.json"
API_CASES = {
    "billing": "/api/billing/summary", "instances": "/api/instances",
    "regions": "/api/catalog/regions", "bundles": "/api/catalog/bundles?region=us-east-1",
    "blueprints": "/api/catalog/blueprints?region=us-east-1", "traffic": "/api/traffic/summary",
}
PAGE_CASES = {
    "dashboard": {"path": "/", "api": ["/api/instances", "/api/traffic/summary"],
                  "ready": ".ant-statistic-content-value", "ready_text": "实例总数"},
    "instances": {"path": "/instances", "api": ["/api/instances"],
                  "ready": ".ant-table-tbody .ant-table-row"},
    "billing": {"path": "/billing", "api": ["/api/billing/summary"],
                "ready": ".ant-statistic-content-value", "ready_text": "本月总费用"},
}
PASSWORD = "T00FixturePass123!"


def configure(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "baseline.db"
    if db_path.exists():
        raise RuntimeError("refusing to overwrite an existing baseline database")
    os.environ.update({
        "DATABASE_URL": "sqlite:///" + db_path.as_posix(),
        "SECRET_KEY": "test-only-secret-key-0123456789abcdef",
        "ENCRYPTION_KEY": "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=",
        "ADMIN_PASSWORD": PASSWORD, "COOKIE_SECURE": "false", "ENABLE_DOCS": "false",
        "AWS_EC2_METADATA_DISABLED": "true", "AWS_ACCESS_KEY_ID": "T00_TEST_ONLY",
        "AWS_SECRET_ACCESS_KEY": "fictional-test-only",
        "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
    })
    sys.path.insert(0, str(ROOT / "backend"))
    return db_path


def seed(aws, samples):
    from app.database import SessionLocal, init_db
    from app.models import AwsCredential, CreditRecord, InstanceSetting, TrafficUsage, User
    from app.security import encrypt_secret, hash_password
    init_db()
    stamp = datetime.fromisoformat(aws.data["date"])
    password_hash = hash_password(PASSWORD)
    scopes = ["api-" + name for name in API_CASES] + ["page-" + name for name in PAGE_CASES]
    with SessionLocal() as db:
        for scope in scopes:
            for sample in range(samples):
                user = User(username=f"t00-{scope}-{sample:02}", password_hash=password_hash,
                            is_admin=False, monthly_limit_gb=1024, created_at=stamp)
                db.add(user)
                db.flush()
                credentials = []
                for n, (key, account) in enumerate(aws.data["keys"].items()):
                    cred = AwsCredential(user_id=user.id, access_key_id_enc=encrypt_secret(key),
                                         secret_access_key_enc=encrypt_secret("fictional-test-only"),
                                         account_label=f"{scope}-{sample:02}-{key}",
                                         is_default=n == 0, created_at=stamp)
                    db.add(cred)
                    db.flush()
                    credentials.append(cred)
                    for region in aws.data["regions"]:
                        for i in range(aws.data["instances_per_region"]):
                            name = f"t00-{account}-{region}-{i:02}"
                            db.add(TrafficUsage(
                                user_id=user.id, credential_id=cred.id, region=region,
                                instance_name=name, year_month=stamp.strftime("%Y-%m"),
                                network_in_bytes=64 * 1024**3, network_out_bytes=64 * 1024**3,
                                last_synced_at=stamp))
                            if i == 0:
                                db.add(InstanceSetting(
                                    user_id=user.id, credential_id=cred.id, region=region,
                                    instance_name=name, monthly_limit_gb=1024,
                                    auto_stop_on_limit=False, created_at=stamp))
                for n in range(aws.data["manual_credits_per_user"]):
                    db.add(CreditRecord(
                        user_id=user.id, credential_id=credentials[n * 2].id,
                        name=f"T00 manual {n}", credit_id=f"T00-MANUAL-{user.id}-{n}",
                        total_amount=100, currency="USD", start_date=date(2026, 1, 1),
                        expires_at=date(2027, 1, 1), created_at=stamp))
        db.commit()
    gc.collect()


def serve(args):
    db_path = configure(args.data_dir)
    aws = FixtureAws(FIXTURE, delay_ms=args.delay_ms)
    seed(aws, args.samples)
    from sqlalchemy import event
    from starlette.exceptions import HTTPException
    from starlette.staticfiles import StaticFiles
    from app.database import engine
    from app import main
    from app.services import billing, traffic

    stamp = datetime.fromisoformat(aws.data["date"])
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)
    billing.datetime = FixedDateTime
    traffic.datetime = FixedDateTime
    main.start_scheduler = lambda: None
    main.stop_scheduler = lambda: None
    app = main.app
    process = psutil.Process()
    state_lock = Lock()
    state = {"sql_queries": 0, "peak_rss_bytes": process.memory_info().rss}
    stopped = Event()

    @event.listens_for(engine, "before_cursor_execute")
    def count_sql(*_):
        with state_lock:
            state["sql_queries"] += 1

    def sample_memory():
        while not stopped.wait(0.01):
            rss = process.memory_info().rss
            with state_lock:
                state["peak_rss_bytes"] = max(state["peak_rss_bytes"], rss)
    sampler = Thread(target=sample_memory, daemon=True)
    sampler.start()

    def stats():
        with closing(sqlite3.connect(db_path)) as db:
            journal = db.execute("PRAGMA journal_mode").fetchone()[0]
            sizes = {suffix or "main": p.stat().st_size if p.exists() else 0
                     for suffix in ("", "-wal", "-shm")
                     for p in [Path(str(db_path) + suffix)]}
        cpu = process.cpu_times()
        with state_lock:
            counters = dict(state)
        return {
            **aws.snapshot(), **counters, "rss_bytes": process.memory_info().rss,
            "cpu_seconds": cpu.user + cpu.system, "clock_seconds": time.perf_counter(),
            "database_bytes": sizes, "journal_mode": journal,
            "resource_scope": "native Python benchmark host, including static asset serving",
        }

    @app.get("/__baseline__/stats")
    def baseline_stats():
        return stats()

    @app.post("/__baseline__/reset")
    def baseline_reset():
        aws.reset()
        with state_lock:
            state["sql_queries"] = 0
            state["peak_rss_bytes"] = process.memory_info().rss
        return stats()

    @app.get("/__baseline__/manifest")
    def manifest():
        return {"fixture_sha256": aws.digest, "fixture": aws.data, "samples": args.samples,
                "api_cases": API_CASES, "page_cases": PAGE_CASES,
                "dataset": {"panel_users": (len(API_CASES) + len(PAGE_CASES)) * args.samples, "credentials_per_user": 3, "distinct_aws_accounts_per_user": 2, "regions_per_account": 2, "instances_per_region": 6},
                "username_pattern": "t00-{api|page}-{case}-{sample:02}",
                "password": PASSWORD}

    class SpaFiles(StaticFiles):
        async def get_response(self, path, scope):
            try:
                response = await super().get_response(path, scope)
            except HTTPException as exc:
                if exc.status_code != 404 or path.startswith("api/"):
                    raise
                response = await super().get_response("index.html", scope)
            return response

    @app.middleware("http")
    async def static_headers(request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith(("/api/", "/__baseline__/")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'self'")
        return response

    app.mount("/", SpaFiles(directory=ROOT / "frontend" / "dist", html=True))
    try:
        with patch("boto3.client", aws.client), patch(
            "boto3.session.Session.client", lambda _self, service_name, **kw: aws.client(service_name, **kw)
        ), patch("botocore.client.BaseClient._make_api_call",
                 lambda *_a, **_kw: aws.reject("unexpected real botocore client")), block_external_connections():
            uvicorn.run(app, host=getattr(args, "host", "127.0.0.1"), port=args.port, workers=1,
                        access_log=False, log_level="warning")
    finally:
        stopped.set()
        sampler.join(timeout=1)
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--delay-ms", type=int)
    options = parser.parse_args()
    if options.samples < 1 or options.samples > 100:
        parser.error("--samples must be in 1..100")
    serve(options)
