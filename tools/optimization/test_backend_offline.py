"""CI and local backend suite with an explicit AWS network prohibition."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
sys.path.insert(0, str(ROOT / 'backend'))
suite = unittest.defaultTestLoader.discover(str(ROOT / 'backend/tests'), pattern='test*.py')
with patch('botocore.endpoint.Endpoint._send', side_effect=AssertionError('AWS transport forbidden')) as transport:
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('AWS_TRANSPORT_ATTEMPTS', transport.call_count)
    raise SystemExit(0 if result.wasSuccessful() and transport.call_count == 0 else 1)
