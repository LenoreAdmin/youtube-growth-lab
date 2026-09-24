from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from app.models import Base
from app import db


def test_frozen_migration_upgrade_downgrade_matches_models(monkeypatch):
    engine = create_engine("sqlite://")
    monkeypatch.setattr(db, "engine", engine)
    cfg=Config("alembic.ini")
    command.upgrade(cfg,"head")
    with engine.connect() as connection:
        diff=compare_metadata(MigrationContext.configure(connection),Base.metadata)
        assert diff == []
    command.downgrade(cfg,"base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    command.upgrade(cfg,"head")
    assert "decisions" in inspect(engine).get_table_names()


def test_v2_additive_upgrade_preserves_v1_snapshot(monkeypatch):
    from sqlalchemy import text
    engine=create_engine("sqlite://")
    monkeypatch.setattr(db,"engine",engine)
    cfg=Config("alembic.ini")
    command.upgrade(cfg,"0002")
    with engine.begin() as c:
        c.execute(text("INSERT INTO channels (id,title,updated_at) VALUES ('keep','existing',CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO videos (id,channel_id,title,published_at,duration_seconds,active) VALUES ('v','keep','existing',CURRENT_TIMESTAMP,60,1)"))
        c.execute(text("INSERT INTO snapshots (video_id,observed_at,views) VALUES ('v',CURRENT_TIMESTAMP,20000)"))
    command.upgrade(cfg,"head")
    command.upgrade(cfg,"head")
    with engine.connect() as c:
        assert c.scalar(text("SELECT views FROM snapshots WHERE video_id='v'")) == 20000
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == "0012"
    engine.dispose()
