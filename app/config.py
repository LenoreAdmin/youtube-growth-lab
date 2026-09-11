from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///work.db"
    app_token: str = ""
    channel_id: str = ""
    focus_video_id: str = ""
    google_client_file: str = "secrets/client_secret.json"
    google_token_file: str = "secrets/token.json"
    enable_revenue: bool = False
    enable_reach: bool = True
    sync_interval_seconds: int = 3600
    analytics_lag_days: int = 3


settings = Settings()
