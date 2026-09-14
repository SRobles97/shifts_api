"""
Who is making a schedule change, verified by auth_api.

`X-API-Key` authorises the app, not a person — it ships inside the web bundle.
The app additionally sends the user's bearer token, and this module asks
auth_api's /auth/me whether it is valid, the same way `backend` does
(backend/app/core/auth.py). No JWT decoding and no shared secret here.

Unlike backend's version this NEVER raises. The identity only feeds the
"Realizado por" line of the notification email; a schedule save must not fail,
or wait long, because auth_api is down, slow, or the token has expired — and
callers that send no bearer at all keep working exactly as before.
"""

from typing import Optional

import httpx
from fastapi import Header
from loguru import logger

from ..models.notification_event import Actor
from .config import settings

# Test seam: an httpx transport to use instead of the network.
_transport: Optional[httpx.AsyncBaseTransport] = None


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip() or None


async def resolve_actor(authorization: Optional[str]) -> Optional[Actor]:
    """The verified user behind an Authorization header, or None."""
    token = _bearer(authorization)
    if token is None or not settings.AUTH_API_URL:
        return None

    url = settings.AUTH_API_URL.rstrip("/") + "/auth/me"
    try:
        async with httpx.AsyncClient(
            timeout=settings.AUTH_ME_TIMEOUT_SECONDS, transport=_transport
        ) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        # Never log the token. The exception type is enough to tell a refused
        # connection from a timeout.
        logger.warning(f"auth_api unreachable, change actor unknown: {type(exc).__name__}")
        return None

    if resp.status_code != 200:
        if resp.status_code != 401:
            logger.warning(f"auth_api /auth/me answered {resp.status_code}, change actor unknown")
        return None

    try:
        body = resp.json()
        email = body.get("email")
    except (ValueError, AttributeError):
        logger.warning("auth_api /auth/me returned an unreadable body, change actor unknown")
        return None
    if not isinstance(email, str) or not email:
        return None

    name = body.get("full_name")
    return Actor(email=email, name=name if isinstance(name, str) else "")


async def get_actor(
    authorization: Optional[str] = Header(default=None),
) -> Optional[Actor]:
    """FastAPI dependency for the notifying schedule routes."""
    return await resolve_actor(authorization)
