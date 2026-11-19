"""Exercise legacy/current/legacy DB with both immutable production images."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
import httpx
from tools.optimization.container_baseline import DockerRun,loopback_bridge
from tools.optimization.run_baseline import save_json


def run(args):
    root=Path(__file__).resolve().parents[2]
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    data=out/'data';data.mkdir();data.chmod(0o777)
    runtime=out/'runtime';runtime.mkdir()
    shutil.copytree(root/'tools',runtime/'tools',ignore=shutil.ignore_patterns('node_modules','__pycache__'))
    docker=DockerRun(out)
    old=json.loads(args.base_images.read_text());new=json.loads(args.candidate_images.read_text())
    env=['-v',f'{data}:/data','-v',f'{runtime}:/fixture:ro','-w','/fixture',
        '-e','PYTHONPATH=/app:/fixture','-e','DATABASE_URL=sqlite:////data/drill.db',
        '-e','SECRET_KEY=test-only-secret-key-0123456789abcdef',
        '-e','ENCRYPTION_KEY=WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=',
        '-e','ADMIN_PASSWORD=TestAdminPass123!','-e','COOKIE_SECURE=false','-e','AWS_EC2_METADATA_DISABLED=true']
    report={'status':'running','old':old,'candidate':new,'steps':[],'service_may_resume':False}
    try:
        for mode,image in [('seed',old['backend']['id']),('fail_upgrade',new['backend']['id']),
            ('tasks',new['backend']['id']),('restore_legacy',new['backend']['id'])]:
            docker.command('run','--rm','--network','none',*env,image,'python','-m','tools.optimization.rollback_fixture',mode,log=mode+'.log')
            report['steps'].append({'step':mode,'image_id':image,'result':json.loads((data/(mode+'.json')).read_text())})
            save_json(out/'verification.json',report)
        prefix='t07-rollback-'+uuid.uuid4().hex[:8]
        docker.network=prefix+'-offline';docker.command('network','create','--internal',docker.network)
        backend=docker.launch(prefix+'-backend','--network',docker.network,'--network-alias','backend',*env,
            old['backend']['id'],'python','-m','tools.optimization.rollback_fixture','serve')
        frontend=docker.launch(prefix+'-frontend','--network',docker.network,old['frontend']['id'])
        containers={name:docker.inspect('container',cid) for name,cid in [('backend',backend),('frontend',frontend)]}
        save_json(out/'old-pair-containers.json',containers)
        address=containers['frontend']['NetworkSettings']['Networks'][docker.network]['IPAddress']
        with loopback_bridge(address,8080) as url,httpx.Client(base_url=url,trust_env=False,timeout=30) as client:
            limit=time.monotonic()+60
            while True:
                try:
                    if client.get('/api/health').status_code==200:break
                except httpx.HTTPError:pass
                if time.monotonic()>limit:raise TimeoutError('Old pair startup')
                time.sleep(.1)
            index=client.get('/');assert index.status_code==200 and '<div id="root">' in index.text
            login=client.post('/api/auth/login',json={'username':'rollback-fixture','password':'T07RollbackPass123!'});login.raise_for_status()
            reads={}
            for path in ['/api/auth/me','/api/billing/credits','/api/traffic/summary']:
                response=client.get(path);response.raise_for_status();reads[path]=response.json()
            blocked=client.post('/api/instances/us-east-1/historic/reboot');assert blocked.status_code==503
            report['old_pair_http']={'index_status':index.status_code,'login_status':login.status_code,'reads':reads,
                'mutation_rejected_status':blocked.status_code,'frontend_image':containers['frontend']['Image'],
                'backend_image':containers['backend']['Image']}
            assert containers['frontend']['Image']==old['frontend']['id'] and containers['backend']['Image']==old['backend']['id']
        report.update(status='completed',all_passed=True,
            boundary='Isolated inspection runs both old immutable images behind a fixture write gate. Old schema cannot inherit task locks; reconciliation is required before normal service restart.')
    except BaseException as exc:
        report.update(status='failed',all_passed=False,error=str(exc));raise
    finally:
        try:docker.cleanup()
        finally:save_json(out/'verification.json',report)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base-images',type=Path,required=True)
    p.add_argument('--candidate-images',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    run(p.parse_args())
