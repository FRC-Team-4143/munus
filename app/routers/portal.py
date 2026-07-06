"""
Student-facing portal — browse opportunities, sign up for shifts, submit hours.

Lightweight identity: the student enters their code once; it is stored in a signed
cookie. No passwords — this is a low-stakes internal tool.
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import (
    HourSubmission, Opportunity, Shift, Signup, SignupStatus, Student,
    SubmissionStatus, level_label,
)
from app.services import opportunities as opp_service
from app.services import submissions as submission_service
from app.services.requirements import resolve_required_hours, season_total_hours
from app.services.student_auth import (
    clear_session_cookie, read_magic_token, safe_next, set_session_cookie,
    student_id_from_session,
)
from app.utils import (
    format_date_range, format_shift_range, now_utc, shift_length_hours, utc_to_local,
)
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["localdt"] = (
    lambda dt, fmt="%b %d, %Y %I:%M %p": utc_to_local(dt).strftime(fmt) if dt else ""
)
templates.env.filters["shiftrange"] = lambda s, e=None: format_shift_range(s, e)
templates.env.filters["levellabel"] = level_label


# ── Student identity ───────────────────────────────────────────────────────────

async def _current_student(request: Request, db: AsyncSession) -> Optional[Student]:
    sid = student_id_from_session(request)
    if sid is None:
        return None
    student = (
        await db.execute(select(Student).where(Student.id == sid))
    ).scalars().first()
    if student is None or not student.is_active:
        return None
    return student


async def _season_progress(db: AsyncSession, student: Student) -> dict:
    """Season progress vs the student's level requirement.

    `projected` is a forward-looking estimate that stays stable across a shift's lifecycle:
    approved hours + any *pending* submission (at its submitted value) + the scheduled length
    of every signed-up shift the student hasn't logged yet (including ones that have already
    ended). A shift keeps counting until its hours are approved (then counted at their real
    value) or rejected (dropped) — so the number never dips in the gap between a shift ending
    and its approval. `upcoming` (shifts not yet ended) is returned so callers that list them
    don't have to re-query.
    """
    total = await season_total_hours(db, student.id)
    required = await resolve_required_hours(db, student.level)

    # Pending submissions count toward the projection at their submitted value.
    pending_hours = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(HourSubmission.hours), 0.0)).where(
                    HourSubmission.student_id == student.id,
                    HourSubmission.status == SubmissionStatus.pending,
                )
            )
        ).scalar()
        or 0.0
    )

    # Shifts already logged (submission of any status) are counted by their submission, not
    # their scheduled length — so a rejected shift drops out of the estimate below.
    logged_shift_ids = set(
        (
            await db.execute(
                select(HourSubmission.shift_id).where(
                    HourSubmission.student_id == student.id,
                    HourSubmission.shift_id.is_not(None),
                )
            )
        ).scalars().all()
    )

    # Every signed-up shift (with its opportunity) — used both to list the upcoming ones and
    # to estimate the scheduled hours of shifts not yet logged.
    signups = (
        await db.execute(
            select(Signup)
            .options(selectinload(Signup.shift).selectinload(Shift.opportunity))
            .join(Shift, Shift.id == Signup.shift_id)
            .where(
                Signup.student_id == student.id,
                Signup.status == SignupStatus.signed_up,
            )
            .order_by(Shift.start_time)
        )
    ).scalars().all()
    now = now_utc()
    upcoming = [su for su in signups if su.shift.end_time >= now]
    projected = total + pending_hours + sum(
        shift_length_hours(su.shift.start_time, su.shift.end_time)
        for su in signups
        if su.shift_id not in logged_shift_ids
    )

    def _pct(value: float) -> int:
        return min(100, round((value / required) * 100)) if required else 100

    return {
        "total": total,
        "required": required,
        "remaining": max(0.0, required - total),
        "pct": _pct(total),
        "projected": projected,
        "projected_pct": _pct(projected),
        "upcoming": upcoming,
    }


# ── Landing / identify ─────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    student = await _current_student(request, db)
    if not student:
        return templates.TemplateResponse("portal/identify.html", {"request": request})

    progress = await _season_progress(db, student)
    recent = (
        await db.execute(
            select(HourSubmission)
            .options(
                selectinload(HourSubmission.opportunity),
                selectinload(HourSubmission.reviewer),
            )
            .where(HourSubmission.student_id == student.id)
            .order_by(HourSubmission.submitted_at.desc())
            .limit(5)
        )
    ).scalars().all()

    return templates.TemplateResponse(
        "portal/home.html",
        {
            "request": request,
            "student": student,
            "progress": progress,
            "upcoming": progress["upcoming"],
            "recent": recent,
        },
    )


@router.get("/enter")
async def enter(
    request: Request, token: str = "", next: str = "/", db: AsyncSession = Depends(get_db)
):
    """One-tap Slack magic-link sign-in: validate the token, set the session cookie."""
    sid = read_magic_token(token)
    if sid is not None:
        student = (await db.execute(select(Student).where(Student.id == sid))).scalars().first()
        if student and student.is_active:
            response = RedirectResponse(safe_next(next), status_code=303)
            set_session_cookie(response, student.id)
            return response
    return templates.TemplateResponse(
        "portal/identify.html",
        {"request": request, "error": "That sign-in link is invalid or expired — run "
                                       "`/vhours` in Slack for a fresh one."},
        status_code=401,
    )


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
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, student.id)
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
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
    cards = []
    for opp in opps:
        # Shifts that aren't fully over yet (upcoming or in progress) — what a student can
        # join. "Over" = both start and end have passed, so a shift stays visible even if
        # it has a bad end-before-start time.
        upcoming_shifts = [s for s in opp.shifts if s.start_time > now or s.end_time > now]
        cards.append({
            "opp": opp,
            "upcoming": len(upcoming_shifts),
            "date_range": format_date_range(upcoming_shifts),
        })
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
    # Show shifts that aren't fully over yet — a shift in progress is still joinable and
    # shouldn't disappear the moment it starts. "Over" = both start and end have passed.
    shifts = sorted(
        [s for s in opp.shifts if s.start_time > now or s.end_time > now],
        key=lambda s: s.start_time,
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

    # Outstanding shifts: signed up, the shift is fully over (both started AND ended —
    # guards against shifts with a bad end-before-start time), and not yet logged (no
    # submission for this student + shift). Mirrors the scheduler's post-shift query.
    now = now_utc()
    already_logged = (
        select(HourSubmission.id)
        .where(
            HourSubmission.student_id == student.id,
            HourSubmission.shift_id == Shift.id,
        )
        .correlate(Shift)
        .exists()
    )
    signups = (
        await db.execute(
            select(Signup)
            .options(selectinload(Signup.shift).selectinload(Shift.opportunity))
            .join(Shift, Shift.id == Signup.shift_id)
            .where(
                Signup.student_id == student.id,
                Signup.status == SignupStatus.signed_up,
                Shift.start_time <= now,
                Shift.end_time <= now,
                ~already_logged,
            )
            .order_by(Shift.end_time)
        )
    ).scalars().all()

    auto_reject_days = settings.auto_reject_days
    outstanding = []
    for su in signups:
        shift = su.shift
        deadline = None
        if auto_reject_days > 0:
            deadline = utc_to_local(shift.end_time + timedelta(days=auto_reject_days))
        outstanding.append({
            "signup_id": su.id,
            "opp_name": shift.opportunity.name if shift.opportunity else "Volunteer shift",
            "shift": shift,
            "default_hours": round(shift_length_hours(shift.start_time, shift.end_time), 2),
            "deadline": deadline,
        })

    return templates.TemplateResponse(
        "portal/submit.html",
        {"request": request, "student": student, "outstanding": outstanding,
         "auto_reject_days": auto_reject_days,
         "message": request.query_params.get("message")},
    )


@router.post("/submit/{signup_id}")
async def submit_shift(
    signup_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    hours: float = Form(...),
    report: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    signup = (
        await db.execute(
            select(Signup)
            .options(
                selectinload(Signup.shift).selectinload(Shift.opportunity),
                selectinload(Signup.student),
            )
            .where(Signup.id == signup_id)
        )
    ).scalars().first()
    if (
        signup is None
        or signup.student_id != student.id
        or signup.status != SignupStatus.signed_up
    ):
        return RedirectResponse("/submit", status_code=303)

    if hours <= 0:
        return RedirectResponse(
            "/submit?message=Enter+a+positive+number+of+hours.", status_code=303
        )

    submission = await submission_service.submit_shift_hours(
        db, signup, round(hours, 2), report.strip() if report and report.strip() else None
    )
    if submission is None:
        return RedirectResponse(
            "/submit?message=You've+already+logged+hours+for+this+shift.",
            status_code=303,
        )

    background_tasks.add_task(submission_service.notify_reviewer, submission.id)
    return RedirectResponse(
        f"/submit?message=Logged+{submission.hours:g}+hrs+for+approval.",
        status_code=303,
    )


# ── My hours ───────────────────────────────────────────────────────────────────

@router.get("/my-hours", response_class=HTMLResponse)
async def my_hours(request: Request, db: AsyncSession = Depends(get_db)):
    student = await _current_student(request, db)
    if not student:
        return RedirectResponse("/", status_code=303)

    progress = await _season_progress(db, student)

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
            "total": progress["total"],
            "required": progress["required"],
            "remaining": progress["remaining"],
            "pct": progress["pct"],
            "projected": progress["projected"],
            "projected_pct": progress["projected_pct"],
            "submissions": subs,
            "message": request.query_params.get("message"),
        },
    )
