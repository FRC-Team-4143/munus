"""
Admin routes — Legion-SSO-gated management UI.
"""
import csv
import io
import logging
import os
import re
import tempfile
from datetime import date, datetime, time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    AuditLog, HourSubmission, LevelRequirement, Mentor, Opportunity, Shift,
    Signup, SignupStatus, Student, StudentLevel, SubmissionStatus, level_label,
)
from app.services import audit, submissions as submission_service
from app.services.app_settings import get_season_start, set_season_start
from app.services.opportunities import active_signup_count, announce_opportunity
from app.services.reports import student_progress_report, student_vhours_message
from app.services.requirements import level_requirements_map, resolve_required_hours, season_total_hours
from app.services.slack_client import send_dm
from app.services.sso import logout_url, make_authorize_url, sso_identity
from app.utils import (
    format_date_range, format_shift_range, local_to_utc, now_utc, shift_length_hours,
    today_local, utc_to_local,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%m/%d %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)
templates.env.filters["shiftrange"] = lambda s, e=None: format_shift_range(s, e)
templates.env.filters["daterange"] = format_date_range
templates.env.filters["levellabel"] = level_label


def _opt_id(raw: Optional[str]) -> Optional[int]:
    """Parse an optional integer form field (e.g. a mentor dropdown), '' -> None."""
    return int(raw) if raw and str(raw).strip() else None


async def _active_mentors(db: AsyncSession):
    return (await db.execute(select(Mentor).where(Mentor.is_active.is_(True)).order_by(Mentor.name))).scalars().all()


# ── Auth helpers ───────────────────────────────────────────────────────────────
#
# /admin is gated by Legion SSO: the shared `mw_sso` cookie must carry the `munus-admin`
# or `munus-manager` group. There is no local password — Legion mints the cookie, Munus
# only verifies it (services/sso.py). The first admin is granted `munus-admin` in
# Legion's /admin/groups.

_ADMIN_GROUP = "munus-admin"
_MANAGER_GROUP = "munus-manager"


def _manager_allowed(path: str) -> bool:
    """The only routes a 'manager' may reach: creating/managing opportunities and shifts."""
    p = path.rstrip("/")
    return (
        p == "/admin/opportunities"
        or p.startswith("/admin/opportunities/")
        or p.startswith("/admin/shifts/")
    )


_SECTION_LABELS = [
    ("/admin/opportunities", "Opportunities"),
    ("/admin/shifts", "Shifts"),
    ("/admin/roster", "Roster"),
    ("/admin/submissions", "Submissions"),
    ("/admin/report", "Report"),
    ("/admin/audit", "Audit Log"),
    ("/admin/backup", "Backup"),
    ("/admin/settings", "Settings"),
    ("/admin", "Dashboard"),
]


def _section_label(path: str) -> str:
    """A human label for the section a denied request was aimed at, for the
    forbidden page's message. Order matters — most-specific prefix first, since
    "/admin" is itself a prefix of every other admin path."""
    for prefix, label in _SECTION_LABELS:
        if path.startswith(prefix):
            return label
    return "this page"


def _require_auth(request: Request):
    """Gate every admin route via Legion SSO. `munus-admin` passes everywhere;
    `munus-manager` only on opportunity/shift paths — anything else renders the same
    shell-wrapped "No Access" page as a fully unauthorized visitor (rather than
    silently redirecting away, which is more disorienting); no/invalid cookie ->
    Legion sign-in."""
    identity = sso_identity(request)
    if identity is None:
        return RedirectResponse(make_authorize_url(request), status_code=303)
    groups = set(identity.get("groups") or [])
    if _ADMIN_GROUP in groups:
        return None
    if _MANAGER_GROUP in groups and _manager_allowed(request.url.path):
        return None
    return templates.TemplateResponse(
        "admin/forbidden.html",
        {
            "request": request,
            "name": identity.get("name", ""),
            "section": _section_label(request.url.path),
        },
        status_code=403,
    )


# Expose to templates: `session_identity` is the raw SSO claims, used for the portal
# <-> admin cross-navigation links (role == "student") and the Admin sidebar link.
templates.env.globals["session_identity"] = sso_identity


# ── Logout ─────────────────────────────────────────────────────────────────────

@router.get("/logout")
async def admin_logout(request: Request):
    # Single logout: bounce to Legion's /sso/logout, which clears the shared `mw_sso`
    # cookie for every sibling app — including the student portal.
    return RedirectResponse(logout_url(request, return_to="/admin"), status_code=303)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    pending = (
        await db.execute(
            select(HourSubmission)
            .options(
                selectinload(HourSubmission.student),
                selectinload(HourSubmission.opportunity),
                selectinload(HourSubmission.reviewer),
            )
            .where(HourSubmission.status == SubmissionStatus.pending)
            .order_by(HourSubmission.submitted_at)
        )
    ).scalars().all()

    active_students = await db.scalar(
        select(func.count()).select_from(Student).where(Student.is_active.is_(True))
    ) or 0
    active_opps = await db.scalar(
        select(func.count()).select_from(Opportunity).where(Opportunity.is_active.is_(True))
    ) or 0

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "pending": pending,
            "active_students": active_students,
            "active_opps": active_opps,
        },
    )


# ── Roster (read-only, synced from Legion) ──────────────────────────────────────
#
# Students/Mentors are a read-only mirror synced from Legion (services/legion_sync.py).
# Add/edit/archive members in Legion's /admin, not here — this is just a view plus a
# manual "Sync now" trigger for the hourly job (services/scheduler.py).

@router.get("/roster", response_class=HTMLResponse)
async def admin_roster(
    request: Request, show_archived: int = 0, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    student_q = select(Student).order_by(Student.name)
    mentor_q = select(Mentor).order_by(Mentor.name)
    if not show_archived:
        student_q = student_q.where(Student.is_active.is_(True))
        mentor_q = mentor_q.where(Mentor.is_active.is_(True))

    from app.services.app_settings import LEGION_LAST_SYNCED_KEY, get_setting
    last_synced = await get_setting(db, LEGION_LAST_SYNCED_KEY)

    return templates.TemplateResponse(
        "admin/roster.html",
        {
            "request": request,
            "students": (await db.execute(student_q)).scalars().all(),
            "mentors": (await db.execute(mentor_q)).scalars().all(),
            "show_archived": bool(show_archived),
            "last_synced": last_synced,
            "legion_base_url": settings.legion_base_url,
            "synced": request.query_params.get("synced"),
            "sync_error": request.query_params.get("sync_error"),
        },
    )


@router.post("/roster/sync")
async def admin_roster_sync(request: Request, db: AsyncSession = Depends(get_db)):
    """Manually trigger a roster pull from Legion."""
    if redirect := _require_auth(request):
        return redirect
    from urllib.parse import quote

    from app.services.legion_sync import LegionSyncError, sync_roster
    try:
        summary = await sync_roster(db)
    except LegionSyncError as e:
        await audit.record(db, request, "roster.sync_failed", f"Legion sync failed: {e}")
        await db.commit()
        return RedirectResponse(f"/admin/roster?sync_error={quote(str(e))}", status_code=303)
    await audit.record(db, request, "roster.sync", f"Synced roster from Legion ({summary})")
    await db.commit()
    return RedirectResponse(f"/admin/roster?synced={quote(summary)}", status_code=303)


# ── Opportunities & shifts ─────────────────────────────────────────────────────

@router.get("/opportunities", response_class=HTMLResponse)
async def admin_opportunities_list(
    request: Request, show_archived: int = 0, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect
    q = select(Opportunity).options(selectinload(Opportunity.shifts)).order_by(Opportunity.name)
    if not show_archived:
        q = q.where(Opportunity.is_active.is_(True))
    opps = (await db.execute(q)).scalars().all()
    return templates.TemplateResponse(
        "admin/opportunities.html",
        {"request": request, "opps": opps, "show_archived": bool(show_archived),
         "mentors": await _active_mentors(db)},
    )


@router.post("/opportunities")
async def admin_opportunities_create(
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    attire: Optional[str] = Form(None),
    contact: Optional[str] = Form(None),
    reviewer_mentor_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    opp = Opportunity(
        name=name.strip(),
        description=description.strip() if description else None,
        location=location.strip() if location else None,
        attire=attire.strip() if attire else None,
        contact=contact.strip() if contact else None,
        reviewer_mentor_id=_opt_id(reviewer_mentor_id),
    )
    db.add(opp)
    await audit.record(db, request, "opportunity.create", f"Created opportunity {opp.name}", entity_type="opportunity")
    await db.commit()
    return RedirectResponse(f"/admin/opportunities/{opp.id}/edit", status_code=303)


@router.get("/opportunities/{opp_id}/edit", response_class=HTMLResponse)
async def admin_opportunities_edit_get(opp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    opp = (
        await db.execute(
            select(Opportunity).options(selectinload(Opportunity.shifts)).where(Opportunity.id == opp_id)
        )
    ).scalars().first()
    if not opp:
        return RedirectResponse("/admin/opportunities", status_code=303)
    shifts = sorted(opp.shifts, key=lambda s: s.start_time)
    counts = {s.id: await active_signup_count(db, s.id) for s in shifts}
    all_mentors = (await db.execute(select(Mentor).order_by(Mentor.name))).scalars().all()
    return templates.TemplateResponse(
        "admin/opportunity_edit.html",
        {
            "request": request, "opp": opp, "shifts": shifts, "counts": counts,
            "mentors": [m for m in all_mentors if m.is_active],
            "mentor_names": {m.id: m.name for m in all_mentors},
        },
    )


@router.post("/opportunities/{opp_id}/edit")
async def admin_opportunities_edit_post(
    opp_id: int,
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    attire: Optional[str] = Form(None),
    contact: Optional[str] = Form(None),
    reviewer_mentor_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    opp = (await db.execute(select(Opportunity).where(Opportunity.id == opp_id))).scalars().first()
    if opp:
        opp.name = name.strip()
        opp.description = description.strip() if description else None
        opp.location = location.strip() if location else None
        opp.attire = attire.strip() if attire else None
        opp.contact = contact.strip() if contact else None
        opp.reviewer_mentor_id = _opt_id(reviewer_mentor_id)
        await audit.record(db, request, "opportunity.edit", f"Edited opportunity {opp.name}", entity_type="opportunity", entity_id=opp.id)
        await db.commit()
    return RedirectResponse(f"/admin/opportunities/{opp_id}/edit", status_code=303)


@router.post("/opportunities/{opp_id}/archive")
async def admin_opportunities_archive(opp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    opp = (await db.execute(select(Opportunity).where(Opportunity.id == opp_id))).scalars().first()
    if opp:
        opp.is_active = not opp.is_active
        opp.archived_at = datetime.utcnow() if not opp.is_active else None
        verb = "archive" if not opp.is_active else "restore"
        await audit.record(db, request, f"opportunity.{verb}", f"{verb.capitalize()}d opportunity {opp.name}", entity_type="opportunity", entity_id=opp.id)
        await db.commit()
    return RedirectResponse("/admin/opportunities?show_archived=1", status_code=303)


@router.post("/opportunities/{opp_id}/purge")
async def admin_opportunities_purge(opp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Permanently delete an ARCHIVED opportunity and ALL its history — shifts, their
    signups, and every hour submission logged against the opportunity or its shifts.

    Only allowed once the opportunity is archived — matches the students archive-then-purge
    flow. Deleting the submissions removes those hours from students' season totals.
    """
    if redirect := _require_auth(request):
        return redirect
    opp = (await db.execute(select(Opportunity).where(Opportunity.id == opp_id))).scalars().first()
    if opp and not opp.is_active:
        name = opp.name
        shift_ids = (
            await db.execute(select(Shift.id).where(Shift.opportunity_id == opp_id))
        ).scalars().all()
        # Delete every submission tied to the opportunity or any of its shifts.
        await db.execute(delete(HourSubmission).where(HourSubmission.opportunity_id == opp_id))
        if shift_ids:
            await db.execute(delete(HourSubmission).where(HourSubmission.shift_id.in_(shift_ids)))
            await db.execute(delete(Signup).where(Signup.shift_id.in_(shift_ids)))
            await db.execute(delete(Shift).where(Shift.opportunity_id == opp_id))
        await audit.record(
            db, request, "opportunity.purge",
            f"Permanently deleted archived opportunity {name} and all its shifts/signups/hours",
            entity_type="opportunity", entity_id=opp_id,
        )
        await db.execute(delete(Opportunity).where(Opportunity.id == opp_id))
        await db.commit()
    return RedirectResponse("/admin/opportunities?show_archived=1", status_code=303)


@router.post("/opportunities/{opp_id}/notify")
async def admin_opportunities_notify(opp_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """DM every student a reminder of the upcoming shifts they're signed up for in this
    opportunity (one grouped message per student)."""
    if redirect := _require_auth(request):
        return redirect

    opp = (
        await db.execute(select(Opportunity).options(selectinload(Opportunity.shifts)).where(Opportunity.id == opp_id))
    ).scalars().first()
    if not opp:
        return RedirectResponse("/admin/opportunities", status_code=303)

    now = now_utc()
    upcoming = {s.id: s for s in opp.shifts if s.end_time >= now}
    by_student: dict[int, tuple] = {}  # student_id -> (student, [shifts])
    if upcoming:
        signups = (
            await db.execute(
                select(Signup)
                .options(selectinload(Signup.student))
                .where(Signup.shift_id.in_(upcoming.keys()), Signup.status == SignupStatus.signed_up)
            )
        ).scalars().all()
        for su in signups:
            if su.student and su.student.slack_user_id:
                by_student.setdefault(su.student_id, (su.student, []))[1].append(upcoming[su.shift_id])

    sent = 0
    for student, shifts in by_student.values():
        shifts.sort(key=lambda s: s.start_time)
        lines = "\n".join(f"• {format_shift_range(s.start_time, s.end_time)}" for s in shifts)
        text = f"🔔 *Reminder — {opp.name}*\nYou're signed up for:\n{lines}"
        if opp.location:
            text += f"\nLocation: {opp.location}"
        if opp.attire:
            text += f"\nAttire: {opp.attire}"
        await send_dm(student.slack_user_id, text)
        sent += 1

    await audit.record(
        db, request, "opportunity.notify",
        f"Sent shift reminders for {opp.name} to {sent} student(s)",
        entity_type="opportunity", entity_id=opp_id,
    )
    await db.commit()
    return RedirectResponse(f"/admin/opportunities?notified={sent}", status_code=303)


@router.post("/opportunities/{opp_id}/shifts")
async def admin_shift_create(
    opp_id: int,
    request: Request,
    start_time: str = Form(...),
    end_time: str = Form(...),
    capacity: int = Form(0),
    notes: Optional[str] = Form(None),
    reviewer_mentor_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    start_dt = local_to_utc(datetime.fromisoformat(start_time))
    end_dt = local_to_utc(datetime.fromisoformat(end_time))
    if end_dt <= start_dt:
        return RedirectResponse(
            f"/admin/opportunities/{opp_id}/edit?error=Shift+end+time+must+be+after+its+start+time.",
            status_code=303,
        )

    # Announce the opportunity to Slack when its FIRST shift is added (opportunities are
    # created empty, so this is the moment there's finally something to sign up for).
    is_first_shift = (
        await db.execute(
            select(func.count()).select_from(Shift).where(Shift.opportunity_id == opp_id)
        )
    ).scalar() == 0
    db.add(Shift(
        opportunity_id=opp_id,
        start_time=start_dt,
        end_time=end_dt,
        capacity=capacity,
        notes=notes.strip() if notes else None,
        reviewer_mentor_id=_opt_id(reviewer_mentor_id),
    ))
    await audit.record(db, request, "shift.create", f"Added shift to opportunity {opp_id}", entity_type="shift")
    await db.commit()
    if is_first_shift and settings.slack_announce_channel:
        opp = (await db.execute(select(Opportunity).where(Opportunity.id == opp_id))).scalars().first()
        if opp:
            await announce_opportunity(opp)
    return RedirectResponse(f"/admin/opportunities/{opp_id}/edit", status_code=303)


@router.post("/shifts/{shift_id}/reviewer")
async def admin_shift_reviewer(
    shift_id: int,
    request: Request,
    reviewer_mentor_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Set/clear a shift's approver override (blank = use the opportunity's default)."""
    if redirect := _require_auth(request):
        return redirect
    shift = (await db.execute(select(Shift).where(Shift.id == shift_id))).scalars().first()
    if shift:
        shift.reviewer_mentor_id = _opt_id(reviewer_mentor_id)
        await audit.record(db, request, "shift.reviewer", f"Set approver override for shift {shift_id}", entity_type="shift", entity_id=shift_id)
        await db.commit()
        return RedirectResponse(f"/admin/opportunities/{shift.opportunity_id}/edit", status_code=303)
    return RedirectResponse("/admin/opportunities", status_code=303)


@router.post("/shifts/{shift_id}/send-prompt")
async def admin_shift_send_prompt(shift_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Test helper: DM the interactive 'log your hours' prompt to this shift's signed-up
    students right now, ignoring the usual end-time/prompted-once/already-submitted guards."""
    if redirect := _require_auth(request):
        return redirect

    shift = (
        await db.execute(select(Shift).options(selectinload(Shift.opportunity)).where(Shift.id == shift_id))
    ).scalars().first()
    if not shift:
        return RedirectResponse("/admin/opportunities", status_code=303)

    signups = (
        await db.execute(
            select(Signup)
            .options(
                selectinload(Signup.student),
                selectinload(Signup.shift).selectinload(Shift.opportunity),
            )
            .where(Signup.shift_id == shift_id, Signup.status == SignupStatus.signed_up)
        )
    ).scalars().all()

    default_hours = shift_length_hours(shift.start_time, shift.end_time)
    sent = 0
    for signup in signups:
        student = signup.student
        if student and student.slack_user_id:
            await send_dm(
                student.slack_user_id, "Log your volunteer hours",
                blocks=submission_service.post_shift_blocks(signup, default_hours),
            )
            sent += 1

    await audit.record(
        db, request, "shift.test_prompt",
        f"Sent test hours prompt for shift {shift_id} to {sent} student(s)",
        entity_type="shift", entity_id=shift_id,
    )
    await db.commit()
    return RedirectResponse(
        f"/admin/opportunities/{shift.opportunity_id}/edit?prompt_sent={sent}", status_code=303
    )


@router.post("/shifts/{shift_id}/delete")
async def admin_shift_delete(shift_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    shift = (await db.execute(select(Shift).where(Shift.id == shift_id))).scalars().first()
    if shift:
        opp_id = shift.opportunity_id
        await db.execute(delete(Shift).where(Shift.id == shift_id))
        await audit.record(db, request, "shift.delete", f"Deleted shift {shift_id}", entity_type="shift", entity_id=shift_id)
        await db.commit()
        return RedirectResponse(f"/admin/opportunities/{opp_id}/edit", status_code=303)
    return RedirectResponse("/admin/opportunities", status_code=303)


# ── Submissions ────────────────────────────────────────────────────────────────

@router.get("/submissions", response_class=HTMLResponse)
async def admin_submissions_list(
    request: Request, status: Optional[str] = None, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    q = (
        select(HourSubmission)
        .options(
            selectinload(HourSubmission.student),
            selectinload(HourSubmission.opportunity),
            selectinload(HourSubmission.reviewer),
        )
        .order_by(HourSubmission.submitted_at.desc())
    )
    try:
        status_filter = SubmissionStatus(status) if status else None
    except ValueError:
        status_filter = None
    if status_filter:
        q = q.where(HourSubmission.status == status_filter)
    subs = (await db.execute(q)).scalars().all()

    return templates.TemplateResponse(
        "admin/submissions.html",
        {
            "request": request,
            "submissions": subs,
            "statuses": list(SubmissionStatus),
            "current_status": status_filter.value if status_filter else "",
        },
    )


@router.get("/submissions/new", response_class=HTMLResponse)
async def admin_submissions_new_get(request: Request, db: AsyncSession = Depends(get_db)):
    """Form to add hours manually — e.g. backfilling volunteer time from before the app."""
    if redirect := _require_auth(request):
        return redirect
    students = (
        await db.execute(
            select(Student).where(Student.is_active.is_(True)).order_by(Student.name)
        )
    ).scalars().all()
    opps = (
        await db.execute(
            select(Opportunity).where(Opportunity.is_active.is_(True)).order_by(Opportunity.name)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        "admin/submission_new.html",
        {
            "request": request,
            "students": students,
            "opps": opps,
            "mentors": await _active_mentors(db),
            "statuses": list(SubmissionStatus),
            "today": today_local().isoformat(),
        },
    )


@router.post("/submissions/new")
async def admin_submissions_new_post(
    request: Request,
    student_id: int = Form(...),
    hours: float = Form(...),
    submitted_on: str = Form(""),
    opportunity_id: Optional[str] = Form(None),
    reviewer_mentor_id: Optional[str] = Form(None),
    report: Optional[str] = Form(None),
    status: str = Form(SubmissionStatus.approved.value),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    # Date the entry: noon local on the chosen day (avoids tz/day-boundary edges), else now.
    submitted_at = now_utc()
    if submitted_on.strip():
        try:
            d = date.fromisoformat(submitted_on.strip())
            submitted_at = local_to_utc(datetime.combine(d, time(12, 0)))
        except ValueError:
            pass
    try:
        new_status = SubmissionStatus(status)
    except ValueError:
        new_status = SubmissionStatus.approved

    submission = await submission_service.create_submission(
        db,
        student_id=student_id,
        opportunity_id=_opt_id(opportunity_id),
        shift_id=None,
        hours=hours,
        report=report.strip() if report else None,
        reviewer_mentor_id=_opt_id(reviewer_mentor_id),
        status=new_status,
        submitted_at=submitted_at,
    )
    student = (await db.execute(select(Student).where(Student.id == student_id))).scalars().first()
    await audit.record(
        db, request, "submission.add",
        f"Added {hours:.1f} manual hours ({new_status.value}) for "
        f"{student.name if student else student_id}",
        entity_type="submission", entity_id=submission.id,
    )
    await db.commit()
    return RedirectResponse("/admin/submissions?added=1", status_code=303)


@router.get("/submissions/{submission_id}/edit", response_class=HTMLResponse)
async def admin_submissions_edit_get(submission_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    submission = (
        await db.execute(
            select(HourSubmission)
            .options(
                selectinload(HourSubmission.student),
                selectinload(HourSubmission.opportunity),
                selectinload(HourSubmission.shift),
                selectinload(HourSubmission.reviewer),
            )
            .where(HourSubmission.id == submission_id)
        )
    ).scalars().first()
    if not submission:
        return RedirectResponse("/admin/submissions", status_code=303)
    mentors = (await db.execute(select(Mentor).order_by(Mentor.name))).scalars().all()
    return templates.TemplateResponse(
        "admin/submission_edit.html",
        {"request": request, "s": submission, "statuses": list(SubmissionStatus), "mentors": mentors},
    )


@router.post("/submissions/{submission_id}/edit")
async def admin_submissions_edit_post(
    submission_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    hours: float = Form(...),
    status: str = Form(...),
    reviewer_mentor_id: Optional[str] = Form(None),
    report: Optional[str] = Form(None),
    review_note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    submission = (
        await db.execute(
            select(HourSubmission).options(selectinload(HourSubmission.student)).where(HourSubmission.id == submission_id)
        )
    ).scalars().first()
    if not submission:
        return RedirectResponse("/admin/submissions", status_code=303)

    before = {"hours": submission.hours, "status": submission.status.value}
    new_status = SubmissionStatus(status)
    status_changed = new_status != submission.status

    submission.hours = hours
    submission.status = new_status
    submission.report = report.strip() if report else None
    submission.review_note = review_note.strip() if review_note else None
    submission.reviewer_mentor_id = (
        int(reviewer_mentor_id) if reviewer_mentor_id and reviewer_mentor_id.strip() else None
    )
    if status_changed:
        submission.reviewed_at = datetime.utcnow()

    await audit.record(
        db, request, "submission.edit",
        f"admin edited {submission.student.name}'s submission "
        f"({before['hours']}h {before['status']} → {hours}h {new_status.value})",
        entity_type="submission", entity_id=submission.id,
        detail={"before": before, "after": {"hours": hours, "status": new_status.value}},
    )
    await db.commit()

    # If an admin flipped a pending submission to a decision, notify the student too.
    if status_changed and new_status in (SubmissionStatus.approved, SubmissionStatus.rejected):
        background_tasks.add_task(submission_service.notify_student_of_review, submission.id)

    return RedirectResponse("/admin/submissions", status_code=303)


@router.post("/submissions/{submission_id}/decision")
async def admin_submissions_decision(
    submission_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    decision: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Quick approve/reject from the dashboard or list without opening the edit form."""
    if redirect := _require_auth(request):
        return redirect
    status = SubmissionStatus.approved if decision == "approve" else SubmissionStatus.rejected
    submission = await submission_service.set_status(db, submission_id, status)
    if submission:
        verb = "approved" if status == SubmissionStatus.approved else "rejected"
        await audit.record(
            db, request, f"submission.{verb}",
            f"admin {verb} {submission.student.name}'s submission ({submission.hours:.1f} hrs)",
            entity_type="submission", entity_id=submission.id,
        )
        await db.commit()
        background_tasks.add_task(submission_service.notify_student_of_review, submission.id)
    return RedirectResponse(request.headers.get("referer", "/admin/submissions"), status_code=303)


@router.post("/submissions/{submission_id}/delete")
async def admin_submissions_delete(submission_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Permanently delete a submission (e.g. a duplicate or bogus entry)."""
    if redirect := _require_auth(request):
        return redirect
    submission = (
        await db.execute(
            select(HourSubmission).options(selectinload(HourSubmission.student)).where(HourSubmission.id == submission_id)
        )
    ).scalars().first()
    if submission:
        who = submission.student.name if submission.student else "unknown"
        await audit.record(
            db, request, "submission.delete",
            f"Deleted {who}'s submission ({submission.hours:.1f} hrs, {submission.status.value})",
            entity_type="submission", entity_id=submission_id,
        )
        await db.execute(delete(HourSubmission).where(HourSubmission.id == submission_id))
        await db.commit()
    return RedirectResponse("/admin/submissions", status_code=303)


# ── Level Requirements (managed on the Settings page) ──────────────────────────

@router.post("/requirements")
async def admin_requirements_post(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    form = await request.form()
    changes = []
    for level in StudentLevel:
        raw = form.get(f"hours_{level.value}")
        if raw is None or not str(raw).strip():
            continue
        try:
            hours = float(raw)
        except ValueError:
            continue
        row = (await db.execute(select(LevelRequirement).where(LevelRequirement.level == level))).scalars().first()
        if row is None:
            db.add(LevelRequirement(level=level, required_hours=hours))
        else:
            row.required_hours = hours
        changes.append(f"{level_label(level)}={hours}")
    await audit.record(db, request, "requirement.set", "Updated level requirements: " + ", ".join(changes), entity_type="requirement")
    await db.commit()
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


# ── Settings ───────────────────────────────────────────────────────────────────

ENV_PATH = ".env"
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _write_env(updates: dict[str, str]) -> None:
    """Upsert KEY=value pairs into .env, preserving other lines."""
    # Values become raw KEY=VALUE lines below — strip any embedded CR/LF so a
    # submitted value can never inject an extra line (e.g. overwriting SSO_SECRET).
    updates = {k: v.replace("\r", "").replace("\n", "") for k, v in updates.items()}
    try:
        with open(ENV_PATH, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    written: set[str] = set()
    new_lines = []
    for line in lines:
        key = line.split("=", 1)[0].strip().upper()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            written.add(key)
        else:
            new_lines.append(line)
    for key, val in updates.items():
        if key not in written:
            new_lines.append(f"{key}={val}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(new_lines)


@router.get("/settings", response_class=HTMLResponse)
async def admin_settings_get(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    reqs = await level_requirements_map(db)
    requirement_rows = [{"level": level, "hours": reqs[level]} for level in StudentLevel]
    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            "season_start": await get_season_start(db),
            "slack_announce_channel": settings.slack_announce_channel,
            "timezone": settings.timezone,
            "reminder_lead_hours": settings.reminder_lead_hours,
            "auto_reject_days": settings.auto_reject_days,
            "backup_day": settings.backup_day,
            "backup_time": settings.backup_time,
            "backup_keep": settings.backup_keep,
            "updates_enabled": settings.updates_enabled,
            "requirement_rows": requirement_rows,
            "saved": request.query_params.get("saved"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/settings")
async def admin_settings_post(
    request: Request,
    season_start: str = Form(""),
    slack_announce_channel: str = Form(""),
    timezone: str = Form(...),
    reminder_lead_hours: int = Form(...),
    auto_reject_days: int = Form(...),
    backup_day: str = Form(...),
    backup_time: str = Form(...),
    backup_keep: int = Form(...),
    updates_enabled: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    # Season start (DB-backed override).
    parsed: Optional[date] = None
    if season_start.strip():
        try:
            parsed = date.fromisoformat(season_start.strip())
        except ValueError:
            parsed = None
    await set_season_start(db, parsed)

    # General config: validate each field, apply the valid ones, write changed
    # keys to .env once, mirror onto the live singleton, and re-apply the
    # scheduler (backup schedule / timezone).
    errors: list[str] = []
    env_updates: dict[str, str] = {}

    tz = timezone.strip()
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"Unknown timezone: {tz!r}.")
    else:
        if tz != settings.timezone:
            env_updates["TIMEZONE"] = tz
            settings.timezone = tz

    day = backup_day.strip().lower()
    if day not in _DAYS:
        errors.append("Backup day must be one of mon–sun.")
    elif day != settings.backup_day:
        env_updates["BACKUP_DAY"] = day
        settings.backup_day = day

    bt = backup_time.strip()
    if not _HHMM_RE.match(bt):
        errors.append("Backup time must be in HH:MM format.")
    elif bt != settings.backup_time:
        env_updates["BACKUP_TIME"] = bt
        settings.backup_time = bt

    if backup_keep < 1:
        errors.append("Backups to keep must be at least 1.")
    elif backup_keep != settings.backup_keep:
        env_updates["BACKUP_KEEP"] = str(backup_keep)
        settings.backup_keep = backup_keep

    if reminder_lead_hours < 0:
        errors.append("Reminder lead time cannot be negative.")
    elif reminder_lead_hours != settings.reminder_lead_hours:
        env_updates["REMINDER_LEAD_HOURS"] = str(reminder_lead_hours)
        settings.reminder_lead_hours = reminder_lead_hours

    if auto_reject_days < 0:
        errors.append("Auto-reject days cannot be negative.")
    elif auto_reject_days != settings.auto_reject_days:
        env_updates["AUTO_REJECT_DAYS"] = str(auto_reject_days)
        settings.auto_reject_days = auto_reject_days

    channel = slack_announce_channel.strip()
    if channel != settings.slack_announce_channel:
        env_updates["SLACK_ANNOUNCE_CHANNEL"] = channel
        settings.slack_announce_channel = channel

    if updates_enabled != settings.updates_enabled:
        env_updates["UPDATES_ENABLED"] = "true" if updates_enabled else "false"
        settings.updates_enabled = updates_enabled

    if env_updates:
        _write_env(env_updates)
        from app.services.scheduler import reschedule_all
        reschedule_all(getattr(request.app.state, "scheduler", None))

    await audit.record(
        db, request, "settings.update",
        f"Updated settings (season_start={parsed or 'all-time'}; timezone={settings.timezone}; "
        f"backup={settings.backup_day} {settings.backup_time} keep={settings.backup_keep}; "
        f"reminder_lead_hours={settings.reminder_lead_hours}; auto_reject_days={settings.auto_reject_days}; "
        f"updates_enabled={settings.updates_enabled})",
        entity_type="settings",
    )
    await db.commit()

    if errors:
        from urllib.parse import quote
        return RedirectResponse(
            f"/admin/settings?error={quote('; '.join(errors))}", status_code=303
        )
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


# ── Audit log ──────────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
async def admin_audit(request: Request, page: int = 1, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    page = max(page, 1)
    per_page = 50
    total = await db.scalar(select(func.count()).select_from(AuditLog)) or 0
    entries = (
        await db.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(per_page).offset((page - 1) * per_page)
        )
    ).scalars().all()
    total_pages = max((total + per_page - 1) // per_page, 1)
    return templates.TemplateResponse(
        "admin/audit.html",
        {"request": request, "entries": entries, "page": page, "total_pages": total_pages, "total": total},
    )


# ── Report ─────────────────────────────────────────────────────────────────────

def _parse_level(level: Optional[str]) -> Optional[StudentLevel]:
    try:
        return StudentLevel(level) if level else None
    except ValueError:
        return None


@router.get("/report", response_class=HTMLResponse)
async def admin_report(
    request: Request,
    level: Optional[str] = None,
    show_archived: int = 0,
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    level_filter = _parse_level(level)
    rows = await student_progress_report(
        db, level=level_filter, include_archived=bool(show_archived)
    )
    met = sum(1 for r in rows if r["met"])
    return templates.TemplateResponse(
        "admin/report.html",
        {
            "request": request,
            "rows": rows,
            "levels": list(StudentLevel),
            "current_level": level_filter.value if level_filter else "",
            "show_archived": bool(show_archived),
            "met_count": met,
        },
    )


@router.post("/report/notify")
async def admin_report_notify(
    request: Request,
    level: Optional[str] = None,
    show_archived: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """DM every active, Slack-linked student the same summary `/vhours` shows them."""
    if redirect := _require_auth(request):
        return redirect
    students = (
        await db.execute(
            select(Student).where(
                Student.is_active.is_(True), Student.slack_user_id.is_not(None)
            )
        )
    ).scalars().all()

    sent = 0
    for student in students:
        text = await student_vhours_message(db, student)
        await send_dm(student.slack_user_id, text)
        sent += 1

    await audit.record(
        db, request, "report.notify",
        f"DMed {sent} student(s) their volunteer-hours summary",
        entity_type="report",
    )
    await db.commit()
    qs = f"notified={sent}"
    if level:
        qs += f"&level={level}"
    if show_archived:
        qs += "&show_archived=1"
    return RedirectResponse(f"/admin/report?{qs}", status_code=303)


@router.get("/report/export")
async def admin_report_export(
    request: Request,
    level: Optional[str] = None,
    show_archived: int = 0,
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    rows = await student_progress_report(
        db, level=_parse_level(level), include_archived=bool(show_archived)
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Student", "Level", "Approved Hours", "Projected Hours", "Required Hours",
        "Remaining", "Percent Complete", "Pending Submissions", "Upcoming Shifts", "Met",
    ])
    for r in rows:
        s = r["student"]
        writer.writerow([
            s.name, level_label(s.level), r["approved"], r["projected"], r["required"],
            r["remaining"], r["pct"], r["pending_count"], r["upcoming_count"],
            "yes" if r["met"] else "no",
        ])

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=munus_report.csv"},
    )


# ── Backup / Restore ───────────────────────────────────────────────────────────

@router.get("/backup", response_class=HTMLResponse)
async def admin_backup_get(request: Request):
    if redirect := _require_auth(request):
        return redirect

    from app.services import backup
    return templates.TemplateResponse(
        "admin/backup.html",
        {
            "request": request,
            "is_sqlite": backup.is_sqlite(),
            "backups": backup.list_backups(),
            "result": request.query_params.get("result"),
            "message": request.query_params.get("message"),
        },
    )


@router.get("/backup/download")
async def admin_backup_download(request: Request):
    if redirect := _require_auth(request):
        return redirect

    from app.services import backup
    if not backup.is_sqlite():
        return RedirectResponse(
            "/admin/backup?result=error&message=Not+a+SQLite+database", status_code=303
        )

    tmp = os.path.join(tempfile.gettempdir(), f"munus-snapshot-{os.getpid()}.db")
    backup.create_snapshot(tmp)
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)

    filename = f"munus-backup-{datetime.now():%Y%m%d-%H%M}.db"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup/restore")
async def admin_backup_restore(
    request: Request,
    file: UploadFile = File(...),
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    from app.services import backup
    if confirm.strip().upper() != "RESTORE":
        return RedirectResponse(
            "/admin/backup?result=error&message=Type+RESTORE+to+confirm", status_code=303
        )

    contents = await file.read()
    ok, message = backup.stage_restore(contents)
    if ok:
        await audit.record(
            db, request, "backup.restore_staged",
            f"Staged restore from uploaded file {file.filename}", entity_type="backup",
        )
        await db.commit()
    result = "success" if ok else "error"
    return RedirectResponse(
        f"/admin/backup?result={result}&message={message.replace(' ', '+')}",
        status_code=303,
    )
