from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: str = "development"
    vercel: str = ""
    vercel_env: str = ""
    database_url: str = Field(default="sqlite:///work.db", repr=False)
    migration_database_url: str = Field(default="", repr=False)
    app_token: str = Field(default="", repr=False)
    cron_secret: str = Field(default="", repr=False)
    channel_id: str = ""
    focus_video_id: str = ""
    google_client_id: str = Field(default="", repr=False)
    google_client_secret: str = Field(default="", repr=False)
    google_refresh_token: str = Field(default="", repr=False)
    google_client_file: str = "secrets/client_secret.json"
    google_token_file: str = "secrets/token.json"
    enable_revenue: bool = False
    enable_reach: bool = True
    sync_interval_seconds: int = 3600
    analytics_lag_days: int = 3
    sync_budget_seconds: int = Field(default=210, ge=30, le=210)
    # Öffentliche Web-Suche für Audience-Pools außerhalb der eigenen Reichweite. Ohne Schlüssel bleibt
    # die Funktion aus; das Tageslimit liegt bewusst unter dem kostenlosen Kontingent des Providers.
    web_search_provider: str = ""
    web_search_key: str = Field(default="", repr=False)
    web_search_cx: str = Field(default="", repr=False)
    web_search_daily_limit: int = Field(default=50, ge=0, le=100)

    @property
    def hosted(self):
        return self.vercel == "1" or self.vercel_env in ("production", "preview") or self.app_env == "production"

    @model_validator(mode="after")
    def production_storage(self):
        try:
            url = make_url(self.database_url)
        except Exception:
            raise ValueError("DATABASE_URL must be a valid database connection URL.") from None
        if url.drivername in ("postgres", "postgresql"):
            url = url.set(drivername="postgresql+psycopg")
        if self.hosted:
            if url.drivername != "postgresql+psycopg":
                raise ValueError("Hosted environments require persistent PostgreSQL; SQLite is forbidden.")
            for name in ("app_token", "cron_secret"):
                value = getattr(self, name)
                if len(value) < 32 or value.startswith("REPLACE_"):
                    raise ValueError(f"{name.upper()} must be an independent random secret of at least 32 characters.")
            if self.app_token == self.cron_secret:
                raise ValueError("APP_TOKEN and CRON_SECRET must differ.")
            if "sslmode" not in url.query:
                url = url.update_query_dict({"sslmode": "require"})
        self.database_url = url.render_as_string(hide_password=False)
        return self


settings = Settings()
