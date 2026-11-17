"""Run reproducible local baseline workloads without AWS access or production data."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
import psutil

from tools.optimization.measure import summarize
from tools.optimization.processes import stop_process_tree

ROOT = Path(__file__).resolve().parents[2]


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def start_run(output):
    output.mkdir(parents=True, exist_ok=True)
    for name in ("run.json", "api.json", "browser.json", "summary.json", "environment.json"):
        if (output / name).exists():
            raise FileExistsError(f"existing evidence in {output}; use a new output directory")
    run_id = str(uuid4())
    save_json(output / "run.json", {"run_id": run_id, "status": "running",
                                   "started_at_utc": datetime.now(timezone.utc).isoformat()})
    return run_id


def environment(output, node):
    def command(args, cwd=ROOT):
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
        return {"command": args, "exit_code": result.returncode,
                "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    data = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python": sys.version,
        "python_executable": sys.executable, "worktree": str(ROOT),
        "logical_cpu_count": psutil.cpu_count(), "physical_cpu_count": psutil.cpu_count(logical=False),
        "system_memory_bytes": psutil.virtual_memory().total,
        "git_head": command(["git", "rev-parse", "HEAD"]),
        "node": command([node, "--version"]),
        "pip_check": command([sys.executable, "-m", "pip", "check"]),
        "packages": sorted(
            [{"name": d.metadata["Name"], "version": d.version}
             for d in importlib.metadata.distributions()], key=lambda d: d["name"].lower()),
    }
    frozen = command([sys.executable, "-m", "pip", "freeze", "--all"])
    (output / "python-freeze.txt").write_text(frozen["stdout"] + "\n", encoding="utf-8")
    for name in ("frontend", "tools/optimization"):
        source = ROOT / name / "package-lock.json"
        if source.exists():
            shutil.copy2(source, output / (name.replace("/", "-") + "-package-lock.json"))
    data["docker"] = {"available": bool(shutil.which("docker")),
                      "image_total_bytes": None, "container_memory_bytes": None,
                      "container_cpu_percent": None,
                      "status": "unmeasured",
                      "reason": "Docker unavailable on PATH; no image/container measurements"}
    if data["docker"]["available"]:
        data["docker"]["reason"] = "Use tools.optimization.container_baseline on a Linux Docker/cgroup-v2 host; see tools/optimization/README.md."
        data["docker"]["version"] = command(["docker", "version", "--format", "{{json .}}"])
    data["input_sha256"] = {
        str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for pattern in ("backend/app/**/*.py", "frontend/src/**/*", "tools/optimization/*.py",
                        "tools/optimization/*.mjs", "tools/optimization/fixtures/*.json",
                        "frontend/dist/**/*")
        for path in sorted(ROOT.glob(pattern)) if path.is_file()
    }
    save_json(output / "environment.json", data)
    return data


def summarize_rows(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["case"], row["phase"])].append(row)
    return [
        {"case": case, "phase": phase,
         **summarize([r[key] for r in values if r.get(key) is not None]),
         "errors": sum(bool(r.get("error")) for r in values),
         "one_core_cpu_percent": round(sum(r.get("cpu_seconds", 0) for r in values) / max(sum(r.get("wall_seconds", 0) for r in values), 0.000001) * 100, 3),
         "aws_calls_per_sample": [sum(r.get("aws_calls", {}).values()) for r in values],
         "peak_rss_bytes": max((r.get("peak_rss_bytes", 0) for r in values), default=None)}
        for (case, phase), values in groups.items()
    ]


def read_browser_report(path, exit_code, expected_count, run_id=None):
    def failed(message):
        return {"results": [], "errors": [{"browser_report_error": message,
                                          "browser_exit_code": exit_code}]}
    if exit_code != 0:
        return failed("browser process failed; existing reports are not trusted")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return failed(str(exc))
    if not isinstance(report, dict) or expected_count < 1:
        return failed("invalid browser report")
    if run_id and (report.get("run_id") != run_id or report.get("status") != "completed"):
        return failed("browser report belongs to another run or is not completed")
    if len(report.get("results", [])) != expected_count:
        return failed("incomplete browser sample count")
    return report


def run_workload(args, run_id):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([args.node, "--check", str(ROOT / "tools/optimization/browser_benchmark.mjs")], check=True)
    env = environment(output, args.node)
    env["run_id"] = run_id
    save_json(output / "environment.json", env)
    scratch = ROOT / ".baseline-runs"
    scratch.mkdir(exist_ok=True)
    errors, rows = [], []
    with tempfile.TemporaryDirectory(prefix="t00-", dir=scratch) as data_dir:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        with (output / "server.log").open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "tools.optimization.benchmark_server",
                 "--port", str(port), "--data-dir", data_dir, "--samples", str(args.samples)],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                with httpx.Client(base_url=url, timeout=30, trust_env=False) as admin:
                    deadline = time.monotonic() + 45
                    while True:
                        if proc.poll() is not None:
                            raise RuntimeError("baseline host exited; see server.log")
                        try:
                            if admin.get("/api/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if time.monotonic() > deadline:
                            raise TimeoutError("baseline host startup timed out")
                        time.sleep(0.1)
                    manifest = admin.get("/__baseline__/manifest").json()
                    save_json(output / "manifest.json", manifest)
                    before_idle = admin.post("/__baseline__/reset").json()
                    time.sleep(1)
                    idle = admin.get("/__baseline__/stats").json()
                    idle["one_core_cpu_percent"] = (
                        (idle["cpu_seconds"] - before_idle["cpu_seconds"]) /
                        (idle["clock_seconds"] - before_idle["clock_seconds"]) * 100)
                    for case, path in manifest["api_cases"].items():
                        for sample in range(args.samples):
                            with httpx.Client(base_url=url, timeout=30, trust_env=False) as client:
                                login = client.post("/api/auth/login", json={
                                    "username": f"t00-api-{case}-{sample:02}",
                                    "password": manifest["password"],
                                })
                                login.raise_for_status()
                                for phase in ("first_request", "repeat_request"):
                                    before = admin.post("/__baseline__/reset").json()
                                    start = time.perf_counter()
                                    response = client.get(path)
                                    elapsed = (time.perf_counter() - start) * 1000
                                    after = admin.get("/__baseline__/stats").json()
                                    row = {"case": case, "phase": phase, "sample": sample,
                                           "elapsed_ms": elapsed, "status_code": response.status_code,
                                           "response_bytes": len(response.content),
                                           "aws_calls": after["calls"], "events": after["events"],
                                           "sql_queries": after["sql_queries"],
                                           "rss_bytes": after["rss_bytes"],
                                           "peak_rss_bytes": after["peak_rss_bytes"],
                                           "peak_concurrency": after["peak_concurrency"],
                                           "cpu_seconds": after["cpu_seconds"] - before["cpu_seconds"],
                                           "wall_seconds": after["clock_seconds"] - before["clock_seconds"],
                                           "database_bytes": after["database_bytes"],
                                           "violations": after["violations"]}
                                    if response.status_code != 200 or after["violations"]:
                                        row["error"] = response.text[:1000]
                                        errors.append({"case": case, "phase": phase, "sample": sample,
                                                       "error": row["error"]})
                                    rows.append(row)
                        print(f"API completed: {case}", flush=True)
                    save_json(output / "api.json", {
                        "run_id": run_id, "status": "completed" if not errors else "failed",
                        "samples_per_case_phase": args.samples, "idle": idle,
                        "results": rows, "errors": errors})
                    with (output / "browser-process.log").open("w", encoding="utf-8") as browser_log:
                        browser_proc = subprocess.Popen(
                            [args.node, str(ROOT / "tools/optimization/browser_benchmark.mjs"),
                             url, str(output / "browser.json"), args.chrome, run_id], cwd=ROOT,
                            stdout=browser_log, stderr=subprocess.STDOUT)
                        try:
                            browser_exit = browser_proc.wait(timeout=args.browser_timeout)
                        except subprocess.TimeoutExpired:
                            errors.append({"browser_timeout_seconds": args.browser_timeout})
                            stop_process_tree(browser_proc)
                            browser_exit = 124
                            partial_path = output / "browser.json"
                            if partial_path.exists():
                                partial = json.loads(partial_path.read_text(encoding="utf-8"))
                                partial["status"] = "failed"
                                partial["errors"].append({"error": "browser process timeout"})
                                save_json(partial_path, partial)
                        finally:
                            if browser_proc.poll() is None:
                                stop_process_tree(browser_proc)
                    if browser_exit:
                        errors.append({"browser_exit_code": browser_exit})
                    final = admin.get("/__baseline__/stats").json()
                    save_json(output / "final-stats.json", final)
                    browser = read_browser_report(output / "browser.json", browser_exit,
                                                  len(manifest["page_cases"]) * args.samples * 3, run_id)
                    errors.extend(browser["errors"])
                    summary = {
                        "run_id": run_id, "status": "failed" if errors else "completed",
                        "measured_source_commit": env["git_head"]["stdout"],
                        "fixture_sha256": manifest["fixture_sha256"],
                        "api": summarize_rows(rows, "elapsed_ms"),
                        "pages_first_data": summarize_rows(browser["results"], "first_data_ms"),
                        "pages_complete": summarize_rows(browser["results"], "complete_ms"),
                        "idle_native_process": idle, "database": final["database_bytes"],
                        "docker": env["docker"], "errors": errors,
                        "limits": [
                            "Local Windows native host; production Nginx/TLS and containers are unmeasured.",
                            "Fixed AWS latency is synthetic and is not a prediction of AWS latency.",
                            "First/repeat phases describe workload state; they do not assert cache existence.",
                            "No fault-injection, task-recovery or migration acceptance is claimed by T00.",
                        ],
                    }
                    save_json(output / "summary.json", summary)
                    print(json.dumps({"errors": len(summary["errors"]),
                                      "api_samples": len(rows),
                                      "browser_samples": len(browser["results"]),
                                      "output": str(output)}, ensure_ascii=False), flush=True)
            finally:
                stop_process_tree(proc)
    return 1 if errors else 0


def run(args):
    output = args.output.resolve()
    run_id = start_run(output)
    status = json.loads((output / "run.json").read_text(encoding="utf-8"))
    try:
        exit_code = run_workload(args, run_id)
        status.update(status="completed" if exit_code == 0 else "failed", exit_code=exit_code)
        return exit_code
    except BaseException as exc:
        status.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        status["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(output / "run.json", status)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--browser-timeout", type=int, default=900)
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--chrome", default="C:/Program Files/Google/Chrome/Application/chrome.exe")
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or not args.node or args.browser_timeout < 1:
        parser.error("samples must be 1..100, timeout positive and Node available")
    sys.exit(run(args))
