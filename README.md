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
- **Student portal** — students sign in with a personal code, view opportunity details
  *before* signing up, claim/cancel shifts (capacity-enforced), and track their progress.
- **Hour submission with approval** — a student submits hours + a short report and picks a
  reviewing mentor; the mentor gets a Slack DM with **Approve / Reject** buttons; the
  student is notified of the outcome. Only approved hours count.
- **Season requirements by level** — Freshman 5 / 4423 Student 10 / 4143 Student 15 hours
  (admin-editable).
- **Admin UI** — dashboard of pending reviews, full CRUD for students, mentors,
  opportunities/shifts, and submissions (edit faulty entries), CSV import, audit log.
- **Slack** — `/vhours` for a student's season progress; automatic pre-shift reminders and
  post-shift "submit your hours" prompts.

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

### Run (production — systemd on Raspberry Pi)

```bash
sudo cp munus.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now munus
```

The unit expects user `pi`, working dir `/home/pi/munus`, a `.env` there, and a virtualenv
at `/home/pi/munus/venv/`. It listens on port **8001** so it can run alongside Tempus.

---

## Configuration Reference

| Env Var | Default | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | *(required for Slack)* | Bot OAuth token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | *(required for Slack)* | App signing secret |
| `ADMIN_PASSWORD` | `changeme` | Password for `/admin` — **change this** |
| `SESSION_SECRET` | `dev-secret...` | Secret signing admin/student cookies — **change this** |
| `DATABASE_URL` | `sqlite+aiosqlite:///./munus.db` | Async SQLAlchemy URL |
| `TIMEZONE` | `America/New_York` | IANA timezone for scheduling/display |
| `SEASON_START` | *(blank)* | Count approved hours from this ISO date (blank = all) |
| `BASE_URL` | `http://localhost:8001` | Public URL used in Slack links |
| `REMINDER_LEAD_HOURS` | `24` | Hours before a shift to DM signed-up students |
| `WEEKLY_DM_DAY` / `WEEKLY_DM_TIME` | `6` / `21:00` | Weekly season-progress DM (0=Mon…6=Sun) |

---

## Slack Setup

1. Create a **new** Slack app (separate from Tempus) at https://api.slack.com/apps
2. **OAuth & Permissions** → add bot scopes: `chat:write`, `im:write`, `commands`
3. **Slash Commands** → add `/vhours` → `https://<host>/slack/command`
4. **Interactivity & Shortcuts** → Request URL `https://<host>/slack/interact`
5. Install to the workspace; copy the Bot Token and Signing Secret into `.env`

Students and reviewing mentors need their Slack user IDs set in the admin UI to receive DMs.

---

## Admin UI

| Section | Description |
|---|---|
| **Dashboard** | Pending submissions with one-click approve/reject; quick counts |
| **Opportunities** | Create/edit opportunities (description, location, attire, contact) and manage their shifts (time + capacity) |
| **Submissions** | Filter by status; edit hours/status/reviewer for faulty entries |
| **Students** | CRUD + CSV import; set level, team, Slack UID; auto portal code |
| **Mentors** | CRUD; mentors with a Slack UID can review submissions |
| **Requirements** | Edit required season hours per level |
| **Import** | Bulk-load students/mentors from CSV |
| **Audit Log** | Append-only record of every mutation |
| **Settings** | Season start date |

---

## Database

SQLite by default (`munus.db`). Tables are created on first startup; no manual schema
steps. To use PostgreSQL, set `DATABASE_URL` to an async URL
(`postgresql+asyncpg://user:pass@host/db`).
