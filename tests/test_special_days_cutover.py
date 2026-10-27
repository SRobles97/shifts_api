"""SPECIAL_DAYS_CUTOVER: single-date special days move to the exceptions path.

Mocked pool and CRUD layers, like test_endpoints.py. The flag is read at call
time, so each test flips it on the settings object.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.authz import Editor, require_schedule_editor
from app.core.config import settings
from app.core.dependencies import get_db_pool, verify_api_key
from app.main import app
from tests.conftest import make_db_record, make_special_days_json

CRUD_PATH = "app.services.schedule_service.schedule_crud"
BASE = "http://test/shifts-api/v1/schedules"
MESSAGE = (
    "Los días especiales de una sola fecha se registran con 'Cambiar un día' "
    "(/schedules/day-changes)"
)

PUT_BODY = {
    "schedule": {
        "monday": {
            "workHours": {"start": "09:00", "end": "18:00"},
            "breaks": [{"start": "13:00", "durationMinutes": 45}],
        }
    },
}


def _special(recurring):
    return {
        "2025-12-25": {
            "name": "Navidad", "type": "holiday",
            "workHours": None, "breaks": None,
            "isRecurring": recurring,
            "recurrencePattern": "yearly" if recurring else None,
        }
    }


@pytest.fixture
def mock_pool():
    return AsyncMock()


@pytest.fixture
async def client(mock_pool):
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[require_schedule_editor] = lambda: Editor(
        email="test@x.cl", name="", is_superuser=True, company_roles={}
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def cutover_on(monkeypatch):
    monkeypatch.setattr(settings, "SPECIAL_DAYS_CUTOVER", True)


def _crud_patches(existing, updated):
    return (
        patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=existing),
        patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True),
        patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=updated),
    )


def test_flag_ships_off():
    assert type(settings).model_fields["SPECIAL_DAYS_CUTOVER"].default is False


@pytest.mark.asyncio
async def test_cutover_off_keeps_current_behaviour(client, monkeypatch):
    monkeypatch.setattr(settings, "SPECIAL_DAYS_CUTOVER", False)
    existing = make_db_record(device_id=1)
    p1, p2, p3 = _crud_patches(existing, existing)
    with p1, p2, p3:
        put = await client.put("/1", json={**PUT_BODY, "specialDays": _special(False)})
        post = await client.post(
            "/special-days/1?date=2025-12-25",
            json={"name": "Navidad", "type": "holiday", "isRecurring": False},
        )
    assert put.status_code == 200
    assert post.status_code == 200


@pytest.mark.asyncio
async def test_cutover_on_rejects_non_recurring_in_put(client, cutover_on):
    existing = make_db_record(device_id=1)
    p1, p2, p3 = _crud_patches(existing, existing)
    with p1, p2 as update, p3:
        put = await client.put("/1", json={**PUT_BODY, "specialDays": _special(False)})
        patch_ = await client.patch("/1", json={"specialDays": _special(False)})
        post = await client.post(
            "/special-days/1?date=2025-12-25",
            json={"name": "Navidad", "type": "holiday"},
        )
    for resp in (put, patch_, post):
        assert resp.status_code == 400
        assert resp.json()["detail"] == MESSAGE
    update.assert_not_called()


@pytest.mark.asyncio
async def test_cutover_on_rejects_non_recurring_in_create(client, cutover_on, create_payload):
    resp = await client.post("/", json={**create_payload, "specialDays": _special(False)})
    assert resp.status_code == 400
    assert resp.json()["detail"] == MESSAGE


@pytest.mark.asyncio
async def test_cutover_on_accepts_recurring(client, cutover_on):
    existing = make_db_record(device_id=1)
    updated = make_db_record(device_id=1, special_days=make_special_days_json(_special(True)))
    p1, p2, p3 = _crud_patches(existing, updated)
    with p1, p2, p3:
        put = await client.put("/1", json={**PUT_BODY, "specialDays": _special(True)})
        post = await client.post(
            "/special-days/1?date=2025-12-25",
            json={"name": "Navidad", "type": "holiday", "isRecurring": True,
                  "recurrencePattern": "yearly"},
        )
    assert put.status_code == 200
    assert post.status_code == 200


@pytest.mark.asyncio
async def test_cutover_on_special_day_delete_removes_exception(client, cutover_on, mock_pool):
    mock_pool.acquire = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value="conn")
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    stub = MagicMock()
    stub.list = AsyncMock(return_value=[{"id": 7, "shift_type": "day"}])
    stub.strip_jsonb_key = AsyncMock(return_value=1)
    remove = AsyncMock()
    with patch("app.services.schedule_service.exceptions_crud", stub), \
         patch("app.services.day_change_service.remove_exception", remove), \
         patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock) as legacy:
        resp = await client.delete("/special-days/1/2025-12-25")
    assert resp.status_code == 200
    assert "2025-12-25" in resp.json()["message"]
    remove.assert_awaited_once()
    assert remove.await_args.args[1] == 7
    stub.list.assert_awaited_once_with(mock_pool, [1], date(2025, 12, 25), date(2025, 12, 25))
    assert stub.strip_jsonb_key.await_args.args == ("conn", 1, date(2025, 12, 25))
    legacy.assert_not_called()


@pytest.mark.asyncio
async def test_cutover_on_delete_without_exception_uses_json_path(client, cutover_on):
    stub = MagicMock()
    stub.list = AsyncMock(return_value=[])
    rec = make_db_record(device_id=1, special_days=make_special_days_json(_special(False)))
    with patch("app.services.schedule_service.exceptions_crud", stub), \
         patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=rec), \
         patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True) as upd:
        resp = await client.delete("/special-days/1/2025-12-25")
    assert resp.status_code == 200
    upd.assert_awaited_once()


@pytest.mark.asyncio
async def test_cutover_on_delete_ignores_other_shift_exception(client, cutover_on):
    stub = MagicMock()
    stub.list = AsyncMock(return_value=[{"id": 7, "shift_type": "night"}])
    remove = AsyncMock()
    rec = make_db_record(device_id=1, special_days=make_special_days_json(_special(False)))
    with patch("app.services.schedule_service.exceptions_crud", stub), \
         patch("app.services.day_change_service.remove_exception", remove), \
         patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=rec), \
         patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True):
        resp = await client.delete("/special-days/1/2025-12-25")
    assert resp.status_code == 200
    remove.assert_not_called()


@pytest.mark.asyncio
async def test_cutover_on_delete_keeps_http_errors(client, cutover_on):
    from fastapi import HTTPException

    stub = MagicMock()
    stub.list = AsyncMock(return_value=[{"id": 7, "shift_type": "day"}])
    remove = AsyncMock(side_effect=HTTPException(status_code=403, detail="no"))
    with patch("app.services.schedule_service.exceptions_crud", stub), \
         patch("app.services.day_change_service.remove_exception", remove):
        resp = await client.delete("/special-days/1/2025-12-25")
    assert resp.status_code == 403
