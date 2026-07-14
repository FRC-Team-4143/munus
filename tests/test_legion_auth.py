"""One-tap sign-in challenge client (services/legion_auth.py) — safe_next's
open-redirect guard, and start_challenge's POST /sso/challenge round trip."""
import httpx

from app.config import settings
from app.services import legion_auth


def test_safe_next_blocks_open_redirects():
    assert legion_auth.safe_next("/submit") == "/submit"
    assert legion_auth.safe_next("//evil.com") == "/"
    assert legion_auth.safe_next("https://evil.com") == "/"
    assert legion_auth.safe_next(None) == "/"
    # A leading "/\" is normalized to "//" by some browsers (protocol-relative) — must be
    # rejected too, matching Legion's own allowed_return_to guard.
    assert legion_auth.safe_next("/\\evil.com") == "/"
    assert legion_auth.safe_next("\\evil.com") == "/"


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=object(), response=self
            )

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; `response` is set per-test on the class."""
    response: _FakeResponse = _FakeResponse()

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, path, json=None):
        return type(self).response


async def test_start_challenge_returns_pending_url(monkeypatch):
    monkeypatch.setattr(settings, "legion_base_url", "http://legion.test")
    monkeypatch.setattr(settings, "legion_api_key", "key")
    monkeypatch.setattr(_FakeAsyncClient, "response", _FakeResponse(200, {"nonce": "abc123"}))
    monkeypatch.setattr(legion_auth.httpx, "AsyncClient", _FakeAsyncClient)

    url = await legion_auth.start_challenge("stu00001", return_to="/submit")
    assert url == "http://legion.test/sso/pending/abc123"


async def test_start_challenge_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr(settings, "legion_base_url", "http://legion.test")
    monkeypatch.setattr(settings, "legion_api_key", "key")
    monkeypatch.setattr(_FakeAsyncClient, "response", _FakeResponse(404, {}))
    monkeypatch.setattr(legion_auth.httpx, "AsyncClient", _FakeAsyncClient)

    assert await legion_auth.start_challenge("nope") is None


async def test_start_challenge_requires_configuration(monkeypatch):
    monkeypatch.setattr(settings, "legion_base_url", "")
    monkeypatch.setattr(settings, "legion_api_key", "")
    assert await legion_auth.start_challenge("stu00001") is None
