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
`SLACK_ANNOUNCE_CHANNEL` (optional) enables new-opportunity announcements.

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
    opportunities.py # Shift capacity checks, signup/cancel logic, new-opportunity announce
    submissions.py   # Create submission -> DM reviewer; approve/reject -> notify student
    requirements.py  # Season required hours by level; season-total calc
    reports.py       # Batched roster progress report (approved/projected/required)
    backup.py        # SQLite snapshot backup + staged restore (VACUUM INTO)
    scheduler.py     # APScheduler: pre-shift reminders, post-shift prompts, weekly DM, backup
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
No passwords. Two ways in, both landing on the dashboard home (`portal/home.html`):
- **Slack magic link (primary):** `/vhours` returns a "📊 Open my dashboard" button whose
  URL is `/enter?token=...` — a signed, 14-day token (`services/student_auth.py`
  `make_magic_token`/`read_magic_token`). `GET /enter` validates it and sets the session
  cookie. `magic_link(student_id, next_path)` supports deep links (e.g. the post-shift DM
  links straight to `/submit`); `safe_next()` blocks open redirects.
- **Student code (fallback):** typing the auto-generated `student_code` (`sha256(name)[:8]`).

Both set the `munus_student` signed cookie (30 days). All identity helpers live in
`services/student_auth.py` so the portal and Slack routers share them without importing each
other. Admin sessions use a separate `admin_session` cookie, same pattern as Tempus.

### Requirements & season total
Required hours come from the `level_requirements` table (admin-editable, seeded from
`DEFAULT_LEVEL_HOURS` = freshman 5 / 4423 10 / 4143 15). The season total is the sum of
**approved** `HourSubmission.hours` since the `season_start` cutoff
(`services/app_settings.py`; blank = count all).

### Submission approval
`services/submissions.py` owns Slack block building + DMs so both the portal and the Slack
router can trigger notifications without a circular import.

**Primary path — log hours in Slack (no site visit):** after a shift ends,
`job_post_shift_prompts` DMs the student interactive blocks (`post_shift_blocks`): **✅ Log
{duration} hrs** (one tap, defaults to the scheduled length) or **✏️ Change hours** (opens
`log_hours_modal`). `/slack/interact` handles `hours_quick` / `hours_adjust` (`views.open`) /
`view_submission` → `submit_shift_hours` creates a pending submission.

**Reviewer routing** (student never picks): `resolve_reviewer_id(shift)` =
`shift.reviewer_mentor_id` (per-shift override) → else `opportunity.reviewer_mentor_id`
(default approver, set in the admin opportunity editor) → else `None` (Admin → Submissions
queue). Then `notify_reviewer` DMs Approve / **✏️ Edit hours** / Reject → `set_status` →
`notify_student_of_review`. The **Edit hours** button opens `review_hours_modal` (the
approver-side counterpart to the student's `log_hours_modal`, guarded to mentors); saving it
updates the still-pending submission's hours/report and re-sends the review card. Admins can
do the same from `/admin/submissions`.

**Fallback:** the web `/submit` form (student picks a mentor) still exists for ad-hoc hours.

Slack modals/buttons require the app's **Interactivity Request URL** = `/slack/interact`
(public host); `views.open` needs a fresh `trigger_id`, so `hours_adjust` opens the modal
inline (not in a background task).

### New-opportunity announcements
When the **first shift** is added to an opportunity (`admin_shift_create`), munus posts an
announcement to `SLACK_ANNOUNCE_CHANNEL` (blank = off; the bot must be in that channel).
Opportunities are created empty, so the first-shift moment is when there's finally something
to sign up for. The message (`opportunities.announce_opportunity`) carries a **🙋 View & sign
up** button (`action_id="opp_dashboard"`, value = opportunity id).

A single channel message can't hold a per-person magic link (the link embeds one
`student_id`, so everyone would sign in as that student). The **button** solves this: on
click, `/slack/interact` reads the clicker's Slack id, looks up their `Student`, and replies
**ephemerally** with a magic link minted for *them*, deep-linked to `/opportunities/{id}` —
so each person gets their own one-tap sign-in. Unlinked users get an ephemeral "ask an admin"
note. Same identity trick as `/vhours`.

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
| Auto-reject unlogged shifts | every 6 h (records a rejected submission `AUTO_REJECT_DAYS` after a shift ends if the student never logged it; `0` = off) |
| Database backup | `BACKUP_DAY` at `BACKUP_TIME` (SQLite snapshot, rotates to `BACKUP_KEEP`) |

## Backups (`services/backup.py`)

SQLite only. Snapshots use `VACUUM INTO` (consistent, no downtime). Restores are staged
next to the DB and swapped in by `apply_pending_restore()` at startup — called from
`init_db()` **before** the engine opens a connection. Admin UI at `/admin/backup`
(download / stage-restore); a scheduled job writes rotating snapshots into `BACKUP_DIR`.
