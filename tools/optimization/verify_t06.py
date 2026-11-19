"""Offline T06 verifier; each Python process forbids all botocore transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = "4142d4544f3bd602d25fe74951b5c467736d253b"
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8", AWS_EC2_METADATA_DISABLED="true")

PROGRAM = '''import sys,unittest
from unittest.mock import patch
suite=unittest.defaultTestLoader.discover('tests',pattern=sys.argv[1])
with patch('botocore.endpoint.Endpoint._send',side_effect=AssertionError('AWS transport forbidden')) as aws:
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 print('AWS_TRANSPORT_ATTEMPTS',aws.call_count)
 sys.exit(0 if result.wasSuccessful() and not aws.call_count else 1)
'''


def run(argv, cwd, log):
    start = time.monotonic()
    with log.open('wb') as stream:
        result = subprocess.run(list(map(str, argv)), cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                timeout=600, env=os.environ.copy())
    return {"argv": list(map(str, argv)), "cwd": str(cwd), "exit": result.returncode,
            "seconds": time.monotonic() - start, "log": log.name}


def main(output, backend_only=False, playwright=None):
    output.mkdir(parents=True, exist_ok=False)
    runs = {}
    for name, pattern in (("focused", "test_t06*.py"), ("migration", "test_migrat*.py"), ("full", "test*.py")):
        runs[name] = run([sys.executable, "-c", PROGRAM, pattern], ROOT / "backend", output / f"{name}.log")
        print(name, runs[name]["exit"], flush=True)
    runs['compileall'] = run([sys.executable, '-m', 'compileall', '-q', 'app'], ROOT / 'backend', output / 'compileall.log')
    if not backend_only:
        for name, argv in (
            ('typescript', ['node', 'node_modules/typescript/bin/tsc', '--noEmit']),
            ('build', ['node', 'node_modules/vite/bin/vite.js', 'build', '--outDir', output / 'frontend-build']),
            ('cache', ['node', '--test', 'tests/queryCache.test.mjs']),
        ):
            runs[name] = run(argv, ROOT / 'frontend', output / f'{name}.log')
        runs['intents'] = run(['node', ROOT / 'tools/optimization/probe_t06_intents.mjs', ROOT / 'frontend',
                              ROOT / 'frontend/node_modules', output / 'intents'], ROOT, output / 'intents.log')
        if playwright:
            runs['browser'] = run(['node', ROOT / 'tools/optimization/probe_t06_browser.mjs', output / 'frontend-build',
                                  output / 'browser', playwright], ROOT, output / 'browser.log')
    git = lambda *args: subprocess.check_output(['git', '-c', f'safe.directory={ROOT.as_posix()}', *args], cwd=ROOT, text=True).strip()
    sources = [p for directory in ('backend/app', 'backend/tests', 'frontend/src', 'tools/optimization')
               for p in (ROOT / directory).rglob('*') if p.suffix in ('.py', '.ts', '.tsx', '.mjs') and 'node_modules' not in p.parts]
    report = {"base": BASE, "head": git('rev-parse', 'HEAD'), "tree": git('rev-parse', 'HEAD^{tree}'),
              "status": git('status', '--short'), "platform": platform.platform(), "python": sys.version,
              "runs": runs, "all_commands_passed": all(row['exit'] == 0 for row in runs.values()),
              "sources": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)},
              "files": {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                        for p in output.iterdir() if p.is_file()}}
    (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['all_commands_passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend-only', action='store_true')
    parser.add_argument('--playwright', type=Path)
    args = parser.parse_args()
    raise SystemExit(main(args.output.resolve(), args.backend_only, args.playwright))
