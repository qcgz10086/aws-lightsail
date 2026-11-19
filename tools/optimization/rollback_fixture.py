"""Offline production-image rollback drill steps; never operate on user data."""
from contextlib import closing
from datetime import date
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

ROOT=Path('/data')
DB=ROOT/'drill.db'


def inspect(path):
    with closing(sqlite3.connect(path)) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],
            'foreign_keys':db.execute('PRAGMA foreign_key_check').fetchall(),
            'traffic':db.execute('SELECT network_in_bytes,network_out_bytes FROM traffic_usage').fetchall(),
            'policy':db.execute('SELECT instance_name,monthly_limit_gb,auto_stop_on_limit,note FROM instance_settings').fetchall(),
            'credit':db.execute('SELECT name,total_amount,currency FROM credit_records').fetchall(),
            'tasks':db.execute('SELECT id,status FROM operation_tasks ORDER BY id').fetchall() if 'operation_tasks' in tables else [],
            'steps':db.execute('SELECT task_id,name,status,intent_json FROM operation_task_steps ORDER BY task_id').fetchall() if 'operation_task_steps' in tables else [],
            'locks':db.execute('SELECT task_id FROM operation_task_locks ORDER BY task_id').fetchall() if 'operation_task_locks' in tables else [],
            'has_task_schema':'operation_tasks' in tables}


def unknown_task(queue, name):
    from app.database import SessionLocal,engine
    from app.models import User,AwsCredential,OperationTask
    from app.services.task_queue import TaskContext
    with SessionLocal() as db, patch.object(queue,'schedule'):
        result=queue.accept(db,db.get(User,1),db.get(AwsCredential,1),'reboot',
            {'region':'us-east-1','resource_name':name},name,region='us-east-1',resource_name=name)
    token='fixture-'+name
    with SessionLocal() as db:
        row=db.get(OperationTask,result['task_id']);row.status='running';row.worker_token=token;row.lease_until=time.time()+3600
        db.commit()
    context=TaskContext(engine,result['task_id'],token,{})
    step=context.begin_step('reboot',{'mutation':True,'name':name,'arn':'arn:aws:lightsail:us-east-1:111111111111:Instance/'+name})
    context.finish_step(step,status='unknown',error='fixture write response lost')
    return result['task_id']


def main(mode):
    if mode=='serve':
        from app import main as application
        from fastapi.responses import JSONResponse
        import uvicorn
        application.start_scheduler=lambda:None
        application.stop_scheduler=lambda:None
        @application.app.middleware('http')
        async def inspection_only(request,call_next):
            if request.method not in ('GET','HEAD') and request.url.path!='/api/auth/login':
                return JSONResponse({'detail':'Rollback inspection only: unresolved task reconciliation required'},status_code=503)
            return await call_next(request)
        from tools.optimization.fake_aws import block_external_connections
        with patch('botocore.endpoint.Endpoint._send',side_effect=AssertionError('AWS forbidden')),block_external_connections():
            uvicorn.run(application.app,host='0.0.0.0',port=8000,access_log=False)
        return
    from app.database import init_db,engine,SessionLocal,Base
    from app.models import User,AwsCredential,CreditRecord,TrafficUsage,InstanceSetting
    from app.security import encrypt_secret,hash_password
    with patch('botocore.endpoint.Endpoint._send',side_effect=AssertionError('AWS forbidden')) as transport:
        if mode=='seed':
            init_db()
            with engine.begin() as db:db.exec_driver_sql('PRAGMA journal_mode=WAL')
            with SessionLocal() as db:
                user=User(username='rollback-fixture',password_hash=hash_password('T07RollbackPass123!'));db.add(user);db.flush()
                cred=AwsCredential(user_id=user.id,is_default=True,access_key_id_enc=encrypt_secret('fixture'),secret_access_key_enc=encrypt_secret('fixture'));db.add(cred);db.flush()
                db.add(CreditRecord(user_id=user.id,credential_id=cred.id,name='historic-credit',total_amount=0.12345678912345678,currency='USD',start_date=date(2026,1,1)))
                db.add(TrafficUsage(user_id=user.id,credential_id=cred.id,region='us-east-1',instance_name='historic',year_month='2026-11',network_in_bytes=2**55+17,network_out_bytes=2**54+23))
                db.add(InstanceSetting(user_id=user.id,credential_id=cred.id,region='us-east-1',instance_name='historic',monthly_limit_gb=123.5,auto_stop_on_limit=False,note='retain-policy'))
                db.commit()
            report={'mode':mode,'database':inspect(DB),'wal_bytes':Path(str(DB)+'-wal').stat().st_size}
        else:
            from app.sqlite_backup import snapshot_database,restore_database,file_sha256
            from app import migrations
            if mode=='fail_upgrade':
                backup=snapshot_database(DB,ROOT/'legacy.db')
                (ROOT/'legacy-receipt.json').write_text(json.dumps(backup))
                before=inspect(DB)
                apply=migrations.apply_v5
                def fail(conn,metadata):
                    apply(conn,metadata)
                    raise RuntimeError('injected after task DDL')
                with patch.object(migrations,'apply_v5',side_effect=fail) as injected:
                    try:init_db()
                    except migrations.MigrationError as exc:
                        assert isinstance(exc.__cause__, RuntimeError) and str(exc.__cause__)=='injected after task DDL'
                    else:raise AssertionError('injected migration did not fail')
                    assert injected.call_count==1
                after=inspect(DB);assert after==before
                report={'mode':mode,'backup':backup,'injected_v5_calls':1,'injected_cause':'injected after task DDL',
                    'unchanged_business_and_schema':True,'database':after}
            elif mode=='tasks':
                init_db()
                from app.services.task_queue import DurableTaskQueue
                from app.models import OperationTask
                with SessionLocal() as db:
                    db.get(AwsCredential,1).account_id='111111111111';db.get(AwsCredential,1).last_confirmed_account_id='111111111111';db.commit()
                queue=DurableTaskQueue(engine)
                try:
                    a=unknown_task(queue,'before-backup')
                    keeper=sqlite3.connect(DB);keeper.execute('PRAGMA wal_autocheckpoint=0')
                    backup=snapshot_database(DB,ROOT/'current.db')
                    b=unknown_task(queue,'after-backup')
                    wal=Path(str(DB)+'-wal').stat().st_size;assert wal>0
                    queue.shutdown(wait=True);engine.dispose();keeper.close()
                    receipt=restore_database(Path(backup['path']),DB,backup['sha256'],offline=True)
                    safety=inspect(receipt['safety_backup']['path']);restored=inspect(DB)
                    assert {row[0] for row in safety['tasks']}=={a,b}
                    assert {row[0] for row in restored['tasks']}=={a}
                    queue=DurableTaskQueue(engine)
                    with patch.object(queue,'schedule') as scheduled:
                        queue.recover();assert scheduled.call_count==0
                    recovered=inspect(DB)
                    assert recovered['tasks']==[[a,'reconciliation_required']] or recovered['tasks']==[(a,'reconciliation_required')]
                    assert len(recovered['locks'])==1 and recovered['steps'][0][2]=='unknown'
                    queue.shutdown(wait=True);engine.dispose()
                    # Return to the complete safety snapshot before the separate legacy rollback.
                    back=restore_database(Path(receipt['safety_backup']['path']),DB,receipt['safety_backup']['sha256'],offline=True)
                    report={'mode':mode,'before_backup_task':a,'after_backup_task':b,'wal_bytes':wal,'current_restore':receipt,
                        'safety_inventory':safety,'restored_inventory':restored,'recovered_inventory':recovered,'return_to_safety':back,
                        'service_may_resume':False,'restart_schedules':0}
                finally:queue.shutdown(wait=True)
            elif mode=='restore_legacy':
                before=inspect(DB);backup=json.loads((ROOT/'legacy-receipt.json').read_text())
                engine.dispose()
                receipt=restore_database(Path(backup['path']),DB,backup['sha256'],offline=True)
                safety=inspect(receipt['safety_backup']['path']);restored=inspect(DB)
                assert safety==before and len(safety['tasks'])==2 and len(safety['locks'])==2
                assert not restored['has_task_schema'] and restored['integrity']=='ok' and not restored['foreign_keys']
                assert all(restored[key]==safety[key] for key in ['traffic','policy','credit'])
                report={'mode':mode,'receipt':receipt,'safety_inventory':safety,'restored_inventory':restored,
                    'unresolved_tasks':safety['tasks'],'service_may_resume':False,
                    'reason':'Legacy schema has no task locks; unresolved effects remain in safety snapshot. Only isolated read inspection allowed.'}
            else:raise ValueError(mode)
        report['transport_attempts']=transport.call_count;assert transport.call_count==0
        (ROOT/(mode+'.json')).write_text(json.dumps(report,indent=2))
        print(json.dumps(report))
    engine.dispose()


if __name__=='__main__':main(sys.argv[1])
