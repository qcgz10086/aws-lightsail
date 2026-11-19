"""Run tests against the installed /app, mounting tests and history only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

PROGRAM = '''import sys,unittest,app
from pathlib import Path
from unittest.mock import patch
assert Path(app.__file__).resolve()==Path('/app/app/__init__.py'),app.__file__
suite=unittest.defaultTestLoader.discover('tests',pattern=sys.argv[1])
with patch('botocore.endpoint.Endpoint._send',side_effect=AssertionError('AWS transport forbidden')) as aws:
 result=unittest.TextTestRunner(verbosity=2).run(suite)
 print('AWS_TRANSPORT_ATTEMPTS',aws.call_count)
 print('PRODUCTION_TEST_RESULT',result.testsRun,len(result.failures),len(result.errors),len(result.skipped))
 sys.exit(0 if result.wasSuccessful() and not aws.call_count and not result.skipped else 1)
'''
INSPECT = '''import hashlib,json,os,shutil,sys,sqlite3,socks,cryptography
from pathlib import Path
from app.security import encrypt_secret,decrypt_secret
assert os.getuid()!=0
assert shutil.which('gcc') is None
assert decrypt_secret(encrypt_secret('fixture'))=='fixture'
assert os.access('/data',os.W_OK)
print(json.dumps({'uid':os.getuid(),'python':sys.version,'sqlite':sqlite3.sqlite_version,
'app_sha256':{p.relative_to('/app').as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
for p in Path('/app/app').rglob('*') if p.is_file() and '__pycache__' not in p.parts}}))
'''


def main(args):
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=False); output.chmod(0o777)
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.image]))[0]
    expected = {p.relative_to(args.source/'backend').as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (args.source/'backend/app').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    common = ['docker','run','--rm','--network','none','--read-only','--tmpfs','/tmp:rw,nosuid,size=512m',
        '-v',f'{output}:/data','-e','PYTHONPATH=/app','-e','PYTHONIOENCODING=utf-8',
        '-e','PYTHONDONTWRITEBYTECODE=1','-e','AWS_EC2_METADATA_DISABLED=true',
        '-e','DATABASE_URL=sqlite:///:memory:','-e','SECRET_KEY=test-only-secret-key-0123456789abcdef',
        '-e','ENCRYPTION_KEY=WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=',
        '-e','ADMIN_PASSWORD=TestAdminPass123!']
    runs={}
    def run(name, extra, program):
        argv=common+extra+[image['Id']]+program
        start=time.monotonic()
        with (output/(name+'.log')).open('wb') as log:
            result=subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT,timeout=900)
        runs[name]={'exit':result.returncode,'seconds':time.monotonic()-start,'argv':argv,
                    'log_sha256':hashlib.sha256((output/(name+'.log')).read_bytes()).hexdigest()}
        (output/'verification.json').write_text(json.dumps({'image_id':image['Id'],'runs':runs},indent=2))
        print(name,result.returncode,flush=True)
        return result.returncode
    assert run('runtime',[],['python','-c',INSPECT])==0
    actual=json.loads((output/'runtime.log').read_text())
    assert actual['app_sha256']==expected, 'Image app differs from exported fixed source'
    mounts=['-v',f'{args.source.resolve()}/backend/tests:/verification/backend/tests:ro',
        '-v',f'{args.history.resolve()}/.git:/verification/.git:ro','-w','/verification/backend',
        '-v','/usr/bin/git:/verify-bin/git:ro',
        '-v','/lib/x86_64-linux-gnu/libpcre2-8.so.0:/verify-lib/libpcre2-8.so.0:ro',
        '-e','LD_LIBRARY_PATH=/verify-lib','-e','PATH=/verify-bin:/usr/local/bin:/usr/bin:/bin',
        '-e','GIT_CONFIG_COUNT=1','-e','GIT_CONFIG_KEY_0=safe.directory','-e','GIT_CONFIG_VALUE_0=/verification']
    for name,pattern in [('migration','test_migrat*.py'),('full','test*.py')]:
        run(name,mounts,['python','-c',PROGRAM,pattern])
    if args.probes:
        for name in ['capacity','task_capacity','restore_tasks']:
            run(name,['-v',f'{args.probes.resolve()}:/probes:ro'],['python',f'/probes/probe_{name}.py','/app','/data/'+name])
    report={'image_id':image['Id'],'source':str(args.source.resolve()),'runtime':actual,'runs':runs,
        'all_passed':all(r['exit']==0 for r in runs.values()),
        'boundary':'Only tests, historical Git objects and probe scripts mounted. Product modules loaded from immutable image /app.'}
    (output/'verification.json').write_text(json.dumps(report,indent=2))
    return 0 if report['all_passed'] else 1


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--source',type=Path,required=True)
    p.add_argument('--history',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--probes',type=Path)
    raise SystemExit(main(p.parse_args()))
