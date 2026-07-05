from app.services import student_auth
from app.services.student_auth import (
    magic_link, make_magic_token, read_magic_token, safe_next,
)


def test_magic_token_roundtrip():
    token = make_magic_token(42)
    assert read_magic_token(token) == 42


def test_tampered_token_rejected():
    token = make_magic_token(42)
    assert read_magic_token(token + "x") is None
    assert read_magic_token("not-a-token") is None


def test_expired_token_rejected(monkeypatch):
    token = make_magic_token(7)
    # Force the max age to 0 so any elapsed time counts as expired.
    monkeypatch.setattr(student_auth, "MAGIC_MAX_AGE", -1)
    assert read_magic_token(token) is None


def test_magic_link_contains_token_and_next():
    link = magic_link(3, "/submit")
    assert "/enter?token=" in link
    assert "next=%2Fsubmit" in link
    # Default target adds no next param.
    assert "next=" not in magic_link(3)


def test_safe_next_blocks_open_redirects():
    assert safe_next("/submit") == "/submit"
    assert safe_next("//evil.com") == "/"
    assert safe_next("https://evil.com") == "/"
    assert safe_next(None) == "/"
