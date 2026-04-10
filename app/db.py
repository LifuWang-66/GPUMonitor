import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

settings = get_settings()
if settings.database_url.startswith('sqlite'):
    sqlite_path = unquote(urlsplit(settings.database_url).path)
    if sqlite_path:
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)

connect_args = {'check_same_thread': False, 'timeout': 30} if settings.database_url.startswith('sqlite') else {}
engine_kwargs = {'connect_args': connect_args}
if settings.database_url.startswith('sqlite'):
    engine_kwargs['poolclass'] = NullPool
engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


if settings.database_url.startswith('sqlite'):
    @event.listens_for(engine, 'connect')
    def _set_sqlite_pragmas(dbapi_connection, _):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA busy_timeout=30000;')
        cursor.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def commit_with_retry(db, retries: int = 5, initial_delay_seconds: float = 0.2) -> None:  # noqa: ANN001
    delay = initial_delay_seconds
    for attempt in range(retries):
        try:
            db.commit()
            return
        except OperationalError as exc:
            message = str(exc).lower()
            if 'database is locked' not in message or attempt == retries - 1:
                raise
            db.rollback()
            time.sleep(delay)
            delay *= 2
