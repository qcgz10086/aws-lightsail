"""Export BASE, the candidate and synthetic historical model fixtures from local Git objects."""
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
BASE = '73655ef8baed876e1cbf2bcae0c578bc94b931df'


def git(*args):
    return subprocess.check_output(['git', '-c', f'safe.directory={ROOT.as_posix()}', *args], cwd=ROOT)


def main(destination, candidate):
    tree = git('rev-parse', '--verify', f'{candidate}^{{tree}}').decode().strip()
    destination.mkdir(parents=True, exist_ok=False)
    identity = {'base': BASE, 'source_tree': tree}
    for label, ref in [('base', BASE), ('source', tree)]:
        paths = ['backend/app', 'backend/tests', 'backend/requirements.txt', 'docs/optimization/evidence/T01/validate_cli.py']
        data = git('archive', '--format=tar', ref, '--', *paths)
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
            archive.extractall(destination / label, filter='data')
        root = destination / label
        identity[label + '_manifest'] = {file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
                                        for folder in paths[:2] for file in sorted((root / folder).rglob('*.py'))}
    models = destination / 'historical-models'
    models.mkdir()
    for ref in ('00a599f', '433b021', '701ae5a', 'c0de0ef', 'c822f0a'):
        (models / f'{ref}.py').write_bytes(git('cat-file', 'blob', f'{ref}:backend/app/models.py'))
    drivers = destination / 'drivers'
    drivers.mkdir()
    for file in (HERE / 'run_suite.py', HERE / 'impact_checks.py', HERE / 'historical_roundtrip.py',
                 HERE.parent / 'T01-revision1/probe_boundaries.py'):
        (drivers / file.name).write_bytes(file.read_bytes().replace(b'\r\n', b'\n'))
    (destination / 'identity.json').write_text(json.dumps(identity, indent=2, sort_keys=True) + '\n', encoding='utf-8', newline='\n')
    print(tree)


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve(), sys.argv[2])
