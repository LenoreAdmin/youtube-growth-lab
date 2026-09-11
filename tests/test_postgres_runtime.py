"""Real PostgreSQL release/lease checks; isolated CI service, never production."""
import os
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from app.db import make_engine
from app.release import migrate, check_schema
from app.jobs import acquire, release


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="No isolated PostgreSQL test service configured")
def test_postgres_migration_and_concurrent_leases():
    url = os.environ["TEST_POSTGRES_URL"]
    from sqlalchemy.engine import make_url
    assert make_url(url).database == "growth_test", "Only the disposable CI database is allowed."
    migrate(url)
    check_schema(url)
    engine = make_engine(url)
    factory = sessionmaker(engine,expire_on_commit=False)
    try:
        # Dedicated test service; only this test uses the lease table.
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM job_leases WHERE name = 'youtube-sync'"))
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: acquire(factory,"postgres-test-hour"), range(4)))
        winners = [owner for owner,_ in results if owner]
        assert len(winners) == 1
        release(factory,winners[0],"postgres-test-hour")
        assert acquire(factory,"postgres-test-hour")[1] == "already_completed"
    finally:
        engine.dispose()
