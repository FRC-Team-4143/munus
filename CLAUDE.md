# Munus — Codebase Guide

Volunteer-hour tracker for FRC teams 4143 (MARS/WARS) and 4423 (MARS' Minions).
Students browse volunteer **opportunities**, sign up for dated **shifts**, and submit
**hours** that a mentor approves. Season requirements are driven by student **level**.
FastAPI + SQLAlchemy (async) + Jinja2 + SQLite. Slack integration for `/vhours` and
interactive Approve/Reject of submissions.

Sibling app to **Tempus** (attendance/hours kiosk) and **Legion** (shared roster + SSO
provider), and intentionally mirrors Tempus's stack and dark styling — but is a fully
separate app with its own DB, Slack app, and Docker service (port 8001). Nothing is
imported across the projects.

## Running

```bash
source venv/bin/activate
uvicorn app.main:app --reload --port 8001
```

Requires a `.env` file (see `.env.example`). Key vars: `SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET`, `BASE_URL`, and the Legion integration — `SSO_SECRET` (must
equal Legion's), `LEGION_BASE_URL`, `LEGION_API_KEY`. There is **no** admin password and
no student token; both `/admin` and the student portal are gated by Legion SSO.
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
    portal.py        # Student-facing: Legion-SSO identity, browse, sign up, submit hours
    admin.py         # Legion-SSO-gated management UI (munus-admin/munus-manager groups)
    slack.py         # /vhours slash command + /interact (approve/reject)
  services/
    opportunities.py # Shift capacity checks, signup/cancel logic, new-opportunity announce
    submissions.py   # Create submission -> DM reviewer; approve/reject -> notify student
    requirements.py  # Season required hours by level; derive_level(grade, team_number)
    reports.py       # Batched roster progress report (approved/projected/required)
    sso.py           # Verifies Legion's mw_sso cookie (verify-only; shared by admin + portal)
    legion_sync.py   # Pulls the roster from Legion's read-only API into the local mirror
    legion_auth.py   # One-tap sign-in: starts a Legion SSO challenge for a known member
    backup.py        # SQLite snapshot backup + staged restore (VACUUM INTO)
    scheduler.py     # APScheduler: pre-shift reminders, post-shift prompts, backup, Legion sync
    slack_client.py  # AsyncWebClient wrapper + send_dm
    audit.py         # Append-only mutation log
    app_settings.py  # Persisted runtime settings (season_start, legion_last_synced)
```

### Legion integration (source of truth for the roster)
Legion owns members, teams, and user groups; Munus is a **read-only consumer** — data
flows Legion → Munus only, never back. Unlike Tempus, Munus's *student portal* runs on
Legion SSO too, not just `/admin` — there is exactly one identity mechanism (`mw_sso`)
for the whole app; no Munus-specific cookie or password exists anywhere.
- **Auth (`services/sso.py`):** both `/admin` and the portal verify Legion's `mw_sso`
  cookie locally with the shared `SSO_SECRET` (no callback). `/admin` additionally
  requires the `munus-admin` (full) or `munus-manager` (opportunities/shifts only) group
  via `_require_auth` in `routers/admin.py`; the portal requires an active `role ==
  "student"` member (`_current_student` in `routers/portal.py`). On a miss, redirect to
  `{LEGION_BASE_URL}/sso/authorize?app=munus`. The audit actor is the SSO username.
- **Roster mirror (`services/legion_sync.py`):** the local `Student`/`Mentor` tables are
  a synced mirror keyed on Legion's stable `member_code`. Sync pulls
  `/api/members?updated_since=…` hourly and on the **Sync now** button; legacy rows are
  back-linked by `slack_user_id` then name. `Signup`/`HourSubmission` FKs stay local.
  Munus has no `Team`/`Subteam` mirror tables (unlike Tempus) — it only ever needed the
  raw `team_number` int. **Never add roster CRUD or write-back to Legion.**
- **Requirement pools are derived, not admin-set:** `Student.level` (nullable — alumni/
  no-grade students have none) is computed on every sync by
  `services.requirements.derive_level(grade, team_number)`: junior_high/freshman grade →
  Freshman (any team); sophomore + team 4423 → 4423 Student; everything else → 4143
  Student. The `level_requirements` table (pool *sizes*, still admin-editable on
  **Admin → Requirements**) is unaffected — only which pool a student falls into changed.
- **One-tap sign-in (`services/legion_auth.py`, `GET /enter` in `routers/portal.py`):**
  `/vhours` and the announcement button link to `/enter?member=<code>&next=<path>`. If
  the browser already holds a live `mw_sso` cookie, `/enter` redirects straight to
  `next` — **no** Legion round trip, which is what stops a repeated `/vhours` call from
  spamming a fresh Slack push every time. Otherwise it calls Legion's
  `POST /sso/challenge` (a small server-to-server addition to Legion made for this
  rework — `X-API-Key`-authenticated, same trust boundary as the roster API; see
  `legion/app/routers/sso.py`) to start a Slack Approve/Deny push for that *specific*
  member without making them type a Legion username, then redirects to Legion's
  `GET /sso/pending/{nonce}` "check Slack" page (reuses the existing `sso/pending.html`
  polling flow — it doesn't care whether the `AuthRequest` came from the username form
  or the API). `safe_next()` in `legion_auth.py` blocks open redirects.
- **Portal ↔ admin cross-navigation:** trivial since both read the same live `mw_sso`
  claims — no bridging route or synced group data needed. `portal/base.html` shows an
  **Admin** link when `session_identity(request).groups` intersects
  `{munus-admin, munus-manager}`; `admin/base.html` shows a **My Dashboard** link when
  `session_identity(request).role == "student"`. Both link straight across (`/admin`,
  `/`) since the shared cookie already grants access on the other side.

## Key Conventions

### Datetimes
All datetimes in the database are **naive UTC** (`app/utils.py`):
- Display: `utc_to_local(dt)` / `format_shift_range(start, end)`
- DB queries / form parsing: `local_to_utc(dt)`
- `now_utc()` for "now" (matches stored values)

### Student identity (portal)
No passwords, no Munus-specific cookie — identity is the shared Legion `mw_sso` cookie
(see "Legion integration" above). `_current_student` (`routers/portal.py`) resolves the
current `Student` from `sso_identity(request)["member_code"]`. Getting a fresh browser
onto that cookie without making the student type a Legion username is the job of
`GET /enter` + `services/legion_auth.py` — see "Legion integration" for the full flow.

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

A single channel message can't hold a per-person sign-in link (a static link would sign
everyone in as whoever it was built for). The **button** solves this: on click,
`/slack/interact` reads the clicker's Slack id, looks up their `Student`, and replies
**ephemerally** with an `/enter?member=<their code>&next=/opportunities/{id}` link — so
each person gets their own one-tap sign-in, deep-linked to the opportunity. Unlinked
users get an ephemeral "ask an admin" note. Same mechanism as `/vhours`.

### Database migrations
No Alembic. Add a `def _migration(conn)` guarded by `inspect(conn)` in `database.py` and
call it from `init_db()`, mirroring Tempus. No production data predates the Legion
rework, so its migration doesn't bother preserving old rows: `_migration_drop_students_
if_legacy_schema` just drops `students` if it's still on the pre-rework schema (NOT NULL
`level`) and lets `create_all()` rebuild it fresh — don't take this as the general
pattern for a real data-preserving migration (see Tempus's/Legion's `_migration_*`
functions for that; they rename-and-copy instead of dropping).

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
| Legion roster sync | hourly, on the hour (cheap incremental pull via `updated_since`) |

## Backups (`services/backup.py`)

SQLite only. Snapshots use `VACUUM INTO` (consistent, no downtime). Restores are staged
next to the DB and swapped in by `apply_pending_restore()` at startup — called from
`init_db()` **before** the engine opens a connection. Admin UI at `/admin/backup`
(download / stage-restore); a scheduled job writes rotating snapshots into `BACKUP_DIR`.
