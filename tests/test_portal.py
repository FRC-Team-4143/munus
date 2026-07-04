"""End-to-end smoke tests for the student portal (exercises the Jinja templates)."""


async def _identify(client, code: str):
    return await client.post("/identify", data={"student_code": code})


async def test_identify_and_browse(client, make_student, make_mentor, make_opportunity, make_shift):
    student = await make_student(code="ada00001")
    await make_mentor(name="Coach Ray")
    opp = await make_opportunity(name="Food Drive", location="Community Center")
    shift = await make_shift(opp.id, capacity=2)

    # Bad code is rejected.
    bad = await _identify(client, "nope")
    assert bad.status_code == 401

    resp = await _identify(client, "ada00001")
    assert resp.status_code == 303

    listing = await client.get("/opportunities")
    assert listing.status_code == 200
    assert "Food Drive" in listing.text

    detail = await client.get(f"/opportunities/{opp.id}")
    assert detail.status_code == 200
    assert "Community Center" in detail.text
    assert "Sign up" in detail.text

    signup = await client.post(f"/shifts/{shift.id}/signup")
    assert signup.status_code == 303

    after = await client.get(f"/opportunities/{opp.id}")
    assert "Signed up" in after.text

    submit_form = await client.get("/submit")
    assert submit_form.status_code == 200
    assert "Coach Ray" in submit_form.text

    my_hours = await client.get("/my-hours")
    assert my_hours.status_code == 200
    assert "Season total" in my_hours.text


async def test_portal_requires_identity(client):
    resp = await client.get("/opportunities")
    assert resp.status_code == 303  # redirected to landing


async def test_in_progress_shift_still_visible(client, db, make_student, make_opportunity, make_shift):
    """A shift that has started but not ended should still show and be joinable."""
    await make_student(code="ada00001")
    opp = await make_opportunity(name="Cleanup")
    # Started an hour ago, ends in two hours.
    in_progress = await make_shift(opp.id, capacity=5, start_in_hours=-1, length_hours=3)

    await _identify(client, "ada00001")
    detail = await client.get(f"/opportunities/{opp.id}")
    assert detail.status_code == 200
    assert "Sign up" in detail.text  # the shift row rendered, not "No upcoming shifts"
