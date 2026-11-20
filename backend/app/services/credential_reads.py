"""Capture authorization once and recheck it at every direct AWS read send."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.read_throttle import read_context, read_scope
from app.services.snapshots import version_for


@dataclass(frozen=True)
class CredentialReadGuard:
    engine: object
    user_id: int
    user_version: int
    credential_id: int | None
    credential_version: str | None
    account_id: str | None
    allow_invalid: bool = False

    @classmethod
    def capture(cls, engine, user, credential=None, *, allow_invalid=False, proposed=False):
        return cls(engine, user.id, user.token_version,
            credential.id if credential is not None else None,
            version_for(user, credential) if credential is not None else None,
            credential.account_id if credential is not None and not proposed else None,
            allow_invalid)

    def _check_connection(self, connection):
        if self.credential_id is None:
            row = connection.exec_driver_sql("SELECT token_version FROM users WHERE id=?", (self.user_id,)).first()
            valid = row is not None and row[0] == self.user_version
        else:
            row = connection.exec_driver_sql("""SELECT u.id,u.token_version,c.id,c.account_id,
                c.access_key_id_enc,c.secret_access_key_enc,c.proxy_url_enc,
                EXISTS(SELECT 1 FROM data_snapshots s WHERE s.credential_id=c.id AND s.version=?
                       AND s.kind='billing-identity' AND s.error_status='credential_invalid')
                FROM users u JOIN aws_credentials c ON c.user_id=u.id WHERE u.id=? AND c.id=?""",
                (self.credential_version, self.user_id, self.credential_id)).first()
            actual = hashlib.sha256(json.dumps(list(row[:7]), separators=(",", ":")).encode()).hexdigest() if row else None
            valid = actual == self.credential_version and (self.allow_invalid or not row[7])
        if not valid:
            raise HTTPException(status_code=409, detail="用户授权或凭证版本已变化，请重新加载后重试")

    def check(self):
        # A new connection prevents an ORM identity map or an earlier request
        # transaction from hiding deletion, rotation or token revocation.
        with self.engine.connect() as connection:
            self._check_connection(connection)

    @contextmanager
    def context(self):
        self.check()
        with read_context(read_scope(self.user_id, self.account_id,
                                    self.credential_id if self.credential_id is not None else "proposed"), self.check):
            try:
                yield
            finally:
                # Service adapters may downgrade ordinary AWS errors; a revoked
                # request must still fail and must never publish a local result.
                self.check()

    def call(self, method, *args, **kwargs):
        with self.context():
            return method(*args, **kwargs)

    def reserve_write(self, db: Session):
        # Reserve the writer before flushing the route's staged changes. Verify
        # against committed columns, not already-mutated ORM attributes.
        with db.no_autoflush:
            connection = db.connection()
            connection.exec_driver_sql("UPDATE users SET id=id WHERE id=?", (self.user_id,))
            self._check_connection(connection)
