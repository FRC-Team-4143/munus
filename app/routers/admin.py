"""
Admin routes — password-protected web UI.

Auth: session cookie signed with itsdangerous.
"""
import csv
import hashlib
import hmac
import io
import logging
import os
import tempfile
from datetime import date, datetime, time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
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

_signer = URLSafeTimedSerializer(settings.session_secret, salt="admin-session")
_COOKIE = "admin_session"
_MAX_AGE = 60 * 60 * 12  # 12 hours


def _student_code(name: str) -> str:
    return hashlib.sha256(name.strip().lower().encode()).hexdigest()[:8]


def _opt_id(raw: Optional[str]) -> Optional[int]:
    """Parse an optional integer form field (e.g. a mentor dropdown), '' -> None."""
    return int(raw) if raw and str(raw).strip() else None


async def _active_mentors(db: AsyncSession):
    return (await db.execute(select(Mentor).where(Mentor.is_active.is_(True)).order_by(Mentor.name))).scalars().all()


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _role(request: Request) -> Optional[str]:
    """The signed-in role from the session cookie: 'admin', 'manager', or None."""
    token = request.cookies.get(_COOKIE)
    if not token:
        return None
    try:
        value = _signer.loads(token, max_age=_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return value if value in ("admin", "manager") else "admin"  # legacy tokens = admin


def _is_authenticated(request: Request) -> bool:
    return _role(request) is not None


def _manager_allowed(path: str) -> bool:
    """The only routes a 'manager' may reach: creating/managing opportunities and shifts."""
    p = path.rstrip("/")
    return (
        p == "/admin/opportunities"
        or p.startswith("/admin/opportunities/")
        or p.startswith("/admin/shifts/")
    )


def _require_auth(request: Request):
    """Gate every admin route. Admins pass everywhere; a manager only on opportunity/shift
    paths (otherwise bounced to their Opportunities page); anonymous users to the login."""
    role = _role(request)
    if role is None:
        return RedirectResponse("/admin/login", status_code=303)
    if role == "admin" or _manager_allowed(request.url.path):
        return None
    return RedirectResponse("/admin/opportunities", status_code=303)


# Expose the role to templates so the sidebar can hide admin-only sections from managers.
templates.env.globals["session_role"] = _role


# ── Login / logout ─────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def admin_login_get(request: Request, error: str = ""):
    return templates.TemplateResponse("admin/login.html", {"request": request, "error": error})


@router.post("/login")
async def admin_login_post(
    request: Request,
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    role = None
    if hmac.compare_digest(password, settings.admin_password):
        role = "admin"
    elif settings.manager_password and hmac.compare_digest(password, settings.manager_password):
        role = "manager"

    if role is None:
        await audit.record(db, request, "admin.login_failed", "Failed admin login attempt", actor="anonymous")
        await db.commit()
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Incorrect password."},
            status_code=401,
        )
    await audit.record(db, request, "admin.login", f"{role.capitalize()} signed in", actor=role)
    await db.commit()
    dest = "/admin" if role == "admin" else "/admin/opportunities"
    response = RedirectResponse(dest, status_code=303)
    response.set_cookie(_COOKIE, _signer.dumps(role), httponly=True, samesite="lax", max_age=_MAX_AGE)
    return response


@router.get("/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(_COOKIE)
    return response


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


# ── Students ───────────────────────────────────────────────────────────────────

@router.get("/students", response_class=HTMLResponse)
async def admin_students_list(
    request: Request, show_archived: int = 0, db: AsyncSession = Depends(get_db)
):
    if redirect := _require_auth(request):
        return redirect

    q = select(Student).order_by(Student.name)
    if not show_archived:
        q = q.where(Student.is_active.is_(True))
    students = (await db.execute(q)).scalars().all()

    return templates.TemplateResponse(
        "admin/students.html",
        {
            "request": request,
            "students": students,
            "levels": list(StudentLevel),
            "show_archived": bool(show_archived),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/students")
async def admin_students_create(
    request: Request,
    name: str = Form(...),
    level: str = Form(...),
    team_number: Optional[str] = Form(None),
    slack_user_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect

    student = Student(
        name=name.strip(),
        student_code=_student_code(name),
        level=StudentLevel(level),
        team_number=int(team_number) if team_number and team_number.strip() else None,
        slack_user_id=slack_user_id.strip() if slack_user_id else None,
    )
    db.add(student)
    await audit.record(db, request, "student.create", f"Created student {student.name}", entity_type="student")
    await db.commit()
    return RedirectResponse("/admin/students", status_code=303)


@router.get("/students/{student_id}/edit", response_class=HTMLResponse)
async def admin_students_edit_get(student_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    student = (await db.execute(select(Student).where(Student.id == student_id))).scalars().first()
    if not student:
        return RedirectResponse("/admin/students", status_code=303)
    return templates.TemplateResponse(
        "admin/student_edit.html",
        {"request": request, "student": student, "levels": list(StudentLevel)},
    )


@router.post("/students/{student_id}/edit")
async def admin_students_edit_post(
    student_id: int,
    request: Request,
    name: str = Form(...),
    level: str = Form(...),
    team_number: Optional[str] = Form(None),
    slack_user_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    student = (await db.execute(select(Student).where(Student.id == student_id))).scalars().first()
    if student:
        student.name = name.strip()
        student.level = StudentLevel(level)
        student.team_number = int(team_number) if team_number and team_number.strip() else None
        student.slack_user_id = slack_user_id.strip() if slack_user_id else None
        await audit.record(db, request, "student.edit", f"Edited student {student.name}", entity_type="student", entity_id=student.id)
        await db.commit()
    return RedirectResponse("/admin/students", status_code=303)


@router.post("/students/{student_id}/delete")
async def admin_students_delete(student_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Archive a student (soft delete) — preserves their submission history."""
    if redirect := _require_auth(request):
        return redirect
    student = (await db.execute(select(Student).where(Student.id == student_id))).scalars().first()
    if student and student.is_active:
        student.is_active = False
        student.archived_at = datetime.utcnow()
        await audit.record(db, request, "student.archive", f"Archived student {student.name}", entity_type="student", entity_id=student.id)
        await db.commit()
    return RedirectResponse("/admin/students?show_archived=1", status_code=303)


@router.post("/students/{student_id}/restore")
async def admin_students_restore(student_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    student = (await db.execute(select(Student).where(Student.id == student_id))).scalars().first()
    if student and not student.is_active:
        student.is_active = True
        student.archived_at = None
        await audit.record(db, request, "student.restore", f"Restored student {student.name}", entity_type="student", entity_id=student.id)
        await db.commit()
    return RedirectResponse("/admin/students?show_archived=1", status_code=303)


@router.post("/students/{student_id}/purge")
async def admin_students_purge(student_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Permanently delete a student and ALL their history (signups + submissions).

    Only allowed once the student is archived — matches Tempus's archive-then-purge flow.
    """
    if redirect := _require_auth(request):
        return redirect
    student = (await db.execute(select(Student).where(Student.id == student_id))).scalars().first()
    if student and not student.is_active:
        name = student.name
        await db.execute(delete(Signup).where(Signup.student_id == student_id))
        await db.execute(delete(HourSubmission).where(HourSubmission.student_id == student_id))
        await audit.record(
            db, request, "student.purge",
            f"Permanently deleted archived student {name} and all their signups/submissions",
            entity_type="student", entity_id=student_id,
        )
        await db.execute(delete(Student).where(Student.id == student_id))
        await db.commit()
    return RedirectResponse("/admin/students?show_archived=1", status_code=303)


# ── Mentors ────────────────────────────────────────────────────────────────────

@router.get("/mentors", response_class=HTMLResponse)
async def admin_mentors_list(request: Request, show_archived: int = 0, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    q = select(Mentor).order_by(Mentor.name)
    if not show_archived:
        q = q.where(Mentor.is_active.is_(True))
    mentors = (await db.execute(q)).scalars().all()
    return templates.TemplateResponse(
        "admin/mentors.html",
        {"request": request, "mentors": mentors, "show_archived": bool(show_archived)},
    )


@router.post("/mentors")
async def admin_mentors_create(
    request: Request,
    name: str = Form(...),
    slack_user_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    db.add(Mentor(name=name.strip(), slack_user_id=slack_user_id.strip() if slack_user_id else None))
    await audit.record(db, request, "mentor.create", f"Created mentor {name.strip()}", entity_type="mentor")
    await db.commit()
    return RedirectResponse("/admin/mentors", status_code=303)


@router.post("/mentors/{mentor_id}/edit")
async def admin_mentors_edit(
    mentor_id: int,
    request: Request,
    name: str = Form(...),
    slack_user_id: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    mentor = (await db.execute(select(Mentor).where(Mentor.id == mentor_id))).scalars().first()
    if mentor:
        mentor.name = name.strip()
        mentor.slack_user_id = slack_user_id.strip() if slack_user_id else None
        await audit.record(db, request, "mentor.edit", f"Edited mentor {mentor.name}", entity_type="mentor", entity_id=mentor.id)
        await db.commit()
    return RedirectResponse("/admin/mentors", status_code=303)


@router.post("/mentors/{mentor_id}/delete")
async def admin_mentors_delete(mentor_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    mentor = (await db.execute(select(Mentor).where(Mentor.id == mentor_id))).scalars().first()
    if mentor and mentor.is_active:
        mentor.is_active = False
        mentor.archived_at = datetime.utcnow()
        await audit.record(db, request, "mentor.archive", f"Archived mentor {mentor.name}", entity_type="mentor", entity_id=mentor.id)
        await db.commit()
    return RedirectResponse("/admin/mentors?show_archived=1", status_code=303)


@router.post("/mentors/{mentor_id}/restore")
async def admin_mentors_restore(mentor_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    mentor = (await db.execute(select(Mentor).where(Mentor.id == mentor_id))).scalars().first()
    if mentor and not mentor.is_active:
        mentor.is_active = True
        mentor.archived_at = None
        await audit.record(db, request, "mentor.restore", f"Restored mentor {mentor.name}", entity_type="mentor", entity_id=mentor.id)
        await db.commit()
    return RedirectResponse("/admin/mentors?show_archived=1", status_code=303)


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
    # Announce the opportunity to Slack when its FIRST shift is added (opportunities are
    # created empty, so this is the moment there's finally something to sign up for).
    is_first_shift = (
        await db.execute(
            select(func.count()).select_from(Shift).where(Shift.opportunity_id == opp_id)
        )
    ).scalar() == 0
    db.add(Shift(
        opportunity_id=opp_id,
        start_time=local_to_utc(datetime.fromisoformat(start_time)),
        end_time=local_to_utc(datetime.fromisoformat(end_time)),
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
            "timezone": settings.timezone,
            "reminder_lead_hours": settings.reminder_lead_hours,
            "requirement_rows": requirement_rows,
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/settings")
async def admin_settings_post(
    request: Request,
    season_start: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    parsed: Optional[date] = None
    if season_start.strip():
        try:
            parsed = date.fromisoformat(season_start.strip())
        except ValueError:
            parsed = None
    await set_season_start(db, parsed)
    await audit.record(db, request, "settings.update", f"Set season start to {parsed or 'all-time'}", entity_type="settings")
    await db.commit()
    return RedirectResponse("/admin/settings?saved=1", status_code=303)


# ── CSV Import ─────────────────────────────────────────────────────────────────

_LEVEL_ALIASES = {
    "freshman": StudentLevel.freshman,
    "team_4423": StudentLevel.team_4423,
    "4423": StudentLevel.team_4423,
    "team_4143": StudentLevel.team_4143,
    "4143": StudentLevel.team_4143,
}


@router.get("/import", response_class=HTMLResponse)
async def admin_import_get(request: Request):
    if redirect := _require_auth(request):
        return redirect
    return templates.TemplateResponse("admin/import.html", {"request": request})


@router.post("/import", response_class=HTMLResponse)
async def admin_import_post(request: Request, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect

    created, updated, errors = [], [], []
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    for i, row in enumerate(reader, start=2):  # row 1 = header
        row_type = (row.get("type") or "").strip().lower()
        name = (row.get("name") or "").strip()
        level_str = (row.get("level") or "").strip().lower()
        team_str = (row.get("team_number") or "").strip()
        slack_uid = (row.get("slack_user_id") or "").strip() or None

        if not row_type or not name:
            errors.append({"row": i, "reason": "Missing type or name", "data": dict(row)})
            continue
        if row_type not in ("student", "mentor"):
            errors.append({"row": i, "reason": f"Unknown type '{row_type}'", "data": dict(row)})
            continue

        if row_type == "student":
            level = _LEVEL_ALIASES.get(level_str)
            if level is None:
                errors.append({"row": i, "reason": f"Invalid level '{level_str}'", "data": dict(row)})
                continue
            team_number = int(team_str) if team_str.isdigit() else None
            existing = (await db.execute(select(Student).where(func.lower(Student.name) == name.lower()))).scalars().first()
            if existing:
                existing.level = level
                existing.team_number = team_number
                existing.slack_user_id = slack_uid
                updated.append(name)
            else:
                db.add(Student(
                    name=name, student_code=_student_code(name), level=level,
                    team_number=team_number, slack_user_id=slack_uid,
                ))
                created.append(name)
        else:  # mentor
            existing = (await db.execute(select(Mentor).where(func.lower(Mentor.name) == name.lower()))).scalars().first()
            if existing:
                if slack_uid:
                    existing.slack_user_id = slack_uid
                updated.append(name)
            else:
                db.add(Mentor(name=name, slack_user_id=slack_uid))
                created.append(name)

    if created or updated:
        await audit.record(
            db, request, "import.csv",
            f"CSV import: {len(created)} created, {len(updated)} updated, {len(errors)} error(s)",
            entity_type="import",
            detail={"created": created, "updated": updated, "error_count": len(errors), "filename": file.filename},
        )
    await db.commit()

    return templates.TemplateResponse(
        "admin/import.html",
        {"request": request, "created": created, "updated": updated, "errors": errors},
    )


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
