"""Schedule writes need MANAGER in the company that owns the device."""

from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.core import authz as authz_module
from app.core.authz import require_schedule_editor
from app.core.dependencies import get_db_pool, verify_api_key
from app.main import app
from app.models.notification_event import DeviceRef
from app.schemas.schedule import ScheduleDeleteResponse, ScheduleRead

AUTH_URL = "http://auth.test/auth-api"
DEVICE = DeviceRef(id=7, company_id=2, key="1103", display_name="")
TOKEN = {"Authorization": "Bearer tok"}


def me(*roles, superuser=False, email="m@x.cl"):
    return {
        "id": 1,
        "email": email,
        "full_name": "Mia",
        "is_superuser": superuser,
        "companies": [
            {"id": cid, "name": f"c{cid}", "role": role, "is_primary": i == 0}
            for i, (cid, role) in enumerate(roles)
        ],
    }


@pytest.fixture
def real_client(mock_pool):
    """Client WITHOUT the superuser stub: exercises the real dependency."""
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides.pop(require_schedule_editor, None)
    with patch.object(authz_module.settings, "AUTH_API_URL", AUTH_URL), patch(
        "app.core.authz.device_crud.get_device_ref",
        new_callable=AsyncMock,
        return_value=DEVICE,
    ):
        yield AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/shifts-api/v1"
        )
    app.dependency_overrides.clear()


def serve(body=None, status=200, exc=None):
    def handler(request):
        if exc:
            raise exc
        return httpx.Response(status, json=body)

    return patch.object(authz_module, "_transport", httpx.MockTransport(handler))


SCHEDULE_READ = ScheduleRead(
    id="1", device_id=7, schedule={}, valid_from=date(2025, 1, 1), metadata={"version": "1.0", "source": "ui"}
)
SVC = "app.routers.schedules.schedule_service"
SPECIAL = {"name": "X", "type": "holiday", "workHours": None, "breaks": None}


ROUTES = [
    ("post", "/schedules/", "create_schedule", lambda p: {"json": p["create"]}),
    ("put", "/schedules/7", "update_schedule", lambda p: {"json": p["update"]}),
    ("patch", "/schedules/7", "patch_schedule", lambda p: {"json": p["patch"]}),
    ("post", "/schedules/special-days/7?date=2025-12-25", "add_special_day", lambda p: {"json": SPECIAL}),
    ("delete", "/schedules/special-days/7/2025-12-25", "delete_special_day", lambda p: {}),
    ("delete", "/schedules/7", "delete_schedule", lambda p: {}),
]
IDS = [r[2] for r in ROUTES]


@pytest.fixture
def payloads(create_payload, update_payload, patch_payload):
    return {"create": create_payload, "update": update_payload, "patch": patch_payload}


async def call(client, route, payloads, headers=TOKEN):
    method, path, _, kw = route
    return await client.request(method, path, headers=headers, **kw(payloads))


def svc_patch(route):
    """Patch the service method; result shape is irrelevant to authz."""
    result = (
        ScheduleDeleteResponse(message="ok")
        if route[2] == "delete_schedule" or route[2] == "delete_special_day"
        else SCHEDULE_READ
    )
    return patch(f"{SVC}.{route[2]}", new_callable=AsyncMock, return_value=result)


async def status_for(client, route, payloads, auth_me, headers=TOKEN, **serve_kw):
    with serve(auth_me, **serve_kw), svc_patch(route) as svc:
        resp = await call(client, route, payloads, headers)
    return resp.status_code, svc


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
async def test_analyst_in_owning_company_is_forbidden(real_client, payloads, route):
    code, svc = await status_for(real_client, route, payloads, me((2, "ANALYST")))
    assert code == 403
    svc.assert_not_awaited()


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
async def test_manager_in_owning_company_is_allowed(real_client, payloads, route):
    code, svc = await status_for(real_client, route, payloads, me((2, "MANAGER")))
    assert code != 403 and code < 500 and code != 401
    svc.assert_awaited_once()


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
async def test_manager_elsewhere_does_not_count(real_client, payloads, route):
    code, svc = await status_for(
        real_client, route, payloads, me((1, "MANAGER"), (2, "ANALYST"))
    )
    assert code == 403
    svc.assert_not_awaited()


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
async def test_no_bearer_is_401(real_client, payloads, route):
    code, svc = await status_for(real_client, route, payloads, me((2, "CEO")), headers={})
    assert code == 401
    svc.assert_not_awaited()


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
async def test_auth_api_rejecting_token_is_401(real_client, payloads, route):
    code, _ = await status_for(real_client, route, payloads, {}, status=401)
    assert code == 401


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
async def test_auth_api_unreachable_is_503(real_client, payloads, route):
    code, svc = await status_for(
        real_client, route, payloads, None, exc=httpx.ConnectError("down")
    )
    assert code == 503
    svc.assert_not_awaited()


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
async def test_superuser_is_allowed(real_client, payloads, route):
    code, svc = await status_for(real_client, route, payloads, me(superuser=True))
    assert code != 403 and code < 500 and code != 401
    svc.assert_awaited_once()


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
async def test_inactive_user_is_403_not_503(real_client, payloads, route):
    code, svc = await status_for(
        real_client, route, payloads, {"detail": "Inactive user"}, status=403
    )
    assert code == 403
    svc.assert_not_awaited()


async def test_auth_api_server_error_is_503(real_client, payloads):
    code, _ = await status_for(real_client, ROUTES[0], payloads, {}, status=500)
    assert code == 503


async def test_unreadable_body_is_503(real_client, payloads):
    code, _ = await status_for(real_client, ROUTES[0], payloads, ["not", "a", "dict"])
    assert code == 503


async def test_unknown_device_is_404(real_client, payloads):
    with patch(
        "app.core.authz.device_crud.get_device_ref",
        new_callable=AsyncMock,
        return_value=None,
    ):
        code, _ = await status_for(real_client, ROUTES[1], payloads, me((2, "CEO")))
    assert code == 404


async def test_create_by_device_name_is_authorised(real_client, create_payload):
    body = {k: v for k, v in create_payload.items() if k != "deviceId"}
    body["deviceName"] = "1103"
    with serve(me((2, "ANALYST"))), patch(
        "app.services.schedule_service.schedule_crud.get_device_id_by_name",
        new_callable=AsyncMock,
        return_value=7,
    ), patch(f"{SVC}.create_schedule", new_callable=AsyncMock) as svc:
        resp = await real_client.post("/schedules/", headers=TOKEN, json=body)
    assert resp.status_code == 403
    svc.assert_not_awaited()


async def test_actor_still_reaches_service(real_client, update_payload):
    with serve(me((2, "MANAGER"))), patch(
        f"{SVC}.update_schedule", new_callable=AsyncMock, return_value=SCHEDULE_READ
    ) as svc:
        await real_client.put("/schedules/7", headers=TOKEN, json=update_payload)
    assert svc.await_args.kwargs["actor"].email == "m@x.cl"


# --- batch authorisation -------------------------------------------------


def _refs(**company_by_id):
    return {
        int(k[1:]): DeviceRef(id=int(k[1:]), company_id=c, key=k[1:], display_name="")
        for k, c in company_by_id.items()
    }


def _editor(*roles, superuser=False):
    return authz_module.Editor(
        email="m@x.cl", name="Mia", is_superuser=superuser, company_roles=dict(roles)
    )


async def _batch(refs, editor, ids):
    with patch.object(
        authz_module.exceptions_crud, "device_refs", AsyncMock(return_value=refs)
    ):
        return await authz_module.assert_can_edit_devices(object(), editor, ids)


async def test_mixed_companies_rejected():
    with pytest.raises(authz_module.HTTPException) as e:
        await _batch(_refs(d1=2, d2=3), _editor((2, "MANAGER"), (3, "MANAGER")), [1, 2])
    assert e.value.status_code == 400
    assert e.value.detail == "Todas las máquinas deben ser de la misma empresa"


async def test_manager_of_other_company_forbidden():
    with pytest.raises(authz_module.HTTPException) as e:
        await _batch(_refs(d1=2, d2=2), _editor((9, "MANAGER"), (2, "ANALYST")), [1, 2])
    assert e.value.status_code == 403
    assert e.value.detail == "Su rol no permite modificar turnos en esta compañía"


async def test_unknown_device_404_first_missing_in_request_order():
    with pytest.raises(authz_module.HTTPException) as e:
        await _batch(_refs(d1=2), _editor((2, "MANAGER")), [1, 8, 5])
    assert e.value.status_code == 404
    assert e.value.detail == "Dispositivo 8 no encontrado"


async def test_unknown_device_404_beats_mixed_companies():
    with pytest.raises(authz_module.HTTPException) as e:
        await _batch(_refs(d1=2, d2=3), _editor(superuser=True), [1, 2, 99])
    assert e.value.status_code == 404


async def test_superuser_any_company_same_company_ok():
    refs = _refs(d1=5, d2=5)
    assert await _batch(refs, _editor(superuser=True), [1, 2]) == refs


async def test_returns_refs():
    refs = _refs(d1=2, d2=2)
    assert await _batch(refs, _editor((2, "MANAGER")), [1, 2]) == refs


async def test_empty_device_list_400():
    with pytest.raises(authz_module.HTTPException) as e:
        await _batch({}, _editor(), [])
    assert e.value.status_code == 400
    assert e.value.detail == "Debe indicar al menos una máquina"


async def test_superuser_mixed_companies_400():
    with pytest.raises(authz_module.HTTPException) as e:
        await _batch(_refs(d1=2, d2=3), _editor(superuser=True), [1, 2])
    assert e.value.status_code == 400
