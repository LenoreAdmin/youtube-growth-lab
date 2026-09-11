"""Deploy-time migrations only: never execute DDL in a request or at import."""
import argparse
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from .config import settings
from .db import make_engine

ROOT = Path(__file__).resolve().parents[1]


def migration_config():
    config = Config(str(ROOT/"alembic.ini"))
    config.set_main_option("script_location", str(ROOT/"migrations"))
    return config


def migrate(url, hosted=False):
    parsed = make_url(url)
    if parsed.drivername in ("postgres", "postgresql"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    if hosted and parsed.drivername != "postgresql+psycopg":
        raise ValueError("Production migrations require a direct PostgreSQL URL.")
    if hosted and "sslmode" not in parsed.query:
        parsed = parsed.update_query_dict({"sslmode": "require"})
    engine = make_engine(parsed.render_as_string(hide_password=False))
    try:
        with engine.begin() as connection:
            if engine.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL lock_timeout = '10s'"))
                connection.execute(text("SET LOCAL statement_timeout = '60s'"))
                connection.execute(text("SELECT pg_advisory_xact_lock(71402952)"))
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def check_schema(url):
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            current = set(MigrationContext.configure(connection).get_current_heads())
            expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
            if current != expected:
                raise RuntimeError("Database schema is not at this release's head; run the migration step first.")
    finally:
        engine.dispose()


def build():
    if settings.hosted and settings.vercel_env not in ("production", "preview", "development"):
        raise ValueError("Hosted build requires VERCEL_ENV; expose Vercel system environment variables.")
    if settings.vercel_env == "production":
        if not settings.migration_database_url:
            raise ValueError("Production build requires MIGRATION_DATABASE_URL (direct PostgreSQL connection).")
        for name in ("google_client_id", "google_client_secret", "google_refresh_token", "channel_id"):
            value = getattr(settings, name)
            if not value or value.startswith("REPLACE_"):
                raise ValueError("Production build requires complete Google OAuth environment and CHANNEL_ID.")
        migrate(settings.migration_database_url, hosted=True)
        check_schema(settings.database_url)
        print("Production schema upgraded and verified.")
    else:
        # Preview builds never migrate any database.
        print("Preview/local build: no database migrations performed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "migrate", "check"])
    args = parser.parse_args()
    try:
        if args.command == "build":
            build()
        elif args.command == "migrate":
            if settings.hosted and not settings.migration_database_url:
                raise ValueError("MIGRATION_DATABASE_URL is required for hosted migrations.")
            migrate(settings.migration_database_url or settings.database_url, hosted=settings.hosted)
            print("Schema upgraded.")
        else:
            check_schema(settings.database_url)
            print("Schema verified.")
    except Exception as exc:
        # SQLAlchemy/psycopg errors may contain connection details: never print raw exceptions.
        print(f"Release step failed ({type(exc).__name__}); check database configuration and migration state.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
