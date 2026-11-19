"""Versioned, offline fixture for paired T00/final production-image measurement."""
from datetime import date, datetime, timedelta
from decimal import Decimal
import argparse
import time
from pathlib import Path

from tools.optimization import benchmark_server as original
from tools.optimization.fake_aws import FixtureAws as OriginalAws, _Client


class SdkClient(_Client):
    def __init__(self, *args):
        super().__init__(*args)
        self._make_api_call = self._invoke

    def _invoke(self, operation, parameters):
        import re
        name = re.sub(r'(?<!^)(?=[A-Z])', '_', operation).lower()
        return self.fixture.invoke(self.service, self.key, self.region, name, parameters)

    def __getattr__(self, name):
        if name not in self.fixture.ALLOWED[self.service]:
            return super().__getattr__(name)
        return lambda **params: self._make_api_call(''.join(part.title() for part in name.split('_')), params)


class VersionedAws(OriginalAws):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.generation = 1
        global fixture
        fixture = self

    def client(self, service_name, **kwargs):
        original_client = super().client(service_name, **kwargs)
        return SdkClient(self, service_name, original_client.key, original_client.region)

    def _response(self, service, key, region, operation, params):
        if service == 'ce':
            amount = Decimal(self.data['billing']['total']) + self.generation
            net = Decimal(self.data['billing']['net']) + self.generation
            if operation == 'get_cost_forecast':
                start = date.fromisoformat(params['TimePeriod']['Start'])
                end = date.fromisoformat(params['TimePeriod']['End'])
                return {'Total': {'Amount': str(Decimal((end - start).days) * 3), 'Unit': 'USD'},
                    'ForecastResultsByTime': [{'TimePeriod': {'Start': (start + timedelta(days=n)).isoformat(),
                        'End': (start + timedelta(days=n+1)).isoformat()}, 'MeanValue': '3'}
                        for n in range((end-start).days)]}
            period = {'TimePeriod': params['TimePeriod'], 'Estimated': True}
            groups = params.get('GroupBy') or []
            if any(group.get('Key') == 'RECORD_TYPE' for group in groups):
                period['Groups'] = [{'Keys': ['Amazon Lightsail', kind],
                    'Metrics': {'UnblendedCost': {'Amount': str(value), 'Unit': 'USD'}}}
                    for kind, value in [('Usage', amount), ('Credit', net - amount)]]
            elif groups:
                period['Groups'] = [{'Keys': ['Amazon Lightsail'], 'Metrics': {
                    'UnblendedCost': {'Amount': str(amount), 'Unit': 'USD'},
                    'NetUnblendedCost': {'Amount': str(net), 'Unit': 'USD'}}}]
            else:
                credit = '"Credit"' in __import__('json').dumps(params.get('Filter', {}))
                period['Total'] = {'UnblendedCost': {'Amount': str(net - amount if credit else amount), 'Unit': 'USD'},
                                   'NetUnblendedCost': {'Amount': str(net), 'Unit': 'USD'}}
            return {'ResultsByTime': [period]}
        response = super()._response(service, key, region, operation, params)
        if operation == 'get_instances':
            for item in response['instances']:
                item['blueprintName'] = f'Fixture-generation-{self.generation}'
                item['publicIpAddress'] = item['publicIpAddress'].replace('203.0.113.', f'203.0.{self.generation}.')
                item['state'] = {'name': 'running' if self.generation % 2 else 'stopped', 'code': 16}
        return response


seed_original = original.seed
def seed(aws, samples):
    seed_original(aws, samples)
    from app.database import SessionLocal
    from app.models import AwsCredential, CreditRecord, TrafficUsage, User
    from app.security import decrypt_secret
    with SessionLocal() as db:
        for cred in db.query(AwsCredential).all():
            if hasattr(cred, 'account_id'):
                cred.account_id = aws.data['keys'][decrypt_secret(cred.access_key_id_enc)]
                cred.last_confirmed_account_id = cred.account_id
                for row in db.query(TrafficUsage).filter(TrafficUsage.credential_id == cred.id):
                    row.account_id = cred.account_id
                for row in db.query(CreditRecord).filter(CreditRecord.credential_id == cred.id):
                    row.account_id = cred.account_id
                    row.total_amount_decimal = '100'
                    row.amount_precision = 'decimal'
        db.commit()
        identities = {user.username: [dict(id=cred.id, account_id=aws.data['keys'][decrypt_secret(cred.access_key_id_enc)],
            selected=decrypt_secret(cred.access_key_id_enc) != 'T00_ALPHA_DUPLICATE') for cred in user.credentials]
            for user in db.query(User).all()}
    from app.main import app
    @app.get('/__baseline__/identities')
    def seeded_identities():
        return identities
    @app.post('/__baseline__/generation/{value}')
    def generation(value: int):
        if aws.snapshot()['active']:
            raise RuntimeError('cannot advance fixture while requests active')
        aws.generation = value
        return {'generation': value, 'epoch_ms': time.time() * 1000}
    # The compatibility module uses actual production routes; no API payload
    # rewriting, readiness shortcuts, or disabled cache/poll mechanisms.


if __name__ == '__main__':
    original.FixtureAws = VersionedAws
    original.seed = seed
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--delay-ms', type=int, default=80)
    original.serve(parser.parse_args())
