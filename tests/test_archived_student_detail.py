"""Tests for the member-search detail page (/admin/report/archived/students/{id}) —
mirrors Tempus's equivalent: full page (not the older Report-screen modal), a Total
Hours card that defaults to all-time and narrows with date_from/date_to, counting only
**approved** hours (matching every other total in Munus)."""
from datetime import datetime, timedelta

import pytest

from app.models import HourSubmission, SubmissionStatus

pytestmark = pytest.mark.asyncio


async def _add_submission(db, student_id, *, days_ago, hours, status=SubmissionStatus.approved):
    db.add(HourSubmission(
        student_id=student_id,
        hours=hours,
        status=status,
        submitted_at=datetime.utcnow() - timedelta(days=days_ago),
        reviewed_at=datetime.utcnow() - timedelta(days=days_ago) if status != SubmissionStatus.pending else None,
    ))
    await db.commit()


async def test_defaults_to_all_time_approved_total(authed_client, db, make_student):
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_submission(db, grad.id, days_ago=400, hours=3.0)
    await _add_submission(db, grad.id, days_ago=40, hours=2.0)

    resp = await authed_client.get(f"/admin/report/archived/students/{grad.id}")
    assert resp.status_code == 200
    assert "5.0 hrs" in resp.text
    assert "all time" in resp.text


async def test_pending_and_rejected_excluded_from_total(authed_client, db, make_student):
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_submission(db, grad.id, days_ago=10, hours=3.0, status=SubmissionStatus.approved)
    await _add_submission(db, grad.id, days_ago=10, hours=99.0, status=SubmissionStatus.pending)
    await _add_submission(db, grad.id, days_ago=10, hours=50.0, status=SubmissionStatus.rejected)

    resp = await authed_client.get(f"/admin/report/archived/students/{grad.id}")
    assert resp.status_code == 200
    assert "3.0 hrs" in resp.text
    # The pending/rejected submissions still show in the list, just don't count.
    assert "99.00" in resp.text
    assert "50.00" in resp.text


async def test_narrows_to_custom_range(authed_client, db, make_student):
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    recent = datetime.utcnow() - timedelta(days=40)
    await _add_submission(db, grad.id, days_ago=400, hours=3.0)
    await _add_submission(db, grad.id, days_ago=40, hours=2.0)

    d_from = (recent - timedelta(days=1)).date().isoformat()
    d_to = (recent + timedelta(days=1)).date().isoformat()
    resp = await authed_client.get(
        f"/admin/report/archived/students/{grad.id}?date_from={d_from}&date_to={d_to}"
    )
    assert resp.status_code == 200
    assert "2.0 hrs" in resp.text
    assert "3.0 hrs" not in resp.text
    assert "filtered to range" in resp.text


async def test_search_view_link_targets_new_page_not_modal(authed_client, db, make_student):
    grad = await make_student(name="Jane Doe", code="jane0001", is_active=False)

    resp = await authed_client.get("/admin/report/search?q=doe&archived=1")
    assert resp.status_code == 200
    assert f"/admin/report/archived/students/{grad.id}" in resp.text
    assert "data-bs-toggle=\"modal\"" not in resp.text


async def test_active_student_reachable_via_same_route(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001", is_active=True)
    resp = await authed_client.get(f"/admin/report/archived/students/{ada.id}")
    assert resp.status_code == 200
    assert "Ada Lovelace" in resp.text
    assert "badge bg-secondary\">Archived" not in resp.text
