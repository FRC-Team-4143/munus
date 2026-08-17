"""Tests for the report page's CSV exports:

  * /admin/report/export — a single combined file: one row per hour submission,
    grouped by student, with a TOTAL subtotal row (approved hours only) after each
    student's submissions. No date range by default (blank = all-time), honors
    `archived=1`, independent of the season the on-screen report table is pinned to.
  * /admin/report/archived/students/{id}/export — one student's raw submission history,
    honoring the detail page's own date range.
"""
import csv
import io
from datetime import datetime, timedelta

import pytest

from app.models import HourSubmission, StudentLevel, SubmissionStatus

pytestmark = pytest.mark.asyncio


async def _add_submission(db, student_id, *, days_ago, hours=3.0,
                          status=SubmissionStatus.approved):
    db.add(HourSubmission(
        student_id=student_id,
        hours=hours,
        status=status,
        submitted_at=datetime.utcnow() - timedelta(days=days_ago),
    ))
    await db.commit()


def _rows(resp):
    """Parse a CSV response into (header, list-of-rows)."""
    parsed = list(csv.reader(io.StringIO(resp.text)))
    return parsed[0], parsed[1:]


def _total_row_for(rows, name):
    return next(r for r in rows if r[0] == name and r[6] == "TOTAL")


def _submission_rows_for(rows, name):
    return [r for r in rows if r[0] == name and r[6] != "TOTAL"]


# ── Combined report export ───────────────────────────────────────────────────────

async def test_export_lists_submissions_and_a_total_row_per_student(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    grace = await make_student(name="Grace Hopper", code="gh000001")
    await _add_submission(db, ada.id, days_ago=3, hours=4.0)
    await _add_submission(db, ada.id, days_ago=2, hours=2.5)
    await _add_submission(db, grace.id, days_ago=1, hours=1.0)

    resp = await authed_client.get("/admin/report/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]

    header, rows = _rows(resp)
    assert header == [
        "Student", "Member Code", "Level", "Opportunity", "Shift", "Hours", "Status",
        "Submitted", "Reviewer", "Approved Hours", "Submissions",
    ]
    assert len(_submission_rows_for(rows, "Ada Lovelace")) == 2
    ada_total = _total_row_for(rows, "Ada Lovelace")
    assert ada_total[1] == "ada00001"
    assert ada_total[9] == "6.50"
    assert ada_total[10] == "2"
    assert len(_submission_rows_for(rows, "Grace Hopper")) == 1
    assert _total_row_for(rows, "Grace Hopper")[9] == "1.00"


async def test_export_total_counts_approved_only(authed_client, db, make_student):
    """Matches every other total in Munus — pending and rejected hours must not move it,
    but they still appear as submission rows."""
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_submission(db, ada.id, days_ago=3, hours=5.0)
    await _add_submission(db, ada.id, days_ago=2, hours=99.0, status=SubmissionStatus.pending)
    await _add_submission(db, ada.id, days_ago=1, hours=99.0, status=SubmissionStatus.rejected)

    resp = await authed_client.get("/admin/report/export")
    _, rows = _rows(resp)
    assert len(_submission_rows_for(rows, "Ada Lovelace")) == 3
    total = _total_row_for(rows, "Ada Lovelace")
    assert total[9] == "5.00"
    assert total[10] == "3"


async def test_export_includes_students_with_no_hours(authed_client, make_student):
    """A roster file needs the zero rows too — a missing line reads as "not on the
    team", not "no hours". A student with no submissions still gets a TOTAL row."""
    await make_student(name="Ada Lovelace", code="ada00001")

    resp = await authed_client.get("/admin/report/export")
    _, rows = _rows(resp)
    assert _submission_rows_for(rows, "Ada Lovelace") == []
    assert _total_row_for(rows, "Ada Lovelace")[9] == "0.00"


async def test_export_honors_date_range(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_submission(db, ada.id, days_ago=400, hours=9.0)   # outside
    await _add_submission(db, ada.id, days_ago=5, hours=2.0)     # inside

    d_from = (datetime.utcnow() - timedelta(days=10)).date().isoformat()
    d_to = (datetime.utcnow() + timedelta(days=1)).date().isoformat()
    resp = await authed_client.get(
        f"/admin/report/export?date_from={d_from}&date_to={d_to}"
    )
    _, rows = _rows(resp)
    assert len(_submission_rows_for(rows, "Ada Lovelace")) == 1
    assert _total_row_for(rows, "Ada Lovelace")[9] == "2.00"


async def test_export_spans_multiple_seasons(authed_client, db, make_student):
    """The point of this export: the season progress report has no date range at all and
    is pinned to season_start, so it can't answer "200 hours since freshman year"."""
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_submission(db, ada.id, days_ago=1200, hours=120.0)
    await _add_submission(db, ada.id, days_ago=2, hours=85.0)

    d_from = (datetime.utcnow() - timedelta(days=1500)).date().isoformat()
    d_to = (datetime.utcnow() + timedelta(days=1)).date().isoformat()
    resp = await authed_client.get(
        f"/admin/report/export?date_from={d_from}&date_to={d_to}"
    )
    _, rows = _rows(resp)
    assert _total_row_for(rows, "Ada Lovelace")[9] == "205.00"


async def test_export_archived_flag(authed_client, db, make_student):
    """A four-year cords window reaches students who have since been archived, so
    they're opt-in rather than absent."""
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_submission(db, grad.id, days_ago=500, hours=210.0)

    default = await authed_client.get("/admin/report/export")
    _, rows = _rows(default)
    assert not any(r[0] == "Old Grad" for r in rows)

    with_archived = await authed_client.get("/admin/report/export?archived=1")
    _, rows = _rows(with_archived)
    assert _total_row_for(rows, "Old Grad")[9] == "210.00"


async def test_export_filters_by_level(authed_client, make_student):
    await make_student(name="Ada Lovelace", code="ada00001", level=StudentLevel.team_4143)
    await make_student(name="Fresh Frank", code="frank001", level=StudentLevel.freshman)

    resp = await authed_client.get(
        f"/admin/report/export?level={StudentLevel.freshman.value}"
    )
    _, rows = _rows(resp)
    assert {r[0] for r in rows} == {"Fresh Frank"}


async def test_export_filename_reflects_range(authed_client, make_student):
    await make_student(name="Ada Lovelace", code="ada00001")

    all_time = await authed_client.get("/admin/report/export")
    assert "munus_report_all-time.csv" in all_time.headers["content-disposition"]

    ranged = await authed_client.get(
        "/admin/report/export?date_from=2022-08-01&date_to=2026-06-01"
    )
    assert "munus_report_2022-08-01_2026-06-01.csv" in ranged.headers["content-disposition"]


# ── Per-student detail export ──────────────────────────────────────────────────

async def test_student_submission_export_rows_and_range(authed_client, db, make_student):
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_submission(db, grad.id, days_ago=400, hours=3.0)
    await _add_submission(db, grad.id, days_ago=2, hours=2.0)

    all_time = await authed_client.get(f"/admin/report/archived/students/{grad.id}/export")
    assert all_time.status_code == 200
    assert "text/csv" in all_time.headers["content-type"]
    header, rows = _rows(all_time)
    assert header == [
        "Name", "Member Code", "Opportunity", "Shift", "Hours", "Status",
        "Submitted", "Reviewer",
    ]
    assert len(rows) == 2
    assert {r[4] for r in rows} == {"3.00", "2.00"}
    assert rows[0][1] == "grad0001"
    assert "old-grad_hours_all-time.csv" in all_time.headers["content-disposition"]

    d_from = (datetime.utcnow() - timedelta(days=10)).date().isoformat()
    d_to = (datetime.utcnow() + timedelta(days=1)).date().isoformat()
    ranged = await authed_client.get(
        f"/admin/report/archived/students/{grad.id}/export?date_from={d_from}&date_to={d_to}"
    )
    _, rows = _rows(ranged)
    assert [r[4] for r in rows] == ["2.00"]


async def test_student_submission_export_lists_every_status(authed_client, db, make_student):
    """The detail page's *list* shows all statuses (only its total is approved-only), so
    the file that mirrors it does too — with a Status column to sort on."""
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_submission(db, ada.id, days_ago=3, hours=4.0)
    await _add_submission(db, ada.id, days_ago=2, hours=1.0, status=SubmissionStatus.pending)

    resp = await authed_client.get(f"/admin/report/archived/students/{ada.id}/export")
    _, rows = _rows(resp)
    assert sorted(r[5] for r in rows) == ["approved", "pending"]


async def test_member_export_missing_student_redirects_to_search(authed_client):
    resp = await authed_client.get(
        "/admin/report/archived/students/99999/export", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/report/search"


# ── Where the buttons live ─────────────────────────────────────────────────────

async def test_export_button_only_appears_once_a_student_is_selected(authed_client, make_student):
    """The export is a per-student action, so it hangs off the single-student surfaces —
    the detail page and the report's per-student modal. The search *results* list stays a
    pure lookup with no export button on it."""
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    detail_export = f"/admin/report/archived/students/{ada.id}/export"

    detail = await authed_client.get(f"/admin/report/archived/students/{ada.id}")
    assert detail_export in detail.text

    # The modal's link is built in JS from the clicked row's student id.
    report = await authed_client.get("/admin/report")
    assert "studentSubsExportLink" in report.text
    assert "/admin/report/archived/students/' + id + '/export" in report.text

    search = await authed_client.get("/admin/report/search?q=ada")
    assert "Ada Lovelace" in search.text
    assert "/export" not in search.text


async def test_detail_export_button_carries_the_pages_date_range(authed_client, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")

    resp = await authed_client.get(
        f"/admin/report/archived/students/{ada.id}?date_from=2025-01-01&date_to=2025-06-30"
    )
    # `&` is HTML-escaped in the rendered href, as it should be.
    assert (
        f"/admin/report/archived/students/{ada.id}/export?date_from=2025-01-01&amp;date_to=2025-06-30"
        in resp.text
    )


async def test_report_page_offers_the_export_modal(authed_client, make_student):
    await make_student(name="Ada Lovelace", code="ada00001")

    resp = await authed_client.get("/admin/report")
    assert 'id="exportReportModal"' in resp.text
    assert 'data-bs-target="#exportReportModal"' in resp.text
    assert 'name="archived"' in resp.text


async def test_manager_can_reach_the_new_exports(client, make_student):
    """`munus-manager` is allowed on /admin/report/* — a manager who can already read
    the report can pull the same data as a file."""
    from app.services.sso import SSO_COOKIE
    from tests.conftest import make_sso_cookie

    student = await make_student(name="Ada Lovelace", code="ada00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["munus-manager"]))

    export = await client.get("/admin/report/export", follow_redirects=False)
    assert export.status_code == 200

    detail = await client.get(
        f"/admin/report/archived/students/{student.id}/export", follow_redirects=False
    )
    assert detail.status_code == 200
