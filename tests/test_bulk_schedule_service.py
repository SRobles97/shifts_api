"""Bulk regular-schedule routes, with the database and service mocked."""

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.core import authz as authz_module
from app.models.notification_event import DeviceRef
from app.schemas.day_changes import BulkDeviceResult, BulkScheduleResponse

SVC = "app.routers.schedules.bulk_schedule_service"

BODY = {
    "deviceIds": [1, 2],
    "shiftType": "day",
    "validFrom": "2026-11-03",
    "schedule": {"monday": {"workHours": {"start": "08:00", "end": "17:00"}}},
}
REFS = {
    1: DeviceRef(id=1, company_id=3, key="F1", display_name="Fresadora 1"),
    2: DeviceRef(id=2, company_id=3, key="F2", display_name="Fresadora 2"),
}


def _response(op=None):
    return BulkScheduleResponse(
        operation_id=op,
        valid_from=date(2026, 11, 3),
        applied=[
            BulkDeviceResult(
                device_id=1, label="Fresadora 1", new_valid_to=None, new_vigencia_kind="split"
            )
        ],
        skipped=[],
    )


async def test_bulk_routes_declared_before_device_id(client):
    op = uuid4()
    with patch.object(
        authz_module.exceptions_crud, "device_refs", AsyncMock(return_value=REFS)
    ), patch(f"{SVC}.apply_bulk", new_callable=AsyncMock, return_value=_response(op)) as apply_, patch(
        f"{SVC}.preview_bulk", new_callable=AsyncMock, return_value=_response()
    ) as preview:
        r_apply = await client.post("/schedules/bulk", json=BODY)
        r_preview = await client.post("/schedules/bulk/preview", json=BODY)

    assert r_apply.status_code == 200, r_apply.text
    body = r_apply.json()
    assert body["operationId"] == str(op)
    assert body["applied"][0]["deviceId"] == 1
    assert body["applied"][0]["newVigenciaKind"] == "split"
    assert r_preview.status_code == 200, r_preview.text
    assert r_preview.json()["operationId"] is None
    apply_.assert_awaited_once()
    preview.assert_awaited_once()
    args = apply_.await_args
    assert args.args[2] == REFS
    assert args.args[3].email == "test@x.cl"


async def test_bulk_mixed_companies_400(client):
    refs = {
        1: DeviceRef(id=1, company_id=3, key="F1"),
        2: DeviceRef(id=2, company_id=4, key="F2"),
    }
    with patch.object(
        authz_module.exceptions_crud, "device_refs", AsyncMock(return_value=refs)
    ), patch(f"{SVC}.apply_bulk", new_callable=AsyncMock) as apply_, patch(
        f"{SVC}.preview_bulk", new_callable=AsyncMock
    ) as preview:
        resp = await client.post("/schedules/bulk", json=BODY)
        resp_preview = await client.post("/schedules/bulk/preview", json=BODY)
    for r in (resp, resp_preview):
        assert r.status_code == 400
        assert r.json()["detail"] == "Todas las máquinas deben ser de la misma empresa"
    apply_.assert_not_awaited()
    preview.assert_not_awaited()


async def test_bulk_service_value_error_is_400(client):
    with patch.object(
        authz_module.exceptions_crud, "device_refs", AsyncMock(return_value=REFS)
    ), patch(f"{SVC}.apply_bulk", new_callable=AsyncMock, side_effect=ValueError("malo")):
        resp = await client.post("/schedules/bulk", json=BODY)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "malo"


async def test_bulk_invalid_body_is_422(client):
    resp = await client.post("/schedules/bulk", json={**BODY, "deviceIds": []})
    assert resp.status_code == 422


async def test_bulk_exclusion_violation_names_the_machine():
    import asyncpg
    import pytest

    from app.schemas.day_changes import BulkScheduleRequest
    from app.services import bulk_schedule_service

    req = BulkScheduleRequest.model_validate(BODY)
    svc = "app.services.bulk_schedule_service"
    with patch(f"{svc}.exceptions_crud.mirrored_ids", AsyncMock(return_value=set())), patch(
        f"{svc}.bulk_schedules_crud.apply_for_device",
        AsyncMock(side_effect=asyncpg.exceptions.ExclusionViolationError("conflicting key value")),
    ):
        with pytest.raises(ValueError) as exc:
            await bulk_schedule_service._run(AsyncMock(), req, REFS)
    assert str(exc.value) == "Fresadora 1: la vigencia se cruza con otro horario de la máquina"
