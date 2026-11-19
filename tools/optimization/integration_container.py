"""Paired production image driver. Inputs are exported fixed source directories."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Event, Thread
import time
import uuid
from datetime import datetime, timezone
import platform

import httpx
from tools.optimization.container_baseline import DockerRun, build_images, cgroup_path, read_cgroup, resource_window, loopback_bridge
from tools.optimization.run_baseline import save_json, summarize_rows


def run(args):
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    docker = DockerRun(output)
    run_id = uuid.uuid4().hex
    state = {'run_id': run_id, 'status': 'running', 'source': str(args.source), 'source_commit': args.label,
             'source_tree': args.tree, 'samples': args.samples, 'started_at': datetime.now(timezone.utc).isoformat()}
    save_json(output / 'run.json', state)
    try:
        bases = json.loads(args.base_images.read_text())
        images = build_images(docker, args.source.resolve(), args.label, bases)
        save_json(output / 'image-inspect.json', {name: docker.inspect('image', image['id']) for name,image in images.items()})
        save_json(output / 'environment.json', {**state, 'platform': platform.platform(), 'cpu_count': os.cpu_count(),
            'docker': json.loads(docker.command('version', '--format', '{{json .}}')),
            'source_sha256': {p.relative_to(args.source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for folder in ('backend','frontend') for p in (args.source/folder).rglob('*') if p.is_file() and 'node_modules' not in p.parts},
            'tool_sha256': {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (root/'tools/optimization').rglob('*') if p.is_file() and 'node_modules' not in p.parts and '__pycache__' not in p.parts}})
        runtime = output / 'runtime'
        runtime.mkdir()
        shutil.copytree(root / 'tools', runtime / 'tools', ignore=shutil.ignore_patterns('node_modules', '__pycache__'))
        data = runtime / 'data'; data.mkdir(); data.chmod(0o777)
        deps = runtime / 'deps'; deps.mkdir(); deps.chmod(0o777)
        docker.command('run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}', '-v', f'{deps}:/deps',
            images['backend']['id'], 'python', '-m', 'pip', 'install', '--no-deps', '--target', '/deps', 'psutil==7.1.3', log='fixture-deps.log')
        prefix = 't07-' + uuid.uuid4().hex[:8]
        exporter = docker.launch(prefix + '-export', '--network', 'none', images['frontend']['id'], 'sleep', '120')
        (runtime / 'frontend').mkdir()
        docker.command('cp', exporter + ':/usr/share/nginx/html', str(runtime / 'frontend/dist'))
        docker.command('rm', '-f', exporter); docker.containers.remove(exporter)
        nginx = (args.source / 'frontend/nginx.conf').read_text().replace('    location /api/ {',
            '    location /__baseline__/ {\n        proxy_pass http://backend:8000;\n    }\n\n    location /api/ {')
        config = runtime / 'nginx.conf'; config.write_text(nginx)
        docker.network = prefix + '-offline'
        docker.command('network', 'create', '--internal', docker.network)
        backend = docker.launch(prefix + '-backend', '--network', docker.network, '--network-alias', 'backend',
            '-v', f'{runtime}:/fixture:ro', '-v', f'{data}:/data', '-v', f'{deps}:/deps:ro', '-w', '/fixture',
            '-e', 'PYTHONPATH=/app:/fixture:/deps', '-e', 'SECRET_KEY=test-only-secret-key-0123456789abcdef',
            '-e', 'ENCRYPTION_KEY=WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=', '-e', 'ADMIN_PASSWORD=T00FixturePass123!', images['backend']['id'], 'python', '-m',
            'tools.optimization.integration_server', '--data-dir', '/data', '--samples', str(args.samples))
        frontend = docker.launch(prefix + '-frontend', '--network', docker.network,
            '-v', f'{config}:/etc/nginx/conf.d/default.conf:ro', images['frontend']['id'])
        containers = {name: docker.inspect('container', cid) for name, cid in [('backend', backend), ('frontend', frontend)]}
        save_json(output / 'containers.json', containers)
        paths = {name: cgroup_path(docker, cid) for name, cid in [('backend', backend), ('frontend', frontend)]}
        address = containers['frontend']['NetworkSettings']['Networks'][docker.network]['IPAddress']
        with loopback_bridge(address, 8080) as url, httpx.Client(base_url=url, trust_env=False, timeout=40) as client:
            deadline = time.monotonic() + 120
            while True:
                try:
                    if client.get('/api/health').status_code == 200: break
                except httpx.HTTPError: pass
                if time.monotonic() >= deadline: raise TimeoutError('fixture startup failed')
                time.sleep(.2)
            save_json(output / 'manifest.json', client.get('/__baseline__/manifest').json())
            docker.command('exec', backend, 'python', '-m', 'pip', 'freeze', '--all', log='python-freeze.txt')
            docker.command('exec', backend, 'python', '-c', 'import os,socks,cryptography,sqlite3; from app.security import encrypt_secret,decrypt_secret; assert decrypt_secret(encrypt_secret("fixture"))=="fixture"; assert os.getuid()!=0; assert os.access("/data",os.W_OK); print("imports encryption uid writable-volume passed")', log='runtime-smoke.log')
            raw, failures, stop = [], [], Event()
            def capture():
                values={}
                for name,p in paths.items():
                    current=int((p/'memory.current').read_text())
                    memory=dict(line.split() for line in (p/'memory.stat').read_text().splitlines())
                    cpu=dict(line.split() for line in (p/'cpu.stat').read_text().splitlines())
                    values[name]={'at_monotonic':time.monotonic(), 'memory_current_bytes':current,
                        'inactive_file_bytes':int(memory['inactive_file']), 'anon_bytes':int(memory['anon']),
                        'working_set_bytes':max(0,current-int(memory['inactive_file'])),
                        'cpu_seconds':int(cpu['usage_usec'])/1_000_000}
                return {'time': time.monotonic(), **values}
            def sample():
                with (output / 'cgroup.jsonl').open('w') as stream:
                    while not stop.is_set():
                        try:
                            row = capture(); raw.append(row); stream.write(json.dumps(row)+'\n'); stream.flush()
                        except Exception as exc: failures.append(str(exc)); return
                        stop.wait(.1)
            sampler = Thread(target=sample, daemon=True); sampler.start()
            try:
                idle_start = capture(); time.sleep(10); idle_end = capture(); active_start = capture()
                with (output / 'browser-process.log').open('w') as stream:
                    result = subprocess.run(['node', str(root / 'tools/optimization/integration_browser.mjs'),
                        url, str(output / 'browser.json'), args.chrome, run_id], stdout=stream, stderr=subprocess.STDOUT, timeout=2400)
                active_end = capture()
                final = client.get('/__baseline__/stats').json(); save_json(output / 'final-stats.json', final)
            finally:
                stop.set(); sampler.join(3)
            if failures: raise RuntimeError(failures)
            if not (output / 'browser.json').exists():
                raise RuntimeError(f'Browser process exited {result.returncode} without a report; see browser-process.log')
            report = json.loads((output / 'browser.json').read_text())
            def window(start, end, name):
                samples = [start[name]] + [r[name] for r in raw if start['time'] < r['time'] < end['time']] + [end[name]]
                return resource_window(samples)
            rows = report['results']
            summary = {'run_id': run_id, 'status': report['status'], 'samples': args.samples, 'browser_exit': result.returncode,
                'windows': {'idle_start': idle_start,'idle_end':idle_end,'active_start':active_start,'active_end':active_end},
                'image_total_bytes': sum(i['size_bytes'] for i in images.values()),
                'pages_first_data': summarize_rows(rows, 'first_data_ms'), 'pages_complete': summarize_rows(rows, 'complete_ms'),
                'resources': {name: {'idle': window(idle_start, idle_end, name),
                    'workload': window(active_start, active_end, name)} for name in paths}, 'database': final['database_bytes'],
                'errors': report['errors'], 'sample_count': len(rows),
                'definitions': {'memory': 'cgroup v2 memory.current-inactive_file; raw current/anon/CPU saved every100ms',
                                'fixture': 'production images, read-only fixture tools and psutil; no product source mount',
                                'images': 'sum of two uncompressed Docker image Size values', 'idle': '10s after startup before workload'}}
            save_json(output / 'summary.json', summary)
            if result.returncode or report['errors'] or len(rows) != args.samples * 12: raise RuntimeError('measurement failed')
        state.update(status='completed',exit_code=0)
    except BaseException as exc:
        state.update(status='failed',exit_code=1,error=str(exc)); raise
    finally:
        try:
            docker.cleanup()
        except BaseException as exc:
            state.update(status='failed', exit_code=1, cleanup_error=str(exc))
            raise
        finally:
            state['finished_at']=datetime.now(timezone.utc).isoformat()
            save_json(output / 'run.json', state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--tree', default='draft')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-images', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--chrome', default='/usr/bin/chromium')
    run(parser.parse_args())
