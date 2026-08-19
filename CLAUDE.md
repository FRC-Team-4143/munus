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

## Manual / visual verification (screenshots)

This sandboxed environment has no seeded dev database and blocks outbound access to
`cdn.jsdelivr.net` (Bootstrap/Bootstrap Icons) — check `curl -sS
$HTTPS_PROXY/__agentproxy/status` if requests through it fail; a `connect_rejected` for
that host is a policy denial, not a bug. Getting an actual screenshot of an admin page
(Playwright's Chromium is pre-installed at `/opt/pw-browsers/chromium`; `pip install
playwright` into the venv if the Python package isn't there yet) needs a few workarounds:

1. **Local Bootstrap assets, served via Playwright route interception** —
   `registry.npmjs.org` *is* reachable. Download once:
   ```bash
   mkdir -p /tmp/cdn_assets && cd /tmp/cdn_assets
   curl -sSL https://registry.npmjs.org/bootstrap/-/bootstrap-5.3.3.tgz | tar xz
   curl -sSL https://registry.npmjs.org/bootstrap-icons/-/bootstrap-icons-1.11.3.tgz | tar xz
   ```
   (both extract into `package/` and merge — `dist/css`, `dist/js`, and `font/` all end
   up under the same directory). In the Playwright script, `context.route(re.compile(r"cdn\.jsdelivr\.net"),
   handler)` and `route.fulfill(...)` the CSS/JS/`.woff`/`.woff2` requests from those
   local files. Skipping this makes every page render unstyled (no dark theme, no
   icons) even though the HTML/JS is completely correct — don't mistake that for a
   real bug.

2. **A seeded temp DB** — point `DATABASE_URL` at a scratch sqlite file
   (`sqlite+aiosqlite:////tmp/munus_demo.db`) and run a short script, with
   `PYTHONPATH=/home/user/munus` (a bare `python script.py` doesn't put the repo root
   on `sys.path`), that calls `Base.metadata.create_all` and inserts a few rows.

3. **A valid `mw_sso` cookie, minted directly** — no need to walk the real SSO flow:
   ```python
   from itsdangerous import URLSafeTimedSerializer
   signer = URLSafeTimedSerializer("<same secret as SSO_SECRET below>", salt="mw-sso")
   cookie = signer.dumps({"member_code": "test0001", "username": "test.admin",
       "name": "Test Admin", "role": "mentor", "team_number": 4143,
       "groups": ["munus-admin"], "slack_user_id": None})
   ```
   `services/sso.py` builds its `itsdangerous` signer **at import time** from
   `settings.sso_secret` — mutating `settings.sso_secret` after the app/server has
   already started has no effect. Set `SSO_SECRET` in the server process's own
   environment *before* it starts, not by patching the already-imported `settings`
   object in-process.

4. **Run and drive it**: `DATABASE_URL=... SSO_SECRET=... uvicorn app.main:app --port
   8001` in the background, then Playwright `add_cookies([{"name": "mw_sso", "value":
   cookie, "domain": "127.0.0.1", "path": "/"}])` before navigating. Talk to
   `127.0.0.1` directly, not `localhost`, and don't route local traffic through the
   session's `HTTPS_PROXY` — a plain-HTTP request through it 405s ("non-CONNECT
   request"); only the CDN-lookalike requests in step 1 need interception.

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
  or the API). `safe_next()` in `legion_auth.py` blocks open redirects. The challenge is
  started with an **absolute** `return_to` (`{BASE_URL}{next}`) — Legion's
  `/sso/complete` redirects to `return_to` as-is, and a bare relative path would resolve
  against *Legion's* own host on this cookie-less path rather than Munus's. Mirrors
  Tempus's `/enter`.
- **Portal ↔ admin cross-navigation:** trivial since both read the same live `mw_sso`
  claims — no bridging route or synced group data needed. `portal/base.html` shows an
  **Admin** link (navbar) **and** a prominent "Open admin area" card on the dashboard when
  `session_identity(request).groups` intersects `{munus-admin, munus-manager}`;
  `admin/base.html` shows a **My Dashboard** link when
  `session_identity(request).role == "student"`. Both link straight across (`/admin`,
  `/me`) since the shared cookie already grants access on the other side.
- **Dashboard route is `/me`:** the student dashboard is canonically **`/me`** (matching
  Tempus), with `/` a 307 redirect to it and logout at `/me/logout`. (`/my-hours` — the
  full submission history — is separate and unchanged.)

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
Munus posts an announcement to `SLACK_ANNOUNCE_CHANNEL` (blank = off; the bot must be
in that channel) at the moment there's finally something to act on: the **first shift**
added to a shift-based opportunity (`admin_shift_create`), or immediately on creation
for a **continuous** one (`admin_opportunities_create` — it has no shifts to wait for).
The message (`opportunities.opportunity_announcement_blocks`) carries a **🙋 View & sign
up** button — a plain Slack *link* button (a `url`, no `action_id`), straight to
`{BASE_URL}/opportunities/{id}`. The title renders in its own `header` block (Slack's
only way to get larger text — mrkdwn `section` text has no font-size control), which
means it's `plain_text` only and capped at 150 chars; `opportunity_announcement_blocks`
truncates with `…` if `opp.name` pushes it over. The required flag, description, and
info bullets (location/date/attire) live in a separate `section` block below it, each
its own paragraph (blank `\n\n` line) so they don't read as one dense block.

Being a link button, it never touches our server — Slack opens the URL directly, a
genuine one-tap click for anyone with a live Legion session. The tradeoff: a shared
channel message can't carry a personalized link per-clicker, so someone *without* a
live session just hits Munus's normal sign-in wall (types their Legion username)
instead of the one-tap Slack-push bootstrap `/enter` gives `/vhours`. Chosen
deliberately over the alternative (an interactive button + an ephemeral reply with a
personalized `/enter` link) to avoid the extra message just to open the page.

`announce_opportunity` records where it posted (`Opportunity.announcement_channel_id`/
`announcement_ts`). The blocks also carry a 📅 date line (`utils.format_date_range`
over `opp.shifts`) for shift-based opportunities. Saving the opportunity editor form,
or adding/editing/deleting any of its shifts, calls `opportunities.update_announcement`
afterward, which re-renders the same blocks and pushes them to that message via
`chat.update` — so a name/description/location/attire/required-status edit *and* a
shift's date span stay in sync with what's pinned in Slack instead of going stale. (The
FIRST shift on a shift-based opportunity is the exception — it triggers the initial
`announce_opportunity` post instead, per the trigger rule above.) No-op if the
opportunity was never announced (blank `SLACK_ANNOUNCE_CHANNEL` at the time, or no
shift/continuous trigger has fired yet). `opportunity_announcement_blocks` needs
`opp.shifts` eager-loaded (`selectinload`) before it's called — it's synchronous and
can't lazy-load across an async session.

### CSV exports
`/admin/report/export`, opened via the report page's **Export CSV** button (a modal for
picking a date range — blank means all-time — and an "include archived students"
checkbox), is a single combined shape: one row per hour submission, grouped by student,
with a `TOTAL` subtotal row after each student's submissions
(`reports.student_submission_export_rows`). Every submission status is listed, but each
`TOTAL` row counts only **approved** hours, matching every other total in Munus. It takes
`archived=1` to include students who have since left the roster — useful for a wide range
handed to an outside program (e.g. Silver Cords, which awards at 200 hours across a whole
high-school career, spanning several seasons). Note `HourSubmission` has **no
date-performed column**, so the range filter is on `submitted_at`, same as the detail page
and the season cutoff. The on-screen season-progress table (approved/projected/required/
remaining/%/pending/upcoming/missing-required-opportunities/met, pinned to `season_start`)
is unchanged and still active-students-only — those KPI columns are view-only now, not
exportable as CSV.

The per-student detail CSV is `/admin/report/archived/students/{id}/export`, parsing
`date_from`/`date_to` exactly like its HTML twin so the button is just the page URL +
`/export` + the same query string. It lists every status (the page's list does too; only
its *total* is approved-only). Linked from single-student surfaces only: the detail page
(all-time by default, following the page's own date filter) and the report modal's header
link, which instead defaults `date_from` to `season_start` (the season window) when a
cutoff is configured — a template-side default on that one link, not a change to the
export route itself. The student search page deliberately has no export.

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
| Auto-archive stale opportunities | every 6 h, plus once at startup (archives a shift-based opportunity `AUTO_ARCHIVE_DAYS` after its last shift ends; `0` = off; never touches continuous opportunities or logged hours — only `is_active`/`archived_at`) |
| Database backup | `BACKUP_DAY` at `BACKUP_TIME` (SQLite snapshot, rotates to `BACKUP_KEEP`) |
| Legion roster sync | hourly, on the hour (cheap incremental pull via `updated_since`) |

## Backups (`services/backup.py`)

SQLite only. Snapshots use `VACUUM INTO` (consistent, no downtime). Restores are staged
next to the DB and swapped in by `apply_pending_restore()` at startup — called from
`init_db()` **before** the engine opens a connection. Admin UI at `/admin/backup`
(download / stage-restore); a scheduled job writes rotating snapshots into `BACKUP_DIR`.
