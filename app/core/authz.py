"""
Per-company authorisation for schedule writes.

`X-API-Key` only authorises the app. Whether a *person* may change a device's
schedule depends on their role in the company that OWNS that device, which
auth_api reports in /auth/me. Unlike `actor.py` this fails CLOSED: anything
other than a clear answer from auth_api denies the request.
"""

from dataclasses import dataclass, field
from typing import Optional

import httpx
from fastapi import Header, HTTPException
from loguru import logger

from ..models.notification_event import Actor, DeviceRef
from ..repositories.devices import device_crud
from ..repositories.exceptions import exceptions_crud
from .actor import _bearer
from .config import settings

# Test seam: an httpx transport to use instead of the network.
_transport: Optional[httpx.AsyncBaseTransport] = None

ROLE_LEVELS = {"VIEWER": 1, "ANALYST": 2, "OPERATOR": 3, "MANAGER": 4, "CEO": 5}
SCHEDULE_EDIT_LEVEL = ROLE_LEVELS["MANAGER"]

_UNAVAILABLE = "Servicio de autenticación no disponible"


@dataclass(frozen=True)
class Editor:
    email: str
    name: str
    is_superuser: bool
    company_roles: dict = field(default_factory=dict)  # company id -> role name

    def actor(self) -> Actor:
        return Actor(email=self.email, name=self.name)


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail=_UNAVAILABLE)


async def require_schedule_editor(
    authorization: Optional[str] = Header(default=None),
) -> Editor:
    """Verify the bearer with auth_api and return who the caller is."""
    token = _bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="Autenticación requerida")
    if not settings.AUTH_API_URL:
        logger.warning("AUTH_API_URL not configured, denying schedule write")
        raise _unavailable()

    url = settings.AUTH_API_URL.rstrip("/") + "/auth/me"
    try:
        async with httpx.AsyncClient(
            timeout=settings.AUTH_ME_TIMEOUT_SECONDS, transport=_transport
        ) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        logger.warning(f"auth_api unreachable, denying schedule write: {type(exc).__name__}")
        raise _unavailable()

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
    if resp.status_code == 403:
        # auth_api: deactivated account. Deny explicitly; this is not an outage.
        raise HTTPException(status_code=403, detail="Usuario inactivo")
    if resp.status_code != 200:
        logger.warning(f"auth_api /auth/me answered {resp.status_code}, denying schedule write")
        raise _unavailable()

    try:
        body = resp.json()
        email = body.get("email")
        name = body.get("full_name")
        roles = {}
        for company in body.get("companies") or []:
            role = company.get("role")
            if isinstance(company.get("id"), int) and isinstance(role, str):
                roles[company["id"]] = role.upper()
        is_superuser = body.get("is_superuser") is True
    except (ValueError, AttributeError, TypeError):
        logger.warning("auth_api /auth/me returned an unreadable body, denying schedule write")
        raise _unavailable()
    if not isinstance(email, str) or not email:
        raise _unavailable()

    return Editor(
        email=email,
        name=name if isinstance(name, str) else "",
        is_superuser=is_superuser,
        company_roles=roles,
    )


def _assert_role_in_company(editor: Editor, company_id: int) -> None:
    """Raise 403 unless `editor` may change schedules of this company."""
    if editor.is_superuser:
        return
    level = ROLE_LEVELS.get(editor.company_roles.get(company_id, ""), 0)
    if level < SCHEDULE_EDIT_LEVEL:
        raise HTTPException(
            status_code=403,
            detail="Su rol no permite modificar turnos en esta compañía",
        )


async def assert_can_edit_device(pool, editor: Editor, device_id: int) -> None:
    """Raise 404/403 unless `editor` may change schedules of this device."""
    ref = await device_crud.get_device_ref(pool, device_id)
    if ref is None:
        raise HTTPException(status_code=404, detail=f"Dispositivo {device_id} no encontrado")
    _assert_role_in_company(editor, ref.company_id)


async def assert_can_edit_devices(
    pool, editor: Editor, device_ids: list[int]
) -> dict[int, DeviceRef]:
    """Raise 404/400/403 unless `editor` may change every device, all of one company."""
    if not device_ids:
        raise HTTPException(status_code=400, detail="Debe indicar al menos una máquina")
    refs = await exceptions_crud.device_refs(pool, device_ids)
    for device_id in device_ids:
        if device_id not in refs:
            raise HTTPException(status_code=404, detail=f"Dispositivo {device_id} no encontrado")
    companies = {ref.company_id for ref in refs.values()}
    if len(companies) > 1:
        raise HTTPException(
            status_code=400, detail="Todas las máquinas deben ser de la misma empresa"
        )
    for company_id in companies:
        _assert_role_in_company(editor, company_id)
    return refs
