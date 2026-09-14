"""
Who made a schedule change, verified by asking auth_api.

The bearer token only identifies someone once auth_api's /auth/me accepts it;
nothing here decodes a JWT. Every failure resolves to None: a schedule save
must never depend on the auth service being up.
"""

from unittest.mock import patch

import httpx
import pytest

from app.core import actor as actor_module
from app.core.actor import resolve_actor
from app.models.notification_event import Actor

AUTH_URL = "http://auth.test/auth-api"

ME = {
    "id": 7,
    "email": "ana@planta.cl",
    "full_name": "Ana Pérez",
    "is_active": True,
    "is_superuser": False,
    "created_at": "2026-01-01T00:00:00Z",
    "companies": [],
}


@pytest.fixture(autouse=True)
def auth_url():
    with patch.object(actor_module.settings, "AUTH_API_URL", AUTH_URL):
        yield


def serve(handler):
    """Route the module's HTTP calls through `handler` instead of the network."""
    return patch.object(actor_module, "_transport", httpx.MockTransport(handler))


async def test_valid_bearer_resolves_to_the_user():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=ME)

    with serve(handler):
        actor = await resolve_actor("Bearer tok-123")

    assert actor == Actor(email="ana@planta.cl", name="Ana Pérez")
    assert seen == {"url": f"{AUTH_URL}/auth/me", "auth": "Bearer tok-123"}


async def test_user_without_a_full_name_still_resolves():
    with serve(lambda r: httpx.Response(200, json={**ME, "full_name": None})):
        assert await resolve_actor("Bearer t") == Actor(email="ana@planta.cl", name="")


async def test_no_header_asks_nobody():
    def handler(request):
        raise AssertionError("must not call auth_api without a token")

    with serve(handler):
        assert await resolve_actor(None) is None
        assert await resolve_actor("") is None
        assert await resolve_actor("Basic abc") is None
        assert await resolve_actor("Bearer ") is None


async def test_rejected_token_is_none():
    with serve(lambda r: httpx.Response(401, json={"detail": "expired"})):
        assert await resolve_actor("Bearer t") is None


async def test_auth_api_error_is_none():
    with serve(lambda r: httpx.Response(503)):
        assert await resolve_actor("Bearer t") is None


async def test_unreachable_auth_api_is_none():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with serve(handler):
        assert await resolve_actor("Bearer t") is None


async def test_timeout_is_none():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with serve(handler):
        assert await resolve_actor("Bearer t") is None


async def test_unexpected_body_is_none():
    with serve(lambda r: httpx.Response(200, text="<html>proxy error</html>")):
        assert await resolve_actor("Bearer t") is None
    with serve(lambda r: httpx.Response(200, json={"id": 7})):
        assert await resolve_actor("Bearer t") is None


async def test_unconfigured_auth_url_asks_nobody():
    def handler(request):
        raise AssertionError("must not call auth_api when AUTH_API_URL is unset")

    with patch.object(actor_module.settings, "AUTH_API_URL", ""), serve(handler):
        assert await resolve_actor("Bearer t") is None
