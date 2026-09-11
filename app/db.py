from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def sqlite_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
Session = sessionmaker(engine, expire_on_commit=False)
