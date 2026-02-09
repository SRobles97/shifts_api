"""
Unit tests for app.services.schedule_service.

The CRUD layer is mocked so these tests exercise service logic
(serialization, error mapping, data transformation) without a real DB.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services.schedule_service import (
    ScheduleService,
    _build_schedule_read,
    _calculate_work_hours_usage,
    _db_record_to_entity,
    _parse_break,
    _serialize_day_schedules,
    _serialize_extra_hours,
    _serialize_special_days,
)
from app.schemas.schedule import (
    BreakSchema,
    DayScheduleSchema,
    ExtraHourSchema,
    ScheduleCreate,
    SchedulePatch,
    ScheduleUpdate,
    SpecialDaySchema,
    WorkHoursSchema,
)

from tests.conftest import make_db_record, make_extra_hours_json, make_special_days_json


# ==================== Helper functions ====================


class TestSerializeDaySchedules:
    def test_round_trip(self):
        ds = {
            "monday": DayScheduleSchema(
                work_hours=WorkHoursSchema(start="08:00", end="17:00"),
                break_time=BreakSchema(start="12:00", duration_minutes=60),
            )
        }
        json_str = _serialize_day_schedules(ds)
        assert '"workHours"' in json_str
        assert '"durationMinutes"' in json_str


class TestSerializeExtraHours:
    def test_none(self):
        assert _serialize_extra_hours(None) is None

    def test_with_data(self):
        eh = {
            "monday": [ExtraHourSchema(start="18:00", end="20:00")]
        }
        json_str = _serialize_extra_hours(eh)
        assert '"18:00"' in json_str


class TestSerializeSpecialDays:
    def test_none(self):
        assert _serialize_special_days(None) is None

    def test_with_data(self):
        sd = {
            "2025-12-25": SpecialDaySchema(
                name="Navidad", type="holiday", is_recurring=True, recurrence_pattern="yearly"
            )
        }
        json_str = _serialize_special_days(sd)
        assert "Navidad" in json_str


class TestParseBreak:
    def test_camel_key(self):
        b = _parse_break({"start": "12:00", "durationMinutes": 60})
        assert b.duration_minutes == 60

    def test_snake_key(self):
        b = _parse_break({"start": "12:00", "duration_minutes": 30})
        assert b.duration_minutes == 30


class TestBuildScheduleRead:
    def test_basic(self):
        rec = make_db_record()
        sr = _build_schedule_read(rec)
        assert sr.id == "1"
        assert sr.device_id == 1
        assert "monday" in sr.schedule

    def test_with_extras(self):
        rec = make_db_record(
            extra_hours=make_extra_hours_json({"monday": [{"start": "18:00", "end": "20:00"}]})
        )
        sr = _build_schedule_read(rec)
        assert sr.extra_hours is not None
        assert len(sr.extra_hours["monday"]) == 1

    def test_with_special_days(self):
        rec = make_db_record(
            special_days=make_special_days_json({
                "2025-12-25": {
                    "name": "Navidad",
                    "type": "holiday",
                    "workHours": None,
                    "break": None,
                    "isRecurring": True,
                    "recurrencePattern": "yearly",
                }
            })
        )
        sr = _build_schedule_read(rec)
        assert sr.special_days is not None
        assert "2025-12-25" in sr.special_days


class TestDbRecordToEntity:
    def test_basic(self):
        rec = make_db_record()
        entity = _db_record_to_entity(rec)
        assert entity.device_id == 1
        assert entity.schedule.is_work_day("monday")

    def test_with_special_day_work_hours(self):
        rec = make_db_record(
            special_days=make_special_days_json({
                "2025-01-20": {
                    "name": "Medio día",
                    "type": "special_event",
                    "workHours": {"start": "08:00", "end": "13:00"},
                    "break": {"start": "11:00", "durationMinutes": 30},
                    "isRecurring": False,
                    "recurrencePattern": None,
                }
            })
        )
        entity = _db_record_to_entity(rec)
        assert "2025-01-20" in entity.special_days
        assert entity.special_days["2025-01-20"].work_hours.end == "13:00"


class TestCalculateWorkHoursUsage:
    def test_before_work(self):
        rec = make_db_record(days=["monday"])
        # Monday at 06:00
        now = datetime(2025, 1, 13, 6, 0, 0)
        stats = _calculate_work_hours_usage(rec, now)
        assert stats["hours_used"] == 0.0
        assert stats["usage_percentage"] == 0.0

    def test_after_work(self):
        rec = make_db_record(days=["monday"])
        now = datetime(2025, 1, 13, 18, 0, 0)
        stats = _calculate_work_hours_usage(rec, now)
        assert stats["usage_percentage"] == 100.0

    def test_non_work_day(self):
        rec = make_db_record(days=["monday"])
        # Sunday
        now = datetime(2025, 1, 12, 10, 0, 0)
        stats = _calculate_work_hours_usage(rec, now)
        assert stats["total_work_hours"] == 0.0


# ==================== ScheduleService ====================


CRUD_PATH = "app.services.schedule_service.schedule_crud"


class TestScheduleServiceCreate:
    @pytest.mark.asyncio
    async def test_create_schedule(self):
        pool = AsyncMock()
        data = ScheduleCreate.model_validate({
            "deviceId": 1,
            "schedule": {
                "monday": {
                    "workHours": {"start": "08:00", "end": "17:00"},
                    "break": {"start": "12:00", "durationMinutes": 60},
                }
            },
        })

        rec = make_db_record(device_id=1, days=["monday"])

        with patch(f"{CRUD_PATH}.upsert", new_callable=AsyncMock, return_value=1), \
             patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
            result = await ScheduleService.create_schedule(pool, data)

        assert result.device_id == 1
        assert "monday" in result.schedule


class TestScheduleServiceUpdate:
    @pytest.mark.asyncio
    async def test_update_not_found(self):
        pool = AsyncMock()
        data = ScheduleUpdate.model_validate({
            "schedule": {
                "monday": {
                    "workHours": {"start": "09:00", "end": "18:00"},
                    "break": {"start": "13:00", "durationMinutes": 45},
                }
            },
        })

        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            with pytest.raises(LookupError):
                await ScheduleService.update_schedule(pool, 999, data)

    @pytest.mark.asyncio
    async def test_update_success(self):
        pool = AsyncMock()
        data = ScheduleUpdate.model_validate({
            "schedule": {
                "monday": {
                    "workHours": {"start": "09:00", "end": "18:00"},
                    "break": {"start": "13:00", "durationMinutes": 45},
                }
            },
        })

        existing = make_db_record(device_id=5)
        updated = make_db_record(device_id=5, days=["monday"])

        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, side_effect=[existing, updated]), \
             patch(f"{CRUD_PATH}.upsert", new_callable=AsyncMock, return_value=5):
            result = await ScheduleService.update_schedule(pool, 5, data)

        assert result.device_id == 5


class TestScheduleServicePatch:
    @pytest.mark.asyncio
    async def test_patch_not_found(self):
        pool = AsyncMock()
        data = SchedulePatch.model_validate({"metadata": {"version": "2.0"}})

        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            with pytest.raises(LookupError):
                await ScheduleService.patch_schedule(pool, 999, data)

    @pytest.mark.asyncio
    async def test_patch_metadata_only(self):
        pool = AsyncMock()
        data = SchedulePatch.model_validate({"metadata": {"version": "2.0"}})

        existing = make_db_record(device_id=3)
        updated = make_db_record(device_id=3, version="2.0")

        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, side_effect=[existing, updated]), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True):
            result = await ScheduleService.patch_schedule(pool, 3, data)

        assert result.device_id == 3


class TestScheduleServiceGet:
    @pytest.mark.asyncio
    async def test_get_not_found(self):
        pool = AsyncMock()
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            result = await ScheduleService.get_schedule(pool, 999)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_found(self):
        pool = AsyncMock()
        rec = make_db_record(device_id=2)
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
            result = await ScheduleService.get_schedule(pool, 2)
        assert result.device_id == 2

    @pytest.mark.asyncio
    async def test_get_all(self):
        pool = AsyncMock()
        recs = [make_db_record(id=1, device_id=1), make_db_record(id=2, device_id=2)]
        with patch(f"{CRUD_PATH}.get_all", new_callable=AsyncMock, return_value=recs):
            results = await ScheduleService.get_all_schedules(pool)
        assert len(results) == 2


class TestScheduleServiceByDay:
    @pytest.mark.asyncio
    async def test_invalid_day(self):
        pool = AsyncMock()
        with pytest.raises(ValueError):
            await ScheduleService.get_schedules_by_day(pool, "notaday")

    @pytest.mark.asyncio
    async def test_valid_day(self):
        pool = AsyncMock()
        recs = [make_db_record()]
        with patch(f"{CRUD_PATH}.get_by_day", new_callable=AsyncMock, return_value=recs):
            results = await ScheduleService.get_schedules_by_day(pool, "monday")
        assert len(results) == 1


class TestScheduleServiceDelete:
    @pytest.mark.asyncio
    async def test_delete_not_found(self):
        pool = AsyncMock()
        with patch(f"{CRUD_PATH}.delete_by_device_id", new_callable=AsyncMock, return_value=False):
            with pytest.raises(LookupError):
                await ScheduleService.delete_schedule(pool, 999)

    @pytest.mark.asyncio
    async def test_delete_success(self):
        pool = AsyncMock()
        with patch(f"{CRUD_PATH}.delete_by_device_id", new_callable=AsyncMock, return_value=True):
            result = await ScheduleService.delete_schedule(pool, 1)
        assert result is True


class TestScheduleServiceSpecialDays:
    @pytest.mark.asyncio
    async def test_get_special_days_not_found(self):
        pool = AsyncMock()
        with patch(f"{CRUD_PATH}.get_special_days", new_callable=AsyncMock, return_value=None):
            with pytest.raises(LookupError):
                await ScheduleService.get_special_days(pool, 999)

    @pytest.mark.asyncio
    async def test_add_special_day_invalid_date(self):
        pool = AsyncMock()
        sd = SpecialDaySchema(name="Test", type="holiday")
        with pytest.raises(ValueError):
            await ScheduleService.add_special_day(pool, 1, "bad-date", sd)

    @pytest.mark.asyncio
    async def test_add_special_day_success(self):
        pool = AsyncMock()
        sd = SpecialDaySchema(name="Navidad", type="holiday")
        existing = make_db_record(device_id=1)
        updated = make_db_record(
            device_id=1,
            special_days=make_special_days_json({
                "2025-12-25": {
                    "name": "Navidad", "type": "holiday",
                    "workHours": None, "break": None,
                    "isRecurring": False, "recurrencePattern": None,
                }
            }),
        )

        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, side_effect=[existing, updated]), \
             patch(f"{CRUD_PATH}.partial_update", new_callable=AsyncMock, return_value=True):
            result = await ScheduleService.add_special_day(pool, 1, "2025-12-25", sd)

        assert result.special_days is not None

    @pytest.mark.asyncio
    async def test_delete_special_day_not_found(self):
        pool = AsyncMock()
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            with pytest.raises(LookupError):
                await ScheduleService.delete_special_day(pool, 999, "2025-12-25")


class TestScheduleServiceEffective:
    @pytest.mark.asyncio
    async def test_invalid_date(self):
        pool = AsyncMock()
        with pytest.raises(ValueError):
            await ScheduleService.get_effective_schedule(pool, 1, "not-a-date")

    @pytest.mark.asyncio
    async def test_not_found(self):
        pool = AsyncMock()
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=None):
            with pytest.raises(LookupError):
                await ScheduleService.get_effective_schedule(pool, 999, "2025-01-13")

    @pytest.mark.asyncio
    async def test_regular_work_day(self):
        pool = AsyncMock()
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
            # 2025-01-13 is a Monday
            result = await ScheduleService.get_effective_schedule(pool, 1, "2025-01-13")
        assert result is not None
        assert result.work_hours.start == "08:00"

    @pytest.mark.asyncio
    async def test_non_work_day(self):
        pool = AsyncMock()
        rec = make_db_record(device_id=1, days=["monday"])
        with patch(f"{CRUD_PATH}.get_by_device_id", new_callable=AsyncMock, return_value=rec):
            # 2025-01-12 is a Sunday
            result = await ScheduleService.get_effective_schedule(pool, 1, "2025-01-12")
        assert result is None
