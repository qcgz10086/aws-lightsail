"""Verify the recorded artifacts and their Git identities without AWS or Instabox."""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]


def info(data):
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def check(condition, label):
    if not condition:
        raise ValueError(label)


def git(*args):
    return subprocess.check_output(['git', '-c', f'safe.directory={ROOT.as_posix()}', *args], cwd=ROOT)


def main():
    index = json.loads((HERE / 'index.json').read_bytes())
    files = {p.relative_to(HERE).as_posix(): p for p in HERE.rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and p != HERE / 'index.json'}
    check(set(files) == set(index['files']), 'evidence file list')
    for name, path in files.items():
        check(info(path.read_bytes()) == index['files'][name], name)
    ids = json.loads(files['identity.json'].read_bytes())
    head = index['implementation_head']
    check(git('rev-parse', head + '^').decode().strip() == ids['base'] == index['base'], 'repair parent')
    check(git('rev-parse', head + '^{tree}').decode().strip() == ids['source_tree'], 'implementation tree')
    for ref, expected in [(ids['base'], ids['base_manifest']), (head, ids['source_manifest'])]:
        names = git('ls-tree', '-r', '--name-only', ref, '--', 'backend/app', 'backend/tests').decode().splitlines()
        check(set(expected) == {name for name in names if name.endswith('.py')}, ref + ' source list')
        for name, digest in expected.items():
            blob = git('cat-file', 'blob', f'{ref}:{name}')
            crlf_export = blob.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
            check(digest in (info(blob)['sha256'], info(crlf_export)['sha256']), f'{ref}:{name}')
    for name, path in [('run_suite.py', HERE / 'run_suite.py'), ('impact_checks.py', HERE / 'impact_checks.py'),
                       ('historical_roundtrip.py', HERE / 'historical_roundtrip.py'),
                       ('probe_boundaries.py', HERE.parent / 'T01-revision1/probe_boundaries.py')]:
        check(info(path.read_bytes().replace(b'\r\n', b'\n'))['sha256'] == index['input_driver_sha256'][name], name)
    with tarfile.open(files['linux-results.tar.gz']) as archive:
        remote = {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}
    execution = json.loads(remote.pop('execution.json'))
    check(execution == json.loads(files['linux-execution.json'].read_bytes()), 'remote execution receipt')
    check(execution['network'] == 'none' and execution['user'] == 'appuser' and execution['exit'] == 0
          and execution['image'] == index['image'] and not execution['oom_killed']
          and any(m['Destination'] == '/in' and not m['RW'] for m in execution['mounts']), 'container identity')
    check({key: info(value) for key, value in remote.items()} == {'results/' + key: value for key, value in execution['files'].items()}, 'remote file list and hashes')
    for name in ('impact-base', 'impact-candidate', 'runs'):
        check(remote['results/' + name + '.json'] == files['linux-' + name + '.json'].read_bytes(), 'Linux copy: ' + name)
    for platform in ('windows', 'linux'):
        def read(name):
            return files['windows/' + name].read_bytes() if platform == 'windows' else remote['results/' + name]

        run = json.loads(read('runs.json'))
        check((run['base'], run['source_tree'], run['source_files']) == (ids['base'], ids['source_tree'], 38), platform)
        for suite, count in [('focused', 47), ('full', 182)]:
            record = run['runs'][suite]
            skipped = int(platform == 'windows')
            log = read(suite + '.log')
            check(record['exit'] == 0 and record['tests'] == count and record['skipped'] == skipped
                  and f'Ran {count} tests'.encode() in log and log.rstrip().endswith(b'OK (skipped=1)' if skipped else b'OK'), suite)
        for phase in ('base', 'candidate'):
            data = json.loads(read('impact-' + phase + '.json'))
            fixed = phase == 'candidate'
            attempts = data['retry']
            check([r['count'] for r in attempts] == ([1]*4 if fixed else [1, 2, 3, 4]), platform + ' F04')
            check(all(not r['ledger_present'] and not r['accepted'] and r['source_sha256'] == data['retry_source_sha256_before']
                      and r['snapshot_bytes'] == r['count'] * 32768 for r in attempts), 'retry source/space')
            if fixed:
                check(all((r['paths'], r['digests']) == (attempts[0]['paths'], attempts[0]['digests']) for r in attempts), 'snapshot reuse')
            column = data['column_rule']
            check(column['before'] == column['verified_backup_rule'] and not column['before']['null_write_allowed']
                  and column['ledger_present'] == column['accepted'] == column['after']['null_write_allowed'] == (not fixed), platform + ' F05')
            if fixed:
                check(column['before'] == column['after'] and column['source_sha256_before'] == column['source_sha256_after'], 'source changed on refusal')
        history = json.loads(read('history.json'))
        check({r['model'] for r in history} == {'00a599f', '433b021', '701ae5a', 'c0de0ef', 'c822f0a'} and all(
            r['original_schema_and_values_restored'] and r['safety_snapshot_has_later_write'] and r['single_backup_after_restart']
            and r['model_sha256'] == info(git('cat-file', 'blob', r['model'] + ':backend/app/models.py'))['sha256'] for r in history), 'historical round trip')
        cli = json.loads(read('cli.json'))['cli_smoke']
        check([r['exit_code'] for r in cli['commands']] == [0, 0, 1, 1, 0, 0] and cli['preserved_pre_restore_data'] and cli['restored_committed_wal'], 'CLI')
    print(f'Verified {len(files)} evidence files, 37 BASE / 38 candidate Git sources, '
          f'{len(remote)} Linux results, and both platforms against {head}.')


if __name__ == '__main__':
    main()
