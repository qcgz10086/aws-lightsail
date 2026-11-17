import tempfile
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import httpx
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from tools.optimization.container_baseline import read_cgroup, resource_window, loopback_bridge, export_source


class ContainerResourceTests(unittest.TestCase):
    def test_scoped_export_does_not_read_unrelated_missing_doc_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)

            def git(*args, content=None):
                return subprocess.check_output(["git", *args], cwd=root, input=content).decode().strip()

            blob = git("hash-object", "-w", "--stdin", content=b"FROM scratch\n")
            tree = git("mktree", content=f"100644 blob {blob}\tDockerfile\n".encode())
            docs = git("mktree", "--missing", content=("100644 blob " + "1" * 40 + "\tmissing.md\n").encode())
            parent = git("mktree", "--missing", content=f"040000 tree {tree}\tbackend\n040000 tree {tree}\tfrontend\n040000 tree {docs}\tdocs\n".encode())
            commit = git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit-tree", parent, "-m", "fixture")
            destination = root / "export"
            destination.mkdir()
            with patch("tools.optimization.container_baseline.ROOT", root):
                hashes = export_source(commit, destination)
            self.assertEqual(set(hashes), {"backend", "frontend"})
            self.assertEqual((destination / "backend/Dockerfile").read_bytes(), b"FROM scratch\n")
            self.assertEqual((destination / "frontend/Dockerfile").read_bytes(), b"FROM scratch\n")

    def test_loopback_bridge_preserves_post_cookies_and_binary_response(self):
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(201)
                self.send_header("Set-Cookie", "session=fixture; HttpOnly")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with loopback_bridge("127.0.0.1", upstream.server_port) as url:
                response = httpx.post(url + "/api/auth/login", content=b"\x00fixture", trust_env=False)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.content, b"\x00fixture")
                self.assertEqual(response.cookies["session"], "fixture")
        finally:
            upstream.shutdown()
            thread.join()
            upstream.server_close()

    def test_memory_excludes_only_inactive_file_and_cpu_uses_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memory.current").write_text("1000")
            (root / "memory.stat").write_text("anon 600\nfile 400\ninactive_file 150\n")
            (root / "cpu.stat").write_text("usage_usec 2000000\n")
            sample = read_cgroup(root, now=10)
        self.assertEqual(sample["working_set_bytes"], 850)
        self.assertEqual(sample["memory_current_bytes"], 1000)
        self.assertEqual(sample["cpu_seconds"], 2)
        before = dict(sample, at_monotonic=8, cpu_seconds=1)
        window = resource_window([before, sample])
        self.assertEqual(window["one_core_cpu_percent"], 50)
        self.assertEqual(window["peak_working_set_bytes"], 850)
        self.assertEqual(window["wall_seconds"], 2)

    def test_missing_cgroup_files_fail_instead_of_reporting_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                read_cgroup(Path(directory))
