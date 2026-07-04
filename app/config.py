from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    admin_password: str = "changeme"
    session_secret: str = "dev-secret-change-in-production"

    database_url: str = "sqlite+aiosqlite:///./munus.db"

    timezone: str = "America/New_York"

    # Season the required-hours total is counted from. Blank = count all approved hours.
    # Stored here as the default; the admin Settings page persists an override in app_settings.
    season_start: str = ""

    # Public base URL used when Slack messages link back to the student portal.
    base_url: str = "http://localhost:8001"

    # DM a signed-up student this many hours before their shift starts.
    reminder_lead_hours: int = 24

    # Weekly season-progress DM
    weekly_dm_day: int = 6   # 0=Mon ... 6=Sun
    weekly_dm_time: str = "21:00"  # HH:MM 24h local time


settings = Settings()
