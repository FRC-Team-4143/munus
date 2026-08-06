"""Tests for /health — unauthenticated liveness + DB check polled by Legion's admin
dashboard System Status panel."""


async def test_health_endpoint_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "app": "munus"}
