"""AWS boto3 construction with explicit credentials and one proxy policy."""
from collections import defaultdict

import boto3

from app.proxy_config import proxy_config


def make_client(service, access_key_id, secret_access_key, region, proxy_url=None, **kwargs):
    return boto3.client(service, region_name=region, aws_access_key_id=access_key_id,
                        aws_secret_access_key=secret_access_key, config=proxy_config(proxy_url), **kwargs)


def group_credentials(credentials):
    groups = defaultdict(list)
    for credential in credentials:
        account_id = getattr(credential, "account_id", None)
        if account_id and len(account_id) == 12 and account_id.isdigit():
            groups[(credential.user_id, account_id)].append(credential)
    return {key: tuple(sorted(value, key=lambda item: (not item.is_default, item.id)))
            for key, value in groups.items()}


def select_scan_credentials(credentials):
    """Pick one preferred credential per confirmed account; isolate unknown identities."""
    groups = {}
    for credential in credentials:
        account_id = getattr(credential, "account_id", None)
        identity = ((credential.user_id, account_id) if account_id and len(account_id) == 12
                    and account_id.isdigit() else (credential.user_id, "credential", credential.id))
        groups.setdefault(identity, []).append(credential)
    return [sorted(group, key=lambda item: (not item.is_default, item.id))[0]
            for group in groups.values()]
