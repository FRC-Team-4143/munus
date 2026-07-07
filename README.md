# Munus

A web-based **volunteer-hour tracker** for FIRST Robotics Competition teams
**4143 (MARS/WARS)** and **4423 (MARS' Minions)**. Students browse volunteer
opportunities, sign up for dated shifts, and submit their hours for a mentor to approve.
Season requirements are set per student level.

Munus is a sibling to [Tempus](../tempus) (the in-shop attendance kiosk) and shares its
look and feel, but is a fully separate app with its own database, Slack app, and service.

## Features

- **Opportunities & shifts** — admins post opportunities with rich detail (description,
  location, attire, contact); each has one or more dated shifts with a capacity.
- **Student portal** — one-tap Slack sign-in (or a personal code) to a dashboard, view
  opportunity details *before* signing up, claim/cancel shifts (capacity-enforced), and
  track their progress.
- **Passwordless login** — running `/vhours` returns an "Open my dashboard" link that signs
  the student in automatically (a signed, 14-day token). Automated DMs carry the same link.
- **Log hours in Slack** — after a shift ends the student gets a DM to **tap once** to log the
  scheduled hours (or open a Slack dialog to adjust if it ran long/short) — no site visit. It
  routes to the shift's approver automatically (per-shift override → opportunity default) for
  **Approve / Reject**; the student is notified of the outcome. Only approved hours count. A
  web `/submit` form remains for ad-hoc hours.
- **Season requirements by level** — Freshman 5 / 4423 Student 10 / 4143 Student 15 hours
  (admin-editable).
- **Admin UI** — dashboard of pending reviews, full CRUD for students, mentors,
  opportunities/shifts, and submissions (edit faulty entries), CSV import, audit log.
- **Slack** — `/vhours` for a student's season progress + upcoming shifts + a one-tap
  dashboard link; automatic pre-shift reminders and post-shift "submit your hours" prompts.

---

## Getting Started

### Prerequisites
- Python 3.11+
- A Slack app with a bot token and signing secret (separate from Tempus's)

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
- Admin UI: `http://localhost:8001/admin` (log in with `ADMIN_PASSWORD`)

On first start the database is created and the per-level requirements are seeded.

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
| `ADMIN_PASSWORD` | `changeme` | Password for `/admin` — **change this** |
| `MANAGER_PASSWORD` | *(blank)* | Optional limited login that can only manage opportunities (blank = disabled) |
| `SESSION_SECRET` | `dev-secret...` | Secret signing admin/student cookies — **change this** |
| `DATABASE_URL` | `sqlite+aiosqlite:///./munus.db` | Async SQLAlchemy URL |
| `TIMEZONE` | `America/New_York` | IANA timezone for scheduling/display |
| `SEASON_START` | *(blank)* | Count approved hours from this ISO date (blank = all) |
| `BASE_URL` | `http://localhost:8001` | Public URL used in Slack links |
| `REMINDER_LEAD_HOURS` | `24` | Hours before a shift to DM signed-up students |
| `AUTO_REJECT_DAYS` | `7` | Close out a never-logged shift this many days after it ends (0 = off) |
| `BACKUP_DIR` / `BACKUP_KEEP` | `backups` / `14` | Snapshot directory and how many to retain |
| `BACKUP_DAY` / `BACKUP_TIME` | `sun` / `23:30` | When the automatic SQLite snapshot runs |
| `UPDATES_ENABLED` | `true` | Master switch for automated Slack messages and scheduled jobs |

> Most non-secret settings — announce channel, timezone, reminder/auto-reject timing, backup schedule, and the updates toggle — can be edited at runtime from **Admin → Settings**, which writes changes back to `.env` and applies them immediately. API keys/secrets (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `ADMIN_PASSWORD`, `MANAGER_PASSWORD`, `SESSION_SECRET`) and deploy-time values (`DATABASE_URL`, `BASE_URL`) are intentionally **not** editable from the UI.

---

## Slack Setup

1. Create a Slack app at https://api.slack.com/apps — **in production this is actually
   the same app shared with Tempus and Legion** (see the note below), despite the
   "separate from Tempus" step this used to say.
2. **OAuth & Permissions** → add bot scopes: `chat:write`, `im:write`, `commands`
3. **Slash Commands** → add `/vhours` → `https://<host>/slack/command`
4. **Interactivity & Shortcuts** → Request URL `https://<host>/slack/interact` — see
   the note below if this app is shared with the sibling apps.
5. Install to the workspace; copy the Bot Token and Signing Secret into `.env`

Students and reviewing mentors need their Slack user IDs set in the admin UI to receive DMs.

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

| Section | Description |
|---|---|
| **Dashboard** | Pending submissions with one-click approve/reject; quick counts |
| **Opportunities** | Create/edit opportunities (description, location, attire, contact) and manage their shifts (time + capacity) |
| **Submissions** | Filter by status; edit hours/status/reviewer for faulty entries |
| **Report** | Roster progress table — approved / **projected** / required hours per student, level filter, CSV export |
| **Students** | CRUD + CSV import; set level, team, Slack UID; auto portal code |
| **Mentors** | CRUD; mentors with a Slack UID can review submissions |
| **Requirements** | Edit required season hours per level |
| **Import** | Bulk-load students/mentors from CSV |
| **Audit Log** | Append-only record of every mutation |
| **Backup** | Download a live SQLite snapshot or stage a restore; automatic rotating snapshots |
| **Settings** | Live-edit non-secret config — season start, timezone, announce channel, reminder & auto-reject timing, backup schedule, the updates toggle, and per-level season requirements. Changes write back to `.env` and apply immediately |

---

## Database

SQLite by default (`munus.db`). Tables are created on first startup; no manual schema
steps. To use PostgreSQL, set `DATABASE_URL` to an async URL
(`postgresql+asyncpg://user:pass@host/db`).