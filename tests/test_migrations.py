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
