"""Upgrade a real original Git version, not a downgraded current model."""
from contextlib import closing
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from app.database import Base
from app import migrations, models
from app.sqlite_backup import file_sha256


class OriginalMigrationTests(unittest.TestCase):
    def test_unversioned_original_database_upgrades_and_v5_failure_rolls_back(self):
        root=Path(__file__).resolve().parents[2]
        archive=subprocess.check_output(['git','archive','024d9d5664fa60a49c4710d129de504bfffbce2c','backend'],cwd=root)
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:bundle.extractall(folder,filter='data')
            database=folder/'original.db'
            env={**os.environ,'DATABASE_URL':'sqlite:///'+database.as_posix(),
                'PYTHONPATH':str(folder/'backend'),'PYTHONDONTWRITEBYTECODE':'1'}
            seed='''from datetime import date
from app.database import init_db,engine,SessionLocal
from app.models import User,AwsCredential,CreditRecord,TrafficUsage,InstanceSetting
init_db()
with SessionLocal() as db:
 u=User(username='original',password_hash='opaque');db.add(u);db.flush()
 c=AwsCredential(user_id=u.id,is_default=True,access_key_id_enc='opaque',secret_access_key_enc='opaque');db.add(c);db.flush()
 db.add(CreditRecord(user_id=u.id,credential_id=c.id,name='historic',total_amount=0.12345678912345678,currency='USD',start_date=date(2026,1,1)))
 db.add(TrafficUsage(user_id=u.id,credential_id=c.id,region='us-east-1',instance_name='historic',year_month='2026-11',network_in_bytes=2**55+17,network_out_bytes=2**54+23))
 db.add(InstanceSetting(user_id=u.id,credential_id=c.id,region='us-east-1',instance_name='historic',monthly_limit_gb=123.5,auto_stop_on_limit=False,note='preserve'))
 db.commit()
engine.dispose()
'''
            result=subprocess.run([sys.executable,'-c',seed],env=env,cwd=folder,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            before=file_sha256(database)
            engine=create_engine('sqlite:///'+database.as_posix())
            try:
                apply=migrations.apply_v5
                def fail(conn,metadata):
                    apply(conn,metadata)
                    raise RuntimeError('after task DDL')
                with patch.object(migrations,'apply_v5',side_effect=fail) as injected:
                    with self.assertRaises(migrations.MigrationError):migrations.upgrade_database(engine,Base.metadata)
                    self.assertEqual(injected.call_count,1)
                self.assertEqual(file_sha256(database),before)
                migrations.upgrade_database(engine,Base.metadata)
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall(),[(1,),(2,),(3,),(4,),(5,),(6,)])
                    self.assertEqual(db.execute('SELECT total_amount_decimal,account_id,amount_precision FROM credit_records').fetchall(),[('0.12345678912345678',None,'legacy_float')])
                    self.assertEqual(db.execute('SELECT network_in_bytes,network_out_bytes FROM traffic_usage').fetchall(),[(2**55+17,2**54+23)])
                    self.assertEqual(db.execute('SELECT note,auto_stop_on_limit,monthly_limit_gb FROM instance_settings').fetchall(),[('preserve',0,123.5)])
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(),('ok',))
                    self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                    for name in ['ix_aws_credentials_account_id','ix_credit_records_account_id']:
                        self.assertIsNotNone(db.execute('SELECT name FROM sqlite_master WHERE type="index" AND name=?',(name,)).fetchone())
                migrations.upgrade_database(engine,Base.metadata)
            finally:engine.dispose()
