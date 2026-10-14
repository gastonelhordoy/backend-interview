import time

from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base


def _create_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            # One shared connection, otherwise every connection gets its own empty DB.
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):
            # SQLite ignores FOREIGN KEY / ON DELETE unless enabled per connection.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = _create_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables():
    """
    Create all tables in the database.
    """
    # Several processes starting on the same database (mesh nodes, workers) can race
    # between "table missing?" and CREATE TABLE. Retrying skips the tables the other
    # process already created.
    for attempt in range(5):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def get_db():
    """
    Dependency to get database session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
