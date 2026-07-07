# Munus

A web-based **volunteer-hour tracker** for FIRST Robotics Competition teams
**4143 (MARS/WARS)** and **4423 (MARS' Minions)**. Students browse volunteer
opportunities, sign up for dated shifts, and submit their hours for a mentor to approve.
Season requirements are set per student level.

Munus is a sibling to [Tempus](../tempus) (the in-shop attendance kiosk) and
[Legion](../legion) (the shared roster + SSO provider), and shares Tempus's look and
feel — but is a fully separate app with its own database and Slack app.

## Features

- **Opportunities & shifts** — admins post opportunities with rich detail (description,
  location, attire, contact); each has one or more dated shifts with a capacity.
- **Student portal** — one-tap sign-in to a dashboard, view opportunity details *before*
  signing up, claim/cancel shifts (capacity-enforced), and track their progress.
- **Passwordless login via Legion** — running `/vhours` returns an "Open my dashboard"
  link. If your browser is already signed in it opens instantly; otherwise Legion sends
  a Slack Approve/Deny push and you're in with one tap — no typing. Automated DMs carry
  the same link. See [Legion integration](#legion-integration) below.
- **Log hours in Slack** — after a shift ends the student gets a DM to **tap once** to log the
  scheduled hours (or open a Slack dialog to adjust if it ran long/short) — no site visit. It
  routes to the shift's approver automatically (per-shift override → opportunity default) for
  **Approve / Reject**; the student is notified of the outcome. Only approved hours count. A
  web `/submit` form remains for ad-hoc hours.
- **Season requirements by level** — Freshman 5 / 4423 Student 10 / 4143 Student 15 hours
  (admin-editable pool sizes; which pool a student is in is derived automatically from
  their Legion grade + team — see [Legion integration](#legion-integration)).
- **Admin UI** — dashboard of pending reviews, a read-only roster synced from Legion,
  full CRUD for opportunities/shifts and submissions (edit faulty entries), audit log.
- **Slack** — `/vhours` for a student's season progress + upcoming shifts + a one-tap
  dashboard link; automatic pre-shift reminders and post-shift "submit your hours" prompts.

---

## Getting Started

### Prerequisites
- Python 3.11+
- A Slack app with a bot token and signing secret (see [Slack Setup](#slack-setup))
- A running [Legion](../legion) instance (roster + SSO provider)

### Installation

```bash
cd munus
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # or requirements-dev.txt to run tests
cp .env.example .env              # then fill in your values
```

### Run (development)

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

- Student portal: `http://localhost:8001/`
- Admin UI: `http://localhost:8001/admin` — signs in through Legion; no local password

On first start the database is created and the per-level requirements are seeded. Run
**Sync now** on `/admin/roster` (or wait for the hourly job) to pull the roster from
Legion before students can sign in.

### Run (production — Docker on DigitalOcean)

The app runs as a Docker container alongside Tempus behind an nginx reverse proxy.
See the [apps-infra](https://github.com/FRC-Team-4143/apps-infra) repo for the full
deployment setup and first-time server instructions.

Accessible at **http://volunteer.marswars.org**.

Pushing to `main` automatically deploys via GitHub Actions (tests must pass first).

---

## Configuration Reference

| Env Var | Default | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | *(required for Slack)* | Bot OAuth token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | *(required for Slack)* | App signing secret |
| `SLACK_ANNOUNCE_CHANNEL` | *(blank)* | Channel ID new opportunities are announced in (blank = off) |
| `SSO_SECRET` | *(required for admin/portal)* | Shared secret for verifying Legion's `mw_sso` cookie — **must equal Legion's `SSO_SECRET`** |
| `SSO_SESSION_TTL` | `43200` | Max age (seconds) of the SSO cookie; match Legion |
| `SSO_COOKIE_DOMAIN` | *(none)* | Cookie domain (e.g. `.marswars.org`) so one login spans subdomains |
| `LEGION_BASE_URL` | *(required)* | Base URL of the Legion app (SSO + roster API) |
| `LEGION_API_KEY` | *(required)* | Shared key sent as `X-API-Key` to Legion's roster API and one-tap SSO challenge endpoint — **must equal Legion's `LEGION_API_KEY`** |
| `DATABASE_URL` | `sqlite+aiosqlite:///./munus.db` | Async SQLAlchemy URL |
| `TIMEZONE` | `America/New_York` | IANA timezone for scheduling/display |
| `SEASON_START` | *(blank)* | Count approved hours from this ISO date (blank = all) |
| `BASE_URL` | `http://localhost:8001` | Public URL used in Slack links |
| `REMINDER_LEAD_HOURS` | `24` | Hours before a shift to DM signed-up students |
| `AUTO_REJECT_DAYS` | `7` | Close out a never-logged shift this many days after it ends (0 = off) |
| `BACKUP_DIR` / `BACKUP_KEEP` | `backups` / `14` | Snapshot directory and how many to retain |
| `BACKUP_DAY` / `BACKUP_TIME` | `sun` / `23:30` | When the automatic SQLite snapshot runs |
| `UPDATES_ENABLED` | `true` | Master switch for automated Slack messages and scheduled jobs |

> Most non-secret settings — announce channel, timezone, reminder/auto-reject timing, backup schedule, and the updates toggle — can be edited at runtime from **Admin → Settings**, which writes changes back to `.env` and applies them immediately. API keys/secrets (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SSO_SECRET`, `LEGION_API_KEY`) and deploy-time values (`DATABASE_URL`, `BASE_URL`) are intentionally **not** editable from the UI.

---

## Slack Setup

1. Create a Slack app at https://api.slack.com/apps — **in production this is actually
   the same app shared with Tempus and Legion** (see the note below), but the steps to
   create one from scratch are the same either way.
2. **OAuth & Permissions** → add bot scopes: `chat:write`, `im:write`, `commands`
3. **Slash Commands** → add `/vhours` → `https://<host>/slack/command`
4. **Interactivity & Shortcuts** → Request URL `https://<host>/slack/interact` — see
   the note below if this app is shared with the sibling apps.
5. Install to the workspace; copy the Bot Token and Signing Secret into `.env`

Students and reviewing mentors need their Slack user IDs set **in Legion** to receive DMs
— see [Legion integration](#legion-integration).

> **Sharing one Slack app across Tempus/Munus/Legion:** sending messages and slash
> commands work fine shared (any number of services can use the same bot token, and
> each slash command has its own independently configurable Request URL) — but Slack
> allows only **one** Interactivity Request URL per app, and each of these three
> services wants its own button clicks. Rather than each getting a separate app, the
> shared app's Interactivity Request URL points at Legion's `/slack/dispatch` (a
> stateless relay with no business logic — see `legion/README.md` "Single sign-on"),
> which forwards each payload to whichever app's own `/slack/interact` actually owns
> it based on `action_id`/`callback_id`. Don't point this app's Interactivity URL at
> Munus's own `/slack/interact` directly if it's the shared app — that would starve
> Tempus's and Legion's interactive buttons of real traffic.

---

## Admin UI

Navigate to `/admin`. Access is gated by **Legion SSO** — you're redirected to Legion to
sign in (a Slack Approve/Deny push, no password), and you must hold the **`munus-admin`**
group in Legion for full access, or **`munus-manager`** for a limited login scoped to
creating/managing opportunities and shifts. There is no local admin password; grant the
first admin `munus-admin` in Legion's `/admin/groups`. The signed-in identity (Legion
username) is recorded as the audit actor.

| Section | Description |
|---|---|
| **Dashboard** | Pending submissions with one-click approve/reject; quick counts |
| **Roster** | Read-only view of the members synced from Legion (students & mentors, level, team, grade, Slack link status), and a **Sync now** button. Add/edit/archive members in Legion, not here |
| **Opportunities** | Create/edit opportunities (description, location, attire, contact) and manage their shifts (time + capacity) — a `munus-manager` login can reach this section only |
| **Submissions** | Filter by status; edit hours/status/reviewer for faulty entries |
| **Report** | Roster progress table — approved / **projected** / required hours per student, level filter, CSV export |
| **Requirements** | Edit required season hours per level |
| **Audit Log** | Append-only record of every mutation |
| **Backup** | Download a live SQLite snapshot or stage a restore; automatic rotating snapshots |
| **Settings** | Live-edit non-secret config — season start, timezone, announce channel, reminder & auto-reject timing, backup schedule, the updates toggle, and per-level season requirements. Changes write back to `.env` and apply immediately |

### Legion integration

Munus is a **Legion consumer**: Legion owns the roster (members, teams, user groups) and
Munus mirrors it read-only. Unlike Tempus, Munus's *student portal* — not just `/admin`
— runs on Legion SSO too; there is no Munus-specific student cookie or password anywhere
in the app.

- **Auth** — both `/admin` and the student portal verify Legion's `mw_sso` cookie locally
  with the shared `SSO_SECRET` (no callback to Legion) — `/admin` additionally checks for
  the `munus-admin`/`munus-manager` group, the portal for an active `role=student`
  member. Add Munus's host to Legion's `SSO_ALLOWED_RETURN_HOSTS`.
- **Roster mirror** — an hourly job (and the **Sync now** button) pulls
  `GET /api/members?updated_since=…` from Legion, keyed on Legion's stable `member_code`,
  and upserts the local `Student`/`Mentor` mirror. Legacy rows are back-linked by
  `slack_user_id` then name on first sync.
- **Requirement pools are derived, not admin-set** — each student's `level` (which of the
  three requirement pools they're in) is computed automatically on every sync from their
  Legion `grade` and `team_number`:
  ```
  junior_high / freshman grade         -> Freshman
  sophomore grade, team 4423           -> 4423 Student
  anything else (any other grade/team) -> 4143 Student
  alumni or no grade (or a mentor)     -> no level (excluded from level reporting)
  ```
  The *pool sizes* (5 / 10 / 15 hours) stay admin-editable on **Admin → Requirements**;
  only which pool a student falls into is Legion-derived.
- **One-tap sign-in** — `/vhours` and the opportunity-announcement button link to
  `/enter?member=<code>`, which skips Legion entirely if the browser already holds a
  live `mw_sso` cookie, or otherwise calls Legion's `POST /sso/challenge` (a small
  server-to-server addition to Legion for this rework — see `legion/README.md`) to start
  a Slack Approve/Deny push for that specific member without making them type a Legion
  username, then sends the browser to Legion's `/sso/pending/{nonce}` "check Slack" page.
- **Portal ↔ admin cross-navigation** — since both surfaces read the same `mw_sso`
  cookie, a student who also holds `munus-admin`/`munus-manager` sees an **Admin** link
  in the portal nav, and an admin/manager whose Legion role is `student` sees a
  **My Dashboard** link in the admin sidebar. Both are plain links (no separate sign-in
  step) — group/role checks are read straight from the live SSO claims.

---

## Kiosk-free by design

Unlike Tempus, Munus has no kiosk device or badge scanning — sign-in is entirely
Slack/browser-based (see Legion integration above).

## Slack Workflow

### Student sign-up / logging hours

1. Student runs `/vhours` in Slack → gets a season-progress summary and a one-tap
   "Open my dashboard" link
2. On the dashboard, they browse **Opportunities**, view details, and sign up for a shift
3. After the shift ends, they get a DM to **tap once** to log the scheduled hours (or
   adjust if it ran long/short)
4. The submission routes to the shift's reviewer (per-shift override → opportunity
   default) for **Approve / Reject**; the student is notified of the outcome

### New-opportunity announcements

When the first shift is added to an opportunity, Munus posts to `SLACK_ANNOUNCE_CHANNEL`
with a **🙋 View & sign up** button. Each click ephemerally replies with *that clicker's*
own one-tap sign-in link, deep-linked to the opportunity.

---

## Database

SQLite is used by default (`munus.db` in the working directory). No manual schema creation is needed — tables are created on first startup.

To use PostgreSQL, set `DATABASE_URL` to an async-compatible URL:

```dotenv
DATABASE_URL=postgresql+asyncpg://user:password@host/dbname
```

---

## Project Structure

```
app/
├── main.py            # FastAPI app setup, startup/shutdown hooks
├── config.py          # Pydantic-settings configuration
├── database.py        # Async SQLAlchemy engine, session factory, init_db
├── models.py          # ORM models (Student, Mentor, Opportunity, Shift, …)
├── schemas.py         # Pydantic request/response schemas
├── utils.py           # Timezone helpers + shift-range formatting
├── routers/
│   ├── admin.py        # /admin — Legion-SSO-gated management UI (munus-admin/manager groups)
│   ├── portal.py        # / — student-facing portal (Legion SSO too — see legion_sync.py)
│   └── slack.py         # /slack — /vhours slash command + interactive Approve/Reject
├── services/
│   ├── opportunities.py # Shift capacity checks, signup/cancel logic, new-opportunity announce
│   ├── submissions.py   # Create submission -> DM reviewer; approve/reject -> notify student
│   ├── requirements.py  # Season required hours by level; derive_level(grade, team_number)
│   ├── reports.py       # Batched roster progress report (approved/projected/required)
│   ├── sso.py            # Verifies Legion's mw_sso cookie (verify-only consumer)
│   ├── legion_sync.py    # Pulls the roster from Legion's read-only API into the local mirror
│   ├── legion_auth.py    # One-tap sign-in: starts a Legion SSO challenge for a known member
│   ├── backup.py         # SQLite snapshot backup + staged restore (VACUUM INTO)
│   ├── scheduler.py      # APScheduler: reminders, post-shift prompts, backup, hourly Legion sync
│   ├── slack_client.py   # Slack AsyncWebClient wrapper + send_dm
│   └── audit.py          # Append-only mutation log
└── templates/          # Jinja2 HTML templates
```

## Legion rework — done

The migration to Legion (SSO auth for both `/admin` and the student portal, a read-only
roster synced from Legion's API, and grade+team-derived requirement pools) is complete.
See **Admin UI → Legion integration** above. The operational steps: add Munus's host to
Legion's `SSO_ALLOWED_RETURN_HOSTS`, and create the `munus-admin`/`munus-manager` groups
in Legion (already seeded by default — see `legion/app/models.py` `DEFAULT_GROUPS`).
