from collections.abc import Generator
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrate_schema() -> None:
    from app import models  # noqa: F401
    from app import query_models  # noqa: F401
    from app import settings_models  # noqa: F401
    from app.migrations import upgrade_database

    if engine.dialect.name == "sqlite":
        upgrade_database(engine, Base.metadata)


def init_db() -> None:
    from app import models  # noqa: F401
    from app import query_models  # noqa: F401
    from app.settings_models import QuerySettings, initial_settings

    if engine.dialect.name == "sqlite":
        # Do not create any tables before the pre-migration snapshot and transaction.
        migrate_schema()
    else:
        Base.metadata.create_all(bind=engine)
        with SessionLocal.begin() as db:
            if db.get(QuerySettings, 1) is None:
                db.add(QuerySettings(**initial_settings(time.time())))
