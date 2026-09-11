import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("APP_TOKEN", "test-token-only")
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.models import Base, Channel, Video, utcnow
from datetime import timedelta

@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool)
    from sqlalchemy import event
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        s.add(Channel(id="channel", title="Test", subscribers=600))
        s.flush()
        for id in ("a", "b"):
            s.add(Video(id=id, channel_id="channel", title=id, published_at=utcnow()-timedelta(days=45), duration_seconds=600))
        s.commit()
        yield s
    engine.dispose()
