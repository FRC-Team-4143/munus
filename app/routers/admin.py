"""
Admin routes — password-protected web UI.

Auth: session cookie signed with itsdangerous.
"""
import csv
import hashlib
import hmac
import io
import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
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
from app.services.opportunities import active_signup_count
from app.services.requirements import level_requirements_map, resolve_required_hours, season_total_hours
from app.utils import local_to_utc, utc_to_local, format_shift_range

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%m/%d %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)
templates.env.filters["shiftrange"] = lambda s, e=None: format_shift_range(s, e)
templates.env.filters["levellabel"] = level_label

_signer = URLSafeTimedSerializer(settings.session_secret, salt="admin-session")
_COOKIE = "admin_session"
_MAX_AGE = 60 * 60 * 12  # 12 hours


def _student_code(name: str) -> str:
    return hashlib.sha256(name.strip().lower().encode()).hexdigest()[:8]


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(_COOKIE)
    if not token:
        return False
    try:
        _signer.loads(token, max_age=_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _require_auth(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


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
    if not hmac.compare_digest(password, settings.admin_password):
        await audit.record(db, request, "admin.login_failed", "Failed admin login attempt", actor="anonymous")
        await db.commit()
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Incorrect password."},
            status_code=401,
        )
    await audit.record(db, request, "admin.login", "Admin signed in")
    await db.commit()
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(_COOKIE, _signer.dumps("authenticated"), httponly=True, samesite="lax", max_age=_MAX_AGE)
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
        {"request": request, "opps": opps, "show_archived": bool(show_archived)},
    )


@router.post("/opportunities")
async def admin_opportunities_create(
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    attire: Optional[str] = Form(None),
    contact: Optional[str] = Form(None),
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
    return templates.TemplateResponse(
        "admin/opportunity_edit.html",
        {"request": request, "opp": opp, "shifts": shifts, "counts": counts},
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


@router.post("/opportunities/{opp_id}/shifts")
async def admin_shift_create(
    opp_id: int,
    request: Request,
    start_time: str = Form(...),
    end_time: str = Form(...),
    capacity: int = Form(0),
    notes: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    if redirect := _require_auth(request):
        return redirect
    db.add(Shift(
        opportunity_id=opp_id,
        start_time=local_to_utc(datetime.fromisoformat(start_time)),
        end_time=local_to_utc(datetime.fromisoformat(end_time)),
        capacity=capacity,
        notes=notes.strip() if notes else None,
    ))
    await audit.record(db, request, "shift.create", f"Added shift to opportunity {opp_id}", entity_type="shift")
    await db.commit()
    return RedirectResponse(f"/admin/opportunities/{opp_id}/edit", status_code=303)


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


# ── Level Requirements ─────────────────────────────────────────────────────────

@router.get("/requirements", response_class=HTMLResponse)
async def admin_requirements_get(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    reqs = await level_requirements_map(db)
    rows = [{"level": level, "hours": reqs[level]} for level in StudentLevel]
    return templates.TemplateResponse(
        "admin/requirements.html",
        {"request": request, "rows": rows, "saved": request.query_params.get("saved")},
    )


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
    return RedirectResponse("/admin/requirements?saved=1", status_code=303)


# ── Settings ───────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings_get(request: Request, db: AsyncSession = Depends(get_db)):
    if redirect := _require_auth(request):
        return redirect
    return templates.TemplateResponse(
        "admin/settings.html",
        {
            "request": request,
            "season_start": await get_season_start(db),
            "timezone": settings.timezone,
            "reminder_lead_hours": settings.reminder_lead_hours,
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
