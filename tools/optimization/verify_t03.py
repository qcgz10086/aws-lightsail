"""Reproduce T03 verification on fixed mock data, with AWS transport blocked."""
import argparse
import hashlib
import inspect
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

BASE = "f868161406664dd383c856fd3345771d7176b9c2"
ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"


def command(argv, cwd, log):
    started = time.perf_counter()
    result = subprocess.run(argv, cwd=cwd, capture_output=True, env=os.environ.copy(), timeout=240)
    log.write_bytes(result.stdout + result.stderr)
    return {"argv": list(map(str, argv)), "cwd": str(cwd), "exit": result.returncode,
            "seconds": round(time.perf_counter() - started, 3), "log": log.name}


def performance(source):
    from datetime import datetime, timezone
    from unittest.mock import Mock
    sys.path.insert(0, str(source / "backend"))
    from app.services.billing import get_billing_summary
    result = []
    for count in (24, 100, 240):
        client = Mock()
        groups = []
        for index in range(count):
            groups.append({"Keys": [f"Service-{index:03}", "Usage"], "Metrics": {
                "UnblendedCost": {"Amount": "0.123456789", "Unit": "USD"},
                "NetUnblendedCost": {"Amount": "0.1", "Unit": "USD"}}})
        client.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": groups}]}
        kwargs = {"account_id": "123456789012"} if "account_id" in inspect.signature(get_billing_summary).parameters else {}
        samples = []
        for _ in range(8):
            client.reset_mock()
            start = time.perf_counter()
            output = get_billing_summary(client, datetime(2026, 11, 19, tzinfo=timezone.utc), **kwargs)
            samples.append((time.perf_counter() - start) * 1000)
        result.append({"service_rows": count, "median_ms": round(statistics.median(samples[1:]), 4),
                       "samples_ms": samples[1:], "cost_api_calls": client.get_cost_and_usage.call_count,
                       "credits_applied": output["credits_applied"], "net_cost": output["net_cost"]})
    print(json.dumps(result))


def main(output):
    output.mkdir(parents=True, exist_ok=False)
    program = """import sys,unittest
from unittest.mock import patch
suite=unittest.defaultTestLoader.discover('tests',pattern=sys.argv[1])
with patch('botocore.endpoint.Endpoint._send',side_effect=AssertionError('AWS transport forbidden')) as aws:
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 print('AWS_TRANSPORT_ATTEMPTS',aws.call_count)
 sys.exit(0 if result.wasSuccessful() and not aws.call_count else 1)
"""
    runs = {"compileall": command([sys.executable, "-m", "compileall", "-q", "app"], ROOT / "backend", output / "compileall.log")}
    for name, pattern in (("focused", "test_t03*.py"), ("migration", "test_migrat*.py"), ("full", "test*.py")):
        runs[name] = command([sys.executable, "-c", program, pattern], ROOT / "backend", output / f"{name}.log")
    runs["typescript"] = command(["node", "node_modules/typescript/bin/tsc", "--noEmit", "-p", "tsconfig.json"],
                                 ROOT / "frontend", output / "typescript.log")
    runs["build"] = command(["node", "node_modules/vite/bin/vite.js", "build", "--outDir", str(output / "frontend-build")],
                            ROOT / "frontend", output / "build.log")
    with tempfile.TemporaryDirectory(prefix="t03-baseline-") as directory:
        baseline = Path(directory)
        raw = subprocess.check_output(["git", "archive", BASE, "backend"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            tar.extractall(baseline, filter="data")
        for label, source in (("base", baseline), ("candidate", ROOT)):
            runs[f"performance-{label}"] = command([sys.executable, __file__, "--performance", str(source)], ROOT,
                                                     output / f"performance-{label}.json")
    sources = [path for directory in ("backend/app", "backend/tests", "frontend/src")
               for path in (ROOT / directory).rglob("*") if path.suffix in (".py", ".ts", ".tsx")]
    record = {"base": BASE, "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip(),
              "status": subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True).strip(),
              "platform": platform.platform(), "python": sys.version, "runs": runs,
              "source_sha256": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted(sources)},
              "all_commands_passed": all(row["exit"] == 0 for row in runs.values()),
              "files": {path.name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                        for path in output.iterdir() if path.is_file()}}
    (output / "verification.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"head": record["head"], "all_commands_passed": record["all_commands_passed"], "runs": runs}, indent=2))
    return 0 if record["all_commands_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--performance", type=Path)
    args = parser.parse_args()
    if args.performance:
        performance(args.performance)
    elif args.output:
        raise SystemExit(main(args.output.resolve()))
    else:
        parser.error("Choose --output or --performance")
