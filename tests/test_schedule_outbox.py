"""
The notification an endpoint enqueues alongside its schedule write.

These tests assert on the OutboxEvent handed to the CRUD layer: what shifts_api
promises to notify_api for each verb. Atomicity itself is proved against a real
database in test_outbox_integration.py.
"""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.models.notification_event import DeviceRef
from app.schemas.schedule import ScheduleCreate, SchedulePatch, ScheduleUpdate
from app.services.schedule_service import ScheduleService
from tests.conftest import make_db_record, make_special_days_json

CRUD_PATH = "app.services.schedule_service.schedule_crud"
DEVICE_PATH = "app.services.schedule_service.device_crud.get_device_ref"


def create_data(**overrides) -> ScheduleCreate:
    payload = {
        "deviceId": 1,
        "schedule": {
            "monday": {
                "workHours": {"start": "08:00", "end": "17:00"},
                "breaks": [{"start": "12:00", "durationMinutes": 60}],
            }
        },
        "validFrom": "2026-09-01",
    }
    payload.update(overrides)
    return ScheduleCreate.model_validate(payload)


async def run_create(data: ScheduleCreate, method: str = "create_with_auto_close"):
    """Drive create_schedule and return the event passed to the CRUD layer."""
    pool = AsyncMock()
    with patch(f"{CRUD_PATH}.{method}", new_callable=AsyncMock, return_value=1) as crud, \
         patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=make_db_record()):
        await ScheduleService.create_schedule(pool, data)
    return crud.call_args.args[2]


async def run_update(data, existing, patch_mode=False):
    """Drive update/patch_schedule and return the event passed to partial_update."""
    pool = AsyncMock()
    method = ScheduleService.patch_schedule if patch_mode else ScheduleService.update_schedule
    with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=existing), \
         patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True) as crud, \
         patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=existing):
        await method(pool, 1, data)
    return crud.call_args.args[3] if crud.call_args else None


# ==================== POST ====================


class TestCreateEnqueues:
    @pytest.mark.asyncio
    async def test_create_enqueues_a_created_event(self):
        event = await run_create(create_data())
        assert event is not None
        assert event.type_key == "shift_change"
        assert event.company_id == 3
        assert event.payload["action"] == "created"

    @pytest.mark.asyncio
    async def test_created_event_describes_the_new_hours(self):
        event = await run_create(create_data())
        assert event.payload["days"] == [
            {"day": "monday", "before": None, "after": "08:00–17:00", "change": "added"}
        ]
        assert event.payload["valid_from"] == "2026-09-01"

    @pytest.mark.asyncio
    async def test_bounded_create_goes_through_the_split_path_with_its_event(self):
        event = await run_create(
            create_data(validTo="2026-09-30"), method="create_with_split"
        )
        assert event.payload["action"] == "created"
        assert event.payload["valid_to"] == "2026-09-30"

    @pytest.mark.asyncio
    async def test_event_routes_to_the_device_company_not_the_request(self):
        pool = AsyncMock()
        other_company = DeviceRef(id=1, company_id=99, key="F1", display_name="F1")
        with patch(DEVICE_PATH, new_callable=AsyncMock, return_value=other_company), \
             patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock, return_value=1) as crud, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=make_db_record()):
            await ScheduleService.create_schedule(pool, create_data())
        assert crud.call_args.args[2].company_id == 99


# ==================== PUT / PATCH ====================


class TestUpdateEnqueues:
    @pytest.mark.asyncio
    async def test_update_reports_the_moved_hours(self):
        existing = make_db_record(days=["monday"])
        data = ScheduleUpdate.model_validate(
            {"schedule": {"monday": {"workHours": {"start": "07:00", "end": "16:00"}}}}
        )
        event = await run_update(data, existing)
        assert event.payload["action"] == "updated"
        assert event.payload["days"][0]["before"] == "08:00–17:00"
        assert event.payload["days"][0]["after"] == "07:00–16:00"

    @pytest.mark.asyncio
    async def test_patch_reports_the_moved_hours(self):
        existing = make_db_record(days=["monday"])
        data = SchedulePatch.model_validate(
            {"schedule": {"monday": {"workHours": {"start": "09:00", "end": "18:00"}}}}
        )
        event = await run_update(data, existing, patch_mode=True)
        assert event.payload["action"] == "patched"
        assert event.payload["days"][0]["after"] == "09:00–18:00"

    @pytest.mark.asyncio
    async def test_metadata_only_patch_enqueues_nothing(self):
        existing = make_db_record(days=["monday"])
        data = SchedulePatch.model_validate({"metadata": {"version": "2.0", "source": "api"}})
        assert await run_update(data, existing, patch_mode=True) is None

    @pytest.mark.asyncio
    async def test_patch_that_only_moves_validity_still_notifies(self):
        existing = make_db_record(days=["monday"], valid_from=date(2026, 9, 1))
        data = SchedulePatch.model_validate({"validFrom": "2026-10-01"})
        event = await run_update(data, existing, patch_mode=True)
        assert event.payload["validity_changed"] is True

    @pytest.mark.asyncio
    async def test_patch_adding_a_special_day_notifies(self):
        existing = make_db_record(days=["monday"])
        data = SchedulePatch.model_validate(
            {
                "specialDays": {
                    "2026-12-25": {"name": "Navidad", "type": "holiday"}
                }
            }
        )
        event = await run_update(data, existing, patch_mode=True)
        assert event.payload["special_days_changed"] == 1


# ==================== DELETE ====================


class TestDeleteEnqueues:
    @pytest.mark.asyncio
    async def test_delete_reports_the_hours_that_disappeared(self):
        pool = AsyncMock()
        existing = make_db_record(days=["monday", "tuesday"])
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=existing), \
             patch(f"{CRUD_PATH}.delete_current_by_device_id", new_callable=AsyncMock, return_value=True) as crud:
            await ScheduleService.delete_schedule(pool, 1)
        event = crud.call_args.args[3]
        assert event.payload["action"] == "deleted"
        assert [d["change"] for d in event.payload["days"]] == ["removed", "removed"]
        assert event.payload["days"][0]["before"] == "08:00–17:00"

    @pytest.mark.asyncio
    async def test_delete_by_id_carries_its_event(self):
        pool = AsyncMock()
        existing = make_db_record(days=["friday"])
        with patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=existing), \
             patch(f"{CRUD_PATH}.delete_by_id", new_callable=AsyncMock, return_value=True) as crud:
            await ScheduleService.delete_schedule(pool, 1, schedule_id=42)
        assert crud.call_args.args[2].payload["action"] == "deleted"


# ==================== Out of scope for this phase ====================


class TestSpecialDayEndpointsStaySilent:
    @pytest.mark.asyncio
    async def test_add_special_day_enqueues_nothing(self):
        from app.schemas.schedule import SpecialDaySchema

        pool = AsyncMock()
        existing = make_db_record(days=["monday"])
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=existing), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True) as crud, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=existing):
            await ScheduleService.add_special_day(
                pool, 1, "2026-12-25", SpecialDaySchema(name="Navidad", type="holiday")
            )
        # partial_update called positionally with (pool, id, data) — no event.
        assert len(crud.call_args.args) == 3

    @pytest.mark.asyncio
    async def test_delete_special_day_enqueues_nothing(self):
        pool = AsyncMock()
        existing = make_db_record(
            days=["monday"],
            special_days=make_special_days_json(
                {"2026-12-25": {"name": "Navidad", "type": "holiday"}}
            ),
        )
        with patch(f"{CRUD_PATH}.get_current_by_device_id", new_callable=AsyncMock, return_value=existing), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True) as crud:
            await ScheduleService.delete_special_day(pool, 1, "2026-12-25")
        assert len(crud.call_args.args) == 3


# ==================== Degraded device lookup ====================


class TestMissingDevice:
    @pytest.mark.asyncio
    async def test_schedule_still_saves_when_the_device_cannot_be_resolved(self):
        pool = AsyncMock()
        with patch(DEVICE_PATH, new_callable=AsyncMock, return_value=None), \
             patch(f"{CRUD_PATH}.create_with_auto_close", new_callable=AsyncMock, return_value=1) as crud, \
             patch(f"{CRUD_PATH}.get_by_id", new_callable=AsyncMock, return_value=make_db_record()):
            result = await ScheduleService.create_schedule(pool, create_data())
        assert result is not None
        assert crud.call_args.args[2] is None
