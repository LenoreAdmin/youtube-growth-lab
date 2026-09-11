from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import sessionmaker
from .config import settings


def make_engine(url):
    options = dict(pool_pre_ping=True)
    if url.startswith("postgres"):
        # No persistent backend session assumptions; compatible with transaction poolers.
        options.update(poolclass=NullPool, connect_args={"connect_timeout": 10, "prepare_threshold": None})
    result = create_engine(url, **options)
    if result.dialect.name == "sqlite":
        @event.listens_for(result, "connect")
        def sqlite_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    return result


engine = make_engine(settings.database_url)
Session = sessionmaker(engine, expire_on_commit=False)
