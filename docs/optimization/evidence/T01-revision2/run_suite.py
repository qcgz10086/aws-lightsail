"""Run the T01 revision-2 comparison from an isolated Git export (see report)."""
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import sqlalchemy


def manifest(root):
    return {file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for folder in ('backend/app', 'backend/tests') for file in sorted((root / folder).rglob('*.py'))}


def main(inputs, output):
    ids = json.loads((inputs / 'identity.json').read_bytes())
    output.mkdir(parents=True, exist_ok=True)
    runs = {}
    with tempfile.TemporaryDirectory(prefix='t01-r2-') as scratch:
        root = Path(scratch)
        for folder in ('source', 'base', 'historical-models'):
            shutil.copytree(inputs / folder, root / folder)
        source, base = root / 'source', root / 'base'
        for label, directory in [('source', source), ('base', base)]:
            if manifest(directory) != ids[label + '_manifest']:
                raise ValueError(label + ' source manifest mismatch')
        drivers = inputs / 'drivers'
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', AWS_EC2_METADATA_DISABLED='true',
                   AWS_ACCESS_KEY_ID='testing', AWS_SECRET_ACCESS_KEY='testing', AWS_CONFIG_FILE=os.devnull,
                   AWS_SHARED_CREDENTIALS_FILE=os.devnull)
        for key in ('SECRET_KEY', 'ENCRYPTION_KEY', 'ADMIN_PASSWORD', 'DATABASE_URL', 'PYTHONPATH', 'AWS_PROFILE',
                    'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI', 'AWS_SESSION_TOKEN'):
            env.pop(key, None)

        def run(name, cwd, args, *, expected=0, json_output=False):
            argv = [sys.executable, *map(str, args)]
            result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE if json_output else subprocess.STDOUT, timeout=180)
            target = output / (name + ('.json' if json_output else '.log'))
            target.write_bytes(result.stdout)
            if json_output:
                (output / (name + '.stderr')).write_bytes(result.stderr)
            runs[name] = {'cwd': str(cwd), 'argv': argv, 'exit': result.returncode, 'expected': expected}
            if result.returncode != expected:
                raise RuntimeError(f'{name}: exit {result.returncode}, expected {expected}: {target}')
            if json_output:
                return json.loads(result.stdout)
            return result.stdout.decode('utf-8', errors='replace')

        for label, directory in (('base', base), ('candidate', source)):
            observed = run('impact-' + label, root, [drivers / 'impact_checks.py', directory], json_output=True)
            retry = observed['retry']
            candidate = label == 'candidate'
            assert [row['count'] for row in retry] == ([1]*4 if candidate else [1, 2, 3, 4])
            assert all(not row['accepted'] and not row['ledger_present'] and row['source_unchanged'] for row in retry)
            assert len({digest for row in retry for digest in row['digests']}) == 1
            if candidate:
                assert len({tuple(row['paths']) for row in retry}) == 1
            column = observed['column_rule']
            assert column['accepted'] == column['ledger_present'] == (not candidate)
            assert column['before'] == column['verified_backup_rule']
            assert column['after']['null_write_allowed'] == (not candidate)
            if candidate:
                assert column['before'] == column['after'] and column['source_sha256_before'] == column['source_sha256_after']

        shutil.copyfile(source / 'backend/tests/test_migration_retries.py', base / 'backend/tests/test_migration_retries.py')
        discover = ['-m', 'unittest', 'discover', '-s', 'tests']
        red = run('red', base / 'backend', [*discover, '-p', 'test_migration_retries.py', '-v'], expected=1)
        assert 'FAILED (' in red and 'Ran 13 tests' in red
        for suite, extra, count in [('focused', ['-p', 'test_migrat*.py'], 47), ('full', [], 182)]:
            log = run(suite, source / 'backend', [*discover, *extra, '-v'])
            match = re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK(?: \(skipped=(\d+)\))?\s*$', log)
            assert match and int(match[1]) == count
            runs[suite].update(tests=count, seconds=float(match[2]), skipped=int(match[3] or 0))
        history = run('history', root, [drivers / 'historical_roundtrip.py', root], json_output=True)
        assert len(history) == 5 and all(item['original_schema_and_values_restored'] for item in history)
        evidence = source / 'docs/optimization/evidence'
        run('cli-command', root, [evidence / 'T01/validate_cli.py', '--output', output / 'cli.json'])
        run('boundaries-command', root, [drivers / 'probe_boundaries.py', '--source', source, '--output', output / 'boundaries.json'])
        cli = json.loads((output / 'cli.json').read_bytes())['cli_smoke']
        assert [row['exit_code'] for row in cli['commands']] == [0, 0, 1, 1, 0, 0]
        assert cli['restored_committed_wal'] and cli['preserved_pre_restore_data']
        boundaries = {r['probe']: r for r in json.loads((output / 'boundaries.json').read_bytes())['observations']}
        assert all(not boundaries[key]['accepted'] for key in ('snapshot_output_sidecar', 'custom_inline_foreign_key',
                                                               'custom_autoincrement', 'ledger_required_columns'))
        index = boundaries['legacy_explicit_unique_index']
        assert index['accepted'] and index['second_credential_same_instance_accepted'] and index['second_credential_same_traffic_accepted']
        assert manifest(source) == ids['source_manifest']
    receipt = {'base': ids['base'], 'source_tree': ids['source_tree'], 'source_files': len(ids['source_manifest']),
               'platform': platform.platform(), 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
               'sqlalchemy': sqlalchemy.__version__, 'runs': runs}
    (output / 'runs.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: runs[key] for key in ('focused', 'full')}))


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
