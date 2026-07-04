"""Smoke tests for the admin UI (auth + template rendering)."""
import pytest

from app.config import settings


async def _login(client):
    resp = await client.post("/admin/login", data={"password": settings.admin_password})
    assert resp.status_code in (200, 303)


async def test_admin_requires_auth(client):
    resp = await client.get("/admin/students")
    assert resp.status_code == 303  # redirect to login


@pytest.mark.parametrize("path", [
    "/admin", "/admin/opportunities", "/admin/submissions", "/admin/students",
    "/admin/mentors", "/admin/requirements", "/admin/import", "/admin/audit", "/admin/settings",
])
async def test_admin_pages_render(client, path):
    await _login(client)
    resp = await client.get(path)
    assert resp.status_code == 200


async def test_admin_create_opportunity_and_shift(client):
    await _login(client)
    # Create opportunity -> redirects to its edit page.
    resp = await client.post("/admin/opportunities", data={
        "name": "Park Cleanup", "description": "Pick up litter",
        "location": "River Park", "attire": "Old clothes", "contact": "Ms. Lee",
    })
    assert resp.status_code == 303
    edit_url = resp.headers["location"]

    edit = await client.get(edit_url)
    assert edit.status_code == 200
    assert "Park Cleanup" in edit.text

    # Add a shift to it.
    opp_id = edit_url.rstrip("/edit").split("/")[-1]
    resp = await client.post(f"/admin/opportunities/{opp_id}/shifts", data={
        "start_time": "2026-08-01T09:00", "end_time": "2026-08-01T12:00",
        "capacity": "6", "notes": "Bring gloves",
    })
    assert resp.status_code == 303
