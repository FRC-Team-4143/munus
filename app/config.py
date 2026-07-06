from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    # Channel to announce new opportunities in (Slack channel ID, e.g. C0ABCDE123).
    # Blank = announcements disabled. The bot must be a member of this channel.
    slack_announce_channel: str = ""

    admin_password: str = "changeme"
    # Optional limited login that can ONLY create/manage opportunities & shifts.
    # Blank = the manager login is disabled.
    manager_password: str = ""
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

    # Auto-reject a signed-up shift a student never logged, this many days after it ends
    # (records a rejected submission so it stops counting toward projected hours). 0 = off.
    auto_reject_days: int = 7

    # Database backups (SQLite only)
    backup_dir: str = "backups"
    backup_keep: int = 14  # number of snapshots to retain
    backup_time: str = "23:30"  # HH:MM 24h local time for the weekly snapshot
    backup_day: str = "sun"  # day of week for the weekly backup (mon-sun)

    # Global toggle for all automated updates (Slack messages, reminders, scheduled jobs)
    updates_enabled: bool = True


settings = Settings()
