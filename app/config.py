from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": tolerate leftover keys in a deployed .env (e.g. the retired
    # ADMIN_PASSWORD/MANAGER_PASSWORD/SESSION_SECRET) instead of failing to boot.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    # Channel to announce new opportunities in (Slack channel ID, e.g. C0ABCDE123).
    # Blank = announcements disabled. The bot must be a member of this channel.
    slack_announce_channel: str = ""

    # Legion SSO — both /admin and the student portal are gated by the shared `mw_sso`
    # cookie. Munus only *verifies* the cookie (Legion mints it); `sso_secret` must
    # equal Legion's SSO_SECRET. There is no local admin password or student token —
    # the first admin is granted `munus-admin` in Legion's /admin/groups.
    sso_secret: str = ""
    sso_session_ttl: int = 43200  # 12h; must match Legion's cookie max_age
    sso_cookie_domain: str = ""   # e.g. ".marswars.org" so one login spans subdomains

    # Legion roster API + one-tap SSO challenge — the read-only source of truth Munus
    # mirrors from, and the server-to-server trigger for /vhours's one-tap sign-in link.
    legion_base_url: str = ""     # e.g. "https://legion.marswars.org"
    legion_api_key: str = ""      # presented as X-API-Key to Legion's /api/* and /sso/challenge

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
