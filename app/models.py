import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Integer, String, Boolean, Float, DateTime, Text,
    ForeignKey, Enum as SAEnum, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class StudentLevel(str, enum.Enum):
    """Drives the season volunteer-hour requirement."""
    freshman = "freshman"
    team_4423 = "team_4423"
    team_4143 = "team_4143"


class SubmissionStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class SignupStatus(str, enum.Enum):
    signed_up = "signed_up"
    cancelled = "cancelled"


# Default season requirement (hours) per level. Admins can override these in the
# level_requirements table via the admin Settings page.
DEFAULT_LEVEL_HOURS: dict[StudentLevel, float] = {
    StudentLevel.freshman: 5.0,
    StudentLevel.team_4423: 10.0,
    StudentLevel.team_4143: 15.0,
}

LEVEL_LABELS: dict[StudentLevel, str] = {
    StudentLevel.freshman: "Freshman",
    StudentLevel.team_4423: "4423 Student",
    StudentLevel.team_4143: "4143 Student",
}


def level_label(level: Optional[StudentLevel]) -> str:
    return LEVEL_LABELS.get(level, "—") if level else "—"


class AppSetting(Base):
    """Small key/value store for runtime-configurable app settings (e.g. season_start)."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_code: Mapped[Optional[str]] = mapped_column(String(8), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    level: Mapped[StudentLevel] = mapped_column(
        SAEnum(StudentLevel), nullable=False, default=StudentLevel.freshman
    )
    team_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slack_user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    signups: Mapped[List["Signup"]] = relationship("Signup", back_populates="student")
    submissions: Mapped[List["HourSubmission"]] = relationship(
        "HourSubmission", back_populates="student"
    )


class Mentor(Base):
    """Selectable as the reviewer of a student's hour submission."""
    __tablename__ = "mentors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slack_user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class LevelRequirement(Base):
    """Season required hours per student level — admin-editable, seeded from defaults."""
    __tablename__ = "level_requirements"

    level: Mapped[StudentLevel] = mapped_column(SAEnum(StudentLevel), primary_key=True)
    required_hours: Mapped[float] = mapped_column(Float, nullable=False)


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    attire: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    contact: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    shifts: Mapped[List["Shift"]] = relationship(
        "Shift", back_populates="opportunity", cascade="all, delete-orphan"
    )


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("opportunities.id"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0 = unlimited
    notes: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    opportunity: Mapped["Opportunity"] = relationship("Opportunity", back_populates="shifts")
    signups: Mapped[List["Signup"]] = relationship(
        "Signup", back_populates="shift", cascade="all, delete-orphan"
    )


class Signup(Base):
    __tablename__ = "signups"
    __table_args__ = (UniqueConstraint("shift_id", "student_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shift_id: Mapped[int] = mapped_column(Integer, ForeignKey("shifts.id"), nullable=False)
    student_id: Mapped[int] = mapped_column(Integer, ForeignKey("students.id"), nullable=False)
    status: Mapped[SignupStatus] = mapped_column(
        SAEnum(SignupStatus), nullable=False, default=SignupStatus.signed_up
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Set once a pre-shift reminder / post-shift prompt has been DMed, so the
    # scheduler never messages the same signup twice.
    reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    prompted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    shift: Mapped["Shift"] = relationship("Shift", back_populates="signups")
    student: Mapped["Student"] = relationship("Student", back_populates="signups")


class HourSubmission(Base):
    __tablename__ = "hour_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(Integer, ForeignKey("students.id"), nullable=False)
    opportunity_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("opportunities.id"), nullable=True
    )
    shift_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("shifts.id"), nullable=True)
    hours: Mapped[float] = mapped_column(Float, nullable=False)
    report: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reviewer_mentor_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("mentors.id"), nullable=True
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        SAEnum(SubmissionStatus), nullable=False, default=SubmissionStatus.pending
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    student: Mapped["Student"] = relationship("Student", back_populates="submissions")
    opportunity: Mapped[Optional["Opportunity"]] = relationship("Opportunity")
    shift: Mapped[Optional["Shift"]] = relationship("Shift")
    reviewer: Mapped[Optional["Mentor"]] = relationship("Mentor")


class AuditLog(Base):
    """Append-only record of admin/reviewer mutations."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    actor: Mapped[str] = mapped_column(String(50), nullable=False, default="admin")
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "submission.approve"
    entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
