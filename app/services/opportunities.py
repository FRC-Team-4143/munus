"""
Opportunity / shift signup logic — capacity checks and signup/cancel.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Shift, Signup, SignupStatus


async def active_signup_count(db: AsyncSession, shift_id: int) -> int:
    """Number of students currently signed up (not cancelled) for a shift."""
    result = await db.execute(
        select(func.count())
        .select_from(Signup)
        .where(Signup.shift_id == shift_id, Signup.status == SignupStatus.signed_up)
    )
    return int(result.scalar() or 0)


async def remaining_capacity(db: AsyncSession, shift: Shift) -> Optional[int]:
    """Remaining open slots for a shift, or None when the shift is unlimited (capacity 0)."""
    if not shift.capacity:
        return None
    taken = await active_signup_count(db, shift.id)
    return max(0, shift.capacity - taken)


async def get_signup(db: AsyncSession, shift_id: int, student_id: int) -> Optional[Signup]:
    return (
        await db.execute(
            select(Signup).where(
                Signup.shift_id == shift_id, Signup.student_id == student_id
            )
        )
    ).scalars().first()


async def signup_student(db: AsyncSession, shift: Shift, student_id: int) -> tuple[bool, str]:
    """Sign a student up for a shift. Returns (ok, message). Enforces capacity and
    re-activates a previously cancelled signup rather than creating a duplicate."""
    existing = await get_signup(db, shift.id, student_id)
    if existing and existing.status == SignupStatus.signed_up:
        return False, "You're already signed up for this shift."

    remaining = await remaining_capacity(db, shift)
    if remaining is not None and remaining <= 0:
        return False, "This shift is full."

    if existing:
        existing.status = SignupStatus.signed_up
        existing.created_at = datetime.utcnow()
        existing.reminded_at = None
        existing.prompted_at = None
    else:
        db.add(Signup(shift_id=shift.id, student_id=student_id, status=SignupStatus.signed_up))
    await db.commit()
    return True, "You're signed up!"


async def cancel_signup(db: AsyncSession, signup: Signup) -> None:
    signup.status = SignupStatus.cancelled
    await db.commit()
