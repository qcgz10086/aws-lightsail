"""Linux/cgroup-v2 baseline using production images and an isolated AWS fixture."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from threading import Event, Thread
import time

import httpx

from tools.optimization.processes import stop_process_tree
from tools.optimization.run_baseline import ROOT, read_browser_report, save_json, start_run, summarize_rows


@contextmanager
def loopback_bridge(address, port):
    class Forwarder(BaseHTTPRequestHandler):
        def forward(self):
            upstream = HTTPConnection(address, port, timeout=30)
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() not in ("host", "connection", "transfer-encoding")}
                upstream.request(self.command, self.path, body=body, headers=headers)
                response = upstream.getresponse()
                content = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in ("content-length", "transfer-encoding", "connection"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except OSError:
                self.send_error(502, "fixture container unavailable")
            finally:
                upstream.close()

        do_GET = forward
        do_POST = forward

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Forwarder)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def read_cgroup(root, now=None):
    memory = int((root / "memory.current").read_text())
    memory_stat = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
    cpu_stat = dict(line.split() for line in (root / "cpu.stat").read_text().splitlines())
    return {"at_monotonic": time.monotonic() if now is None else now,
            "memory_current_bytes": memory,
            "working_set_bytes": max(0, memory - int(memory_stat["inactive_file"])),
            "anon_bytes": int(memory_stat["anon"]),
            "cpu_seconds": int(cpu_stat["usage_usec"]) / 1_000_000}


def resource_window(samples):
    wall = samples[-1]["at_monotonic"] - samples[0]["at_monotonic"]
    cpu = samples[-1]["cpu_seconds"] - samples[0]["cpu_seconds"]
    if wall <= 0:
        raise ValueError("resource window must have positive duration")
    return {"wall_seconds": wall, "cpu_seconds": cpu,
            "one_core_cpu_percent": cpu / wall * 100,
            "peak_working_set_bytes": max(row["working_set_bytes"] for row in samples),
            "peak_memory_current_bytes": max(row["memory_current_bytes"] for row in samples),
            "mean_working_set_bytes": sum(row["working_set_bytes"] for row in samples) / len(samples),
            "samples": len(samples)}


def export_source(commit, source):
    hashes = {}
    for service in ("backend", "frontend"):
        archive = source / f"{service}-source.tar"
        subprocess.run(["git", "-c", "core.autocrlf=false", "archive", "--format=tar", "--prefix=" + service + "/",
                        "-o", str(archive), commit + ":" + service], cwd=ROOT, check=True)
        hashes[service] = hashlib.sha256(archive.read_bytes()).hexdigest()
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter="data")
    return hashes


class DockerRun:
    def __init__(self, output):
        self.output = output
        self.containers = []
        self.network = None

    def command(self, *args, log=None):
        result = subprocess.run(["docker", *args], text=True, capture_output=True)
        if log:
            (self.output / log).write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-2000:]}")
        return result.stdout.strip()

    def inspect(self, kind, identifier):
        return json.loads(self.command(kind, "inspect", identifier))[0]

    def launch(self, name, *args):
        self.command("run", "-d", "--name", name, *args)
        self.containers.append(name)
        return name

    def cleanup(self):
        errors = []
        for container in reversed(self.containers):
            try:
                self.command("logs", container, log=f"{container}.log")
            except Exception as exc:
                errors.append(str(exc))
            try:
                self.command("rm", "-f", container)
            except Exception as exc:
                errors.append(str(exc))
        if self.network:
            try:
                self.command("network", "rm", self.network)
            except Exception as exc:
                errors.append(str(exc))
        save_json(self.output / "cleanup.json", {"containers": self.containers,
                                                "network": self.network, "errors": errors})
        if errors:
            raise RuntimeError("container cleanup errors: " + str(errors))


def cgroup_path(docker, container):
    pid = docker.inspect("container", container)["State"]["Pid"]
    entries = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    relative = next(line[3:] for line in entries if line.startswith("0::"))
    root = Path("/sys/fs/cgroup").resolve()
    target = (root / relative.lstrip("/")).resolve()
    if not target.is_relative_to(root):
        raise RuntimeError("cgroup path escapes cgroup root")
    read_cgroup(target)
    return target


def build_images(docker, source, commit, base_images):
    images = {}
    for service in ("backend", "frontend"):
        text = (source / service / "Dockerfile").read_text()
        for image in re.findall(r"^FROM (\S+)", text, flags=re.MULTILINE):
            if image not in base_images:
                docker.command("pull", image, log="pull-" + image.replace("/", "-").replace(":", "-") + ".log")
                metadata = docker.inspect("image", image)
                base_images[image] = metadata["RepoDigests"][0]
            text = text.replace("FROM " + image, "FROM " + base_images[image])
        pinned = source / service / "Dockerfile.t00-pinned"
        pinned.write_text(text)
        tag = f"lightsail-t00-{service}:{commit[:12]}"
        docker.command("build", "-f", str(pinned), "-t", tag, str(source / service), log=f"build-{service}.log")
        metadata = docker.inspect("image", tag)
        images[service] = {"tag": tag, "id": metadata["Id"], "size_bytes": metadata["Size"],
                           "layers": metadata["RootFS"]["Layers"], "dockerfile": text,
                           "original_dockerfile_sha256": hashlib.sha256((source / service / "Dockerfile").read_bytes()).hexdigest()}
    save_json(docker.output / "base-images.json", base_images)
    save_json(docker.output / "images.json", images)
    return images


def workload(args, output, run_id, docker, source, images):
    prefix = "t00-" + run_id[:8]
    runtime = source / "runtime"
    shutil.copytree(ROOT / "tools", runtime / "tools", ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    data = runtime / "data"
    data.mkdir()
    data.chmod(0o777)
    dependencies = runtime / "dependencies"
    dependencies.mkdir()
    docker.command("run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{dependencies}:/deps",
                   images["backend"]["id"], "python", "-m", "pip", "install", "--no-deps",
                   "--target", "/deps", "psutil==7.1.3", log="fixture-dependency-install.log")
    exporter = docker.launch(prefix + "-export", "--network", "none", images["frontend"]["id"], "sleep", "300")
    (runtime / "frontend").mkdir()
    docker.command("cp", exporter + ":/usr/share/nginx/html", str(runtime / "frontend" / "dist"))
    docker.command("rm", "-f", exporter)
    docker.containers.remove(exporter)
    nginx = (source / "frontend/nginx.conf").read_text().replace("    location /api/ {", "    location /__baseline__/ {\n        proxy_pass http://backend:8000;\n    }\n\n    location /api/ {")
    config = runtime / "nginx.conf"
    config.write_text(nginx)
    (output / "fixture-nginx.conf").write_text(nginx)
    docker.network = prefix + "-offline"
    docker.command("network", "create", "--internal", docker.network)
    backend = docker.launch(prefix + "-backend", "--network", docker.network, "--network-alias", "backend",
                            "-v", f"{runtime}:/fixture:ro", "-v", f"{data}:/data",
                            "-v", f"{dependencies}:/deps:ro", "-w", "/fixture",
                            "-e", "PYTHONPATH=/app:/fixture:/deps",
                            images["backend"]["id"], "python", "-m", "tools.optimization.benchmark_server",
                            "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data",
                            "--samples", str(args.samples))
    frontend = docker.launch(prefix + "-frontend", "--network", docker.network,
                             "-v", f"{config}:/etc/nginx/conf.d/default.conf:ro",
                             images["frontend"]["id"])
    address = docker.inspect("container", frontend)["NetworkSettings"]["Networks"][docker.network]["IPAddress"]
    with loopback_bridge(address, 8080) as url, httpx.Client(base_url=url, trust_env=False, timeout=30) as admin:
        deadline = time.monotonic() + 90
        while True:
            try:
                if admin.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("fixture container startup timed out")
            time.sleep(0.2)
        manifest = admin.get("/__baseline__/manifest").json()
        save_json(output / "manifest.json", manifest)
        paths = {name: cgroup_path(docker, container) for name, container in (("backend", backend), ("frontend", frontend))}
        save_json(output / "containers.json", {
            "backend": docker.inspect("container", backend), "frontend": docker.inspect("container", frontend),
            "network": json.loads(docker.command("network", "inspect", docker.network))[0]})
        docker.command("exec", backend, "python", "-m", "pip", "freeze", "--all", log="backend-python-freeze.txt")
        raw = []
        sampling_errors = []
        stopped = Event()

        def capture():
            return {"at_monotonic": time.monotonic(), **{name: read_cgroup(path) for name, path in paths.items()}}

        def sample():
            with (output / "cgroup-samples.jsonl").open("w") as log:
                while not stopped.is_set():
                    try:
                        row = capture()
                        raw.append(row)
                        log.write(json.dumps(row) + "\n")
                        log.flush()
                    except BaseException as exc:
                        sampling_errors.append(str(exc))
                        return
                    stopped.wait(0.1)

        sampler = Thread(target=sample, daemon=True)
        sampler.start()
        try:
            idle_start = capture()
            time.sleep(10)
            idle_end = capture()
            work_start = capture()
            rows, errors = [], []
            for case, path in manifest["api_cases"].items():
                for sample_index in range(args.samples):
                    with httpx.Client(base_url=url, trust_env=False, timeout=30) as client:
                        client.post("/api/auth/login", json={"username": f"t00-api-{case}-{sample_index:02}", "password": manifest["password"]}).raise_for_status()
                        for phase in ("first_request", "repeat_request"):
                            before = admin.post("/__baseline__/reset").json()
                            started = time.perf_counter()
                            response = client.get(path)
                            elapsed = (time.perf_counter() - started) * 1000
                            after = admin.get("/__baseline__/stats").json()
                            row = {"case": case, "phase": phase, "sample": sample_index,
                                   "elapsed_ms": elapsed, "status_code": response.status_code,
                                   "aws_calls": after["calls"], "sql_queries": after["sql_queries"],
                                   "cpu_seconds": after["cpu_seconds"] - before["cpu_seconds"],
                                   "wall_seconds": after["clock_seconds"] - before["clock_seconds"],
                                   "violations": after["violations"]}
                            if response.status_code != 200 or after["violations"]:
                                row["error"] = response.text[:1000]
                                errors.append(row)
                            rows.append(row)
                print("container API completed: " + case, flush=True)
                save_json(output / "api.json", {"run_id": run_id, "results": rows, "errors": errors})
            with (output / "browser-process.log").open("w") as log:
                browser = subprocess.Popen([args.node, str(ROOT / "tools/optimization/browser_benchmark.mjs"),
                                            url, str(output / "browser.json"), args.chrome, run_id], stdout=log, stderr=subprocess.STDOUT)
                try:
                    browser_exit = browser.wait(timeout=args.browser_timeout)
                except subprocess.TimeoutExpired:
                    stop_process_tree(browser)
                    browser_exit = 124
                finally:
                    if browser.poll() is None:
                        stop_process_tree(browser)
            browser_report = read_browser_report(output / "browser.json", browser_exit, args.samples * 9, run_id)
            errors.extend(browser_report["errors"])
            work_end = capture()
            final = admin.get("/__baseline__/stats").json()
            save_json(output / "final-stats.json", final)
        finally:
            stopped.set()
            sampler.join(timeout=3)
        if sampling_errors:
            raise RuntimeError("cgroup sampling failed: " + str(sampling_errors))

        def window(start, end, name):
            samples = [start[name]] + [row[name] for row in raw if start["at_monotonic"] < row["at_monotonic"] < end["at_monotonic"]] + [end[name]]
            return resource_window(samples)

        summary = {"run_id": run_id, "status": "failed" if errors else "completed",
                   "windows": {"idle_start": idle_start, "idle_end": idle_end,
                               "work_start": work_start, "work_end": work_end},
                   "fixture_sha256": manifest["fixture_sha256"], "api_samples": len(rows),
                   "browser_samples": len(browser_report["results"]), "api": summarize_rows(rows, "elapsed_ms"),
                   "pages_first_data": summarize_rows(browser_report["results"], "first_data_ms"),
                   "pages_complete": summarize_rows(browser_report["results"], "complete_ms"),
                   "image_total_bytes": sum(image["size_bytes"] for image in images.values()),
                   "resources": {name: {"idle": window(idle_start, idle_end, name),
                                        "workload": window(work_start, work_end, name)} for name in paths},
                   "database": final["database_bytes"], "errors": errors,
                   "definitions": {"images": "sum of two uncompressed image Size values; shared layers counted per image",
                                   "memory": "cgroup v2 memory.current minus inactive_file; raw current and anon retained",
                                   "idle": "10 seconds after fixture startup and before requests",
                                   "workload": "all API queries and 180 browser phases including fixture logins; CPU delta / wall time",
                                   "sampling": "100 ms; reported peak is sampled, not an absolute maximum",
                                   "scope": "production images with read-only fixture harness, psutil and admin Nginx location; host loopback bridge to internal network; no TLS or real AWS; scheduler disabled"}}
        save_json(output / "summary.json", summary)
        return 1 if errors else 0


def run(args):
    if platform.system() != "Linux":
        raise RuntimeError("container baseline requires a Linux Docker host with readable cgroup v2")
    output = args.output.resolve()
    run_id = start_run(output)
    state = json.loads((output / "run.json").read_text())
    docker = DockerRun(output)
    scratch = ROOT / ".baseline-runs"
    scratch.mkdir(exist_ok=True)
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", args.source_ref + "^{commit}"], cwd=ROOT, text=True).strip()
        base_images = json.loads(args.base_images.read_text()) if args.base_images else {}
        with tempfile.TemporaryDirectory(prefix="t00-container-", dir=scratch) as directory:
            source = Path(directory)
            archive_hash = export_source(commit, source)
            save_json(output / "environment.json", {
                "source_commit": commit, "source_archive_sha256": archive_hash, "run_id": run_id,
                "platform": platform.platform(), "python": sys.version,
                "logical_cpu_count": os.cpu_count(),
                "system_memory_bytes": os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"),
                "node_version": subprocess.check_output([args.node, "--version"], text=True).strip(),
                "docker": json.loads(docker.command("version", "--format", "{{json .}}")),
                "tool_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (ROOT / "tools/optimization").rglob("*")
                                if path.is_file() and "node_modules" not in path.parts and "__pycache__" not in path.parts},
                "production_sha256": {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
                                      for folder in ("backend", "frontend") for path in (source / folder).rglob("*") if path.is_file()}})
            images = build_images(docker, source, commit, base_images)
            try:
                exit_code = workload(args, output, run_id, docker, source, images)
            finally:
                docker.cleanup()
            state.update(status="completed" if exit_code == 0 else "failed", exit_code=exit_code, source_commit=commit)
            return exit_code
    except BaseException as exc:
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        state["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(output / "run.json", state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-images", type=Path)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--browser-timeout", type=int, default=900)
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--chrome", default=shutil.which("chromium") or shutil.which("google-chrome"))
    options = parser.parse_args()
    if not 1 <= options.samples <= 100 or not options.node or not options.chrome or options.browser_timeout < 1:
        parser.error("samples must be 1..100 and timeout positive; Node and Chrome/Chromium required")
    sys.exit(run(options))
