"""
Student-facing portal — browse opportunities, sign up for shifts, submit hours.

Lightweight identity: the student enters their code once; it is stored in a signed
cookie. No passwords — this is a low-stakes internal tool.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    HourSubmission, Mentor, Opportunity, Shift, Signup, SignupStatus, Student,
    SubmissionStatus, level_label,
)
from app.services import opportunities as opp_service
from app.services import submissions as submission_service
from app.services.requirements import resolve_required_hours, season_total_hours
from app.utils import format_shift_range, now_utc, utc_to_local
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%b %d, %Y %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)
templates.env.filters["shiftrange"] = lambda s, e=None: format_shift_range(s, e)
templates.env.filters["levellabel"] = level_label

_signer = URLSafeSerializer(settings.session_secret, salt="student-session")
_COOKIE = "munus_student"


# ── Student identity ───────────────────────────────────────────────────────────

def _set_student_cookie(response, student_id: int) -> None:
    response.set_cookie(
        _COOKIE, _signer.dumps(student_id),
        httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30,
    )


def _student_id_from_cookie(request: Request) -> Optional[int]:
    token = request.cookies.get(_COOKIE)
    if not token:
        return None
    try:
        return int(_signer.loads(token))
    except (BadSignature, ValueError, TypeError):
        return None


async def _current_student(request: Request, db: AsyncSession) -> Optional[Student]:
    sid = _student_id_from_cookie(request)
    if sid is None:
        return None
    student = (
        await db.execute(select(Student).where(Student.id == sid))
    ).scalars().first()
    if student is None or not student.is_active:
        return None
    return student


# ── Landing / identify ─────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    student = await _current_student(request, db)
    if student:
        return RedirectResponse("/opportunities", status_code=303)
    return templates.TemplateResponse("portal/identify.html", {"request": request})


@router.post("/identify")
async def identify(
    request: Request,
    student_code: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    code = student_code.strip().lower()
    student = (
        await db.execute(
            select(Student).where(
                Student.student_code == code, Student.is_active.is_(True)
            )
        )
    ).scalars().first()
    if not student:
        return templates.TemplateResponse(
            "portal/identify.html",
            {"request": request, "error": "That code didn't match an active student."},
            status_code=401,
        )
    response = RedirectResponse("/opportunities", status_code=303)
    _set_student_cookie(response, student.id)
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(_COOKIE)
    return response


# ── Opportunities ──────────────────────────────────────────────────────────────

@router.get("/opportunities", response_class=HTMLResponse)
async def opportunities_list(request: Request, db: AsyncSession = Depends(get_db)):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    opps = (
        await db.execute(
            select(Opportunity)
            .options(selectinload(Opportunity.shifts))
            .where(Opportunity.is_active.is_(True))
            .order_by(Opportunity.name)
        )
    ).scalars().all()

    now = now_utc()
    cards = [
        {
            "opp": opp,
            # Count shifts that haven't ended yet (upcoming or in progress).
            "upcoming": sum(1 for s in opp.shifts if s.end_time >= now),
        }
        for opp in opps
    ]
    return templates.TemplateResponse(
        "portal/opportunities.html",
        {"request": request, "student": student, "cards": cards},
    )


@router.get("/opportunities/{opp_id}", response_class=HTMLResponse)
async def opportunity_detail(
    opp_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    opp = (
        await db.execute(
            select(Opportunity)
            .options(selectinload(Opportunity.shifts))
            .where(Opportunity.id == opp_id)
        )
    ).scalars().first()
    if not opp:
        return RedirectResponse("/opportunities", status_code=303)

    now = now_utc()
    # Show shifts that haven't ended yet — a shift in progress is still joinable and
    # shouldn't disappear the moment it starts.
    shifts = sorted(
        [s for s in opp.shifts if s.end_time >= now], key=lambda s: s.start_time
    )
    # Which of this opportunity's shifts the student is already signed up for.
    my_signups = {
        row.shift_id: row
        for row in (
            await db.execute(
                select(Signup).where(
                    Signup.student_id == student.id,
                    Signup.status == SignupStatus.signed_up,
                )
            )
        ).scalars().all()
    }

    shift_rows = []
    for shift in shifts:
        remaining = await opp_service.remaining_capacity(db, shift)
        shift_rows.append({
            "shift": shift,
            "remaining": remaining,
            "is_full": remaining is not None and remaining <= 0,
            "signed_up": shift.id in my_signups,
            "signup_id": my_signups[shift.id].id if shift.id in my_signups else None,
        })

    return templates.TemplateResponse(
        "portal/opportunity.html",
        {"request": request, "student": student, "opp": opp, "shift_rows": shift_rows,
         "message": request.query_params.get("message")},
    )


@router.post("/shifts/{shift_id}/signup")
async def shift_signup(
    shift_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    shift = (
        await db.execute(select(Shift).where(Shift.id == shift_id))
    ).scalars().first()
    if not shift:
        return RedirectResponse("/opportunities", status_code=303)

    ok, message = await opp_service.signup_student(db, shift, student.id)
    return RedirectResponse(
        f"/opportunities/{shift.opportunity_id}?message={message.replace(' ', '+')}",
        status_code=303,
    )


@router.post("/signups/{signup_id}/cancel")
async def signup_cancel(
    signup_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    signup = (
        await db.execute(select(Signup).where(Signup.id == signup_id))
    ).scalars().first()
    if signup and signup.student_id == student.id:
        opp_id = (
            await db.execute(select(Shift.opportunity_id).where(Shift.id == signup.shift_id))
        ).scalars().first()
        await opp_service.cancel_signup(db, signup)
        return RedirectResponse(
            f"/opportunities/{opp_id}?message=Signup+cancelled", status_code=303
        )
    return RedirectResponse("/opportunities", status_code=303)


# ── Submit hours ───────────────────────────────────────────────────────────────

@router.get("/submit", response_class=HTMLResponse)
async def submit_get(request: Request, db: AsyncSession = Depends(get_db)):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    opps = (
        await db.execute(
            select(Opportunity)
            .options(selectinload(Opportunity.shifts))
            .where(Opportunity.is_active.is_(True))
            .order_by(Opportunity.name)
        )
    ).scalars().all()
    mentors = (
        await db.execute(
            select(Mentor).where(Mentor.is_active.is_(True)).order_by(Mentor.name)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        "portal/submit.html",
        {"request": request, "student": student, "opps": opps, "mentors": mentors,
         "message": request.query_params.get("message")},
    )


@router.post("/submit")
async def submit_post(
    request: Request,
    background_tasks: BackgroundTasks,
    hours: float = Form(...),
    reviewer_mentor_id: int = Form(...),
    opportunity_id: Optional[str] = Form(None),
    shift_id: Optional[str] = Form(None),
    report: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    opp_id = int(opportunity_id) if opportunity_id and opportunity_id.strip() else None
    sh_id = int(shift_id) if shift_id and shift_id.strip() else None

    submission = await submission_service.create_submission(
        db,
        student_id=student.id,
        opportunity_id=opp_id,
        shift_id=sh_id,
        hours=hours,
        report=report.strip() if report else None,
        reviewer_mentor_id=reviewer_mentor_id,
    )
    background_tasks.add_task(submission_service.notify_reviewer, submission.id)
    return RedirectResponse(
        "/my-hours?message=Submitted!+Your+reviewer+has+been+notified.", status_code=303
    )


# ── My hours ───────────────────────────────────────────────────────────────────

@router.get("/my-hours", response_class=HTMLResponse)
async def my_hours(request: Request, db: AsyncSession = Depends(get_db)):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    total = await season_total_hours(db, student.id)
    required = await resolve_required_hours(db, student.level)

    subs = (
        await db.execute(
            select(HourSubmission)
            .options(
                selectinload(HourSubmission.opportunity),
                selectinload(HourSubmission.reviewer),
            )
            .where(HourSubmission.student_id == student.id)
            .order_by(HourSubmission.submitted_at.desc())
        )
    ).scalars().all()

    return templates.TemplateResponse(
        "portal/my_hours.html",
        {
            "request": request,
            "student": student,
            "total": total,
            "required": required,
            "remaining": max(0.0, required - total),
            "pct": min(100, round((total / required) * 100)) if required else 100,
            "submissions": subs,
            "message": request.query_params.get("message"),
        },
    )
