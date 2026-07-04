# Munus — Codebase Guide

Volunteer-hour tracker for FRC teams 4143 (MARS/WARS) and 4423 (MARS' Minions).
Students browse volunteer **opportunities**, sign up for dated **shifts**, and submit
**hours** that a mentor approves. Season requirements are driven by student **level**.
FastAPI + SQLAlchemy (async) + Jinja2 + SQLite. Slack integration for `/vhours` and
interactive Approve/Reject of submissions.

Sibling app to **Tempus** (attendance/hours kiosk) and intentionally mirrors its stack,
dark styling, and conventions — but is a fully separate app with its own DB, Slack app,
and systemd service (port 8001). Nothing is imported across the two projects.

## Running

```bash
source venv/bin/activate
uvicorn app.main:app --reload --port 8001
```

Requires a `.env` file (see `.env.example`). Key vars: `SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET`, `ADMIN_PASSWORD`, `SESSION_SECRET`, `BASE_URL`.

## Testing

```bash
pytest
```

In-memory SQLite with async fixtures via `pytest-asyncio`. **Do not mock the database** —
tests hit a real (in-memory) DB to catch query bugs.

## Project Layout

```
app/
  main.py            # FastAPI app, router wiring, lifespan (init_db + scheduler)
  config.py          # Settings (pydantic-settings, reads .env)
  database.py        # Engine, session, init_db(), seed level requirements
  models.py          # ORM models + StudentLevel labels/defaults
  utils.py           # Timezone helpers + shift-range formatting
  routers/
    portal.py        # Student-facing: identify by code, browse, sign up, submit hours
    admin.py         # Password-protected management UI
    slack.py         # /vhours slash command + /interact (approve/reject)
  services/
    opportunities.py # Shift capacity checks, signup/cancel logic
    submissions.py   # Create submission -> DM reviewer; approve/reject -> notify student
    requirements.py  # Season required hours by level; season-total calc
    scheduler.py     # APScheduler: pre-shift reminders, post-shift prompts, weekly DM
    slack_client.py  # AsyncWebClient wrapper + send_dm
    audit.py         # Append-only mutation log
    app_settings.py  # Persisted runtime settings (season_start)
```

## Key Conventions

### Datetimes
All datetimes in the database are **naive UTC** (`app/utils.py`):
- Display: `utc_to_local(dt)` / `format_shift_range(start, end)`
- DB queries / form parsing: `local_to_utc(dt)`
- `now_utc()` for "now" (matches stored values)

### Student identity (portal)
No passwords. Students identify with their `student_code` (auto-generated
`sha256(name)[:8]`); the id is stored in a signed cookie (`munus_student`). Admin sessions
use a separate signed cookie (`admin_session`), same pattern as Tempus.

### Requirements & season total
Required hours come from the `level_requirements` table (admin-editable, seeded from
`DEFAULT_LEVEL_HOURS` = freshman 5 / 4423 10 / 4143 15). The season total is the sum of
**approved** `HourSubmission.hours` since the `season_start` cutoff
(`services/app_settings.py`; blank = count all).

### Submission approval
`services/submissions.py` owns Slack block building + DMs so both the portal and the Slack
router can trigger notifications without a circular import. Flow: student submits and picks
a reviewer → `notify_reviewer` DMs Approve/Reject buttons → `/slack/interact` calls
`set_status` → `notify_student_of_review` DMs the outcome. Admins can do the same from
`/admin/submissions`.

### Database migrations
No Alembic. Add a `def _migration(conn)` guarded by `inspect(conn)` in `database.py` and
call it from `init_db()`, mirroring Tempus.

## UI Conventions

Single dark theme shared with Tempus (`#0a0a0a` bg, `#111111` panels, accent red
`#cc2200`, borders `#2a1a1a`). Admin pages extend `admin/base.html` (Bootstrap 5 with
kiosk-color overrides); the student portal extends `portal/base.html` (same palette). Don't
add Bootstrap default light classes.

## Scheduled Jobs (`scheduler.py`)

| Job | Trigger |
|-----|---------|
| Pre-shift reminders | every 30 min (DMs shifts within `REMINDER_LEAD_HOURS`) |
| Post-shift submit prompts | every 30 min (DMs after a shift ends, once) |
| Weekly season-progress DM | `WEEKLY_DM_DAY` at `WEEKLY_DM_TIME` |
