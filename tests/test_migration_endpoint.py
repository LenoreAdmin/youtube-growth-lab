"""Der Migrationsendpunkt darf nicht an einer handgepflegten Variablen haengen.

Der Produktionsausfall: `DATABASE_URL` und `MIGRATION_DATABASE_URL` waren von Hand eingetragen und
wurden nach einer Rotation der Zugangsdaten ungueltig – Production und der Build konnten die Datenbank
nicht mehr erreichen. Die Neon-Integration pflegt `DATABASE_URL_UNPOOLED` selbst; darauf faellt der
Build jetzt zurueck.
"""
import os
from app.config import Settings


def _settings(**env):
    base = {"DATABASE_URL": "postgresql://user:pw@host/db", "APP_TOKEN": "x"*40, "CRON_SECRET": "y"*40,
            "MIGRATION_DATABASE_URL": "", "DATABASE_URL_UNPOOLED": ""}
    previous = {key: os.environ.get(key) for key in set(base) | set(env)}
    os.environ.update({key: value for key, value in {**base, **env}.items()})
    try:
        return Settings(_env_file=None)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_integration_maintained_endpoint_is_used_when_no_own_one_is_set():
    found = _settings(DATABASE_URL_UNPOOLED="postgresql://user:pw@direct/db")
    assert found.direct_database_url == "postgresql://user:pw@direct/db"


def test_an_explicit_migration_endpoint_still_wins():
    found = _settings(MIGRATION_DATABASE_URL="postgresql://user:pw@own/db",
                      DATABASE_URL_UNPOOLED="postgresql://user:pw@direct/db")
    assert found.direct_database_url == "postgresql://user:pw@own/db"


def test_without_either_there_is_no_direct_endpoint():
    assert _settings().direct_database_url == ""


def test_the_production_build_names_both_variables_when_neither_is_set(monkeypatch):
    from app import release
    settings = _settings()
    monkeypatch.setattr(settings, "vercel_env", "production", raising=False)
    monkeypatch.setattr(release, "settings", settings)
    try:
        release.build()
        raise AssertionError("ohne direkten Endpunkt darf der Build nicht migrieren")
    except ValueError as exc:
        assert "MIGRATION_DATABASE_URL" in str(exc) and "DATABASE_URL_UNPOOLED" in str(exc)


def test_the_production_build_migrates_through_the_fallback(monkeypatch):
    from app import release
    settings = _settings(DATABASE_URL_UNPOOLED="postgresql://user:pw@direct/db",
                         GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret",
                         GOOGLE_REFRESH_TOKEN="token", CHANNEL_ID="channel")
    monkeypatch.setattr(settings, "vercel_env", "production", raising=False)
    monkeypatch.setattr(release, "settings", settings)
    used = {}
    monkeypatch.setattr(release, "migrate", lambda url, hosted=False: used.update(url=url, hosted=hosted))
    monkeypatch.setattr(release, "check_schema", lambda url: used.update(checked=url))
    release.build()
    assert used["url"] == "postgresql://user:pw@direct/db" and used["hosted"] is True
    assert used["checked"] == settings.database_url
